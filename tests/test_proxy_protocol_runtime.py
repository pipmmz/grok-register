import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from proxy_protocol_runtime import ProtocolRuntimeManager, ProxyRuntimeError, RuntimeEntry
from proxy_protocols import parse_proxy_line


class FakeProcess:
    def __init__(self):
        self.code = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True
        self.code = -9


class ProtocolRuntimeTests(unittest.TestCase):
    def test_plain_http_descriptor_returns_direct_endpoint_without_runtime(self):
        manager = ProtocolRuntimeManager({"proxy_protocol_backend": "auto"})
        node = parse_proxy_line("http://127.0.0.1:8080")
        endpoint, key = manager.acquire(node)
        self.assertEqual(endpoint, node.canonical_uri)
        self.assertIsNone(key)
        self.assertEqual(manager.active_snapshot(), {})

    def test_socks_descriptor_is_normalized_to_local_http_endpoint_and_cached_idle(self):
        manager = ProtocolRuntimeManager({"proxy_protocol_backend": "auto", "proxy_runtime_idle_ttl_sec": 120})
        node = parse_proxy_line("socks5://user:pass@127.0.0.1:1080")
        endpoint, key = manager.acquire(node)
        self.assertTrue(endpoint.startswith("http://127.0.0.1:"))
        self.assertEqual(key, node.node_id)
        state = manager.active_snapshot()[key]
        self.assertEqual(state["kind"], "bridge")
        self.assertTrue(state["alive"])
        self.assertEqual(state["refcount"], 1)
        manager.release(key)
        self.assertEqual(manager.active_snapshot()[key]["refcount"], 0)
        manager.shutdown()
        self.assertEqual(manager.active_snapshot(), {})

    def test_idle_ttl_zero_stops_runtime_immediately(self):
        manager = ProtocolRuntimeManager({"proxy_protocol_backend": "auto", "proxy_runtime_idle_ttl_sec": 0})
        self.assertEqual(manager.idle_ttl, 0)
        node = parse_proxy_line("socks5://127.0.0.1:1080")
        endpoint, key = manager.acquire(node)
        self.assertTrue(endpoint.startswith("http://127.0.0.1:"))
        manager.release(key)
        self.assertEqual(manager.active_snapshot(), {})

    def test_build_config_exposes_local_http_and_advanced_outbound(self):
        manager = ProtocolRuntimeManager({})
        node = parse_proxy_line("trojan://secret@trojan.example.com:443?sni=trojan.example.com")
        value = manager._build_config(node, 32123)
        self.assertEqual(value["inbounds"][0]["type"], "http")
        self.assertEqual(value["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(value["inbounds"][0]["listen_port"], 32123)
        self.assertEqual(value["outbounds"][0]["type"], "trojan")
        self.assertEqual(value["outbounds"][0]["tag"], "proxy")
        self.assertEqual(value["route"]["final"], "proxy")

    def test_advanced_runtime_is_lazy_reused_then_cached_idle(self):
        manager = ProtocolRuntimeManager({"proxy_runtime_idle_ttl_sec": 120})
        node = parse_proxy_line("vless://11111111-1111-1111-1111-111111111111@a.example.com:443?security=tls")
        fd, path = tempfile.mkstemp()
        os.close(fd)
        process = FakeProcess()
        entry = RuntimeEntry(node.node_id, process, 32001, path, 0)
        with patch.object(manager, "_start_entry", return_value=entry) as start, patch.object(manager, "_stop_entry") as stop:
            first, first_key = manager.acquire(node)
            second, second_key = manager.acquire(node)
            self.assertEqual(first, "http://127.0.0.1:32001")
            self.assertEqual(second, first)
            self.assertEqual(first_key, node.node_id)
            self.assertEqual(second_key, node.node_id)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(manager.active_snapshot()[node.node_id]["refcount"], 2)
            manager.release(first_key)
            self.assertEqual(manager.active_snapshot()[node.node_id]["refcount"], 1)
            manager.release(second_key)
            self.assertEqual(manager.active_snapshot()[node.node_id]["refcount"], 0)
            self.assertEqual(stop.call_count, 0)
            manager._entries[node.node_id].idle_since = time.time() - 121
            manager.cleanup_idle()
            self.assertEqual(manager.active_snapshot(), {})
            stop.assert_called_once()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def test_native_only_allows_native_bridge_but_rejects_advanced_protocols(self):
        manager = ProtocolRuntimeManager({"proxy_protocol_backend": "native-only", "proxy_runtime_idle_ttl_sec": 0})
        socks = parse_proxy_line("socks5://127.0.0.1:1080")
        endpoint, key = manager.acquire(socks)
        try:
            self.assertTrue(endpoint.startswith("http://127.0.0.1:"))
        finally:
            manager.release(key)
        node = parse_proxy_line("trojan://secret@a.example.com:443?sni=a.example.com")
        with self.assertRaises(ProxyRuntimeError):
            manager.acquire(node)

    def test_concurrent_acquire_waits_for_inflight_start(self):
        """第二个调用者在等待别人启动运行时后必须拿到同一个出口,不能崩。"""
        import threading

        manager = ProtocolRuntimeManager({"proxy_runtime_idle_ttl_sec": 0})
        node = parse_proxy_line("socks5://127.0.0.1:1080")
        started = threading.Event()
        release = threading.Event()
        results = {}

        def slow_start(descriptor):
            started.set()
            release.wait(timeout=5)
            return RuntimeEntry(descriptor.node_id, FakeProcess(), 12345, "", 0, kind="sing-box")

        def worker(name):
            try:
                results[name] = manager._acquire_runtime_entry(node, slow_start)
            except Exception as exc:      # noqa: BLE001 - 测试要看到异常本身
                results[name] = exc

        first = threading.Thread(target=worker, args=("first",), daemon=True)
        first.start()
        self.assertTrue(started.wait(timeout=5))
        second = threading.Thread(target=worker, args=("second",), daemon=True)
        second.start()
        time.sleep(0.3)                   # 让第二个线程进入"等待别人启动"的分支
        release.set()
        first.join(timeout=5); second.join(timeout=5)
        self.assertEqual(results.get("first"), ("http://127.0.0.1:12345", node.node_id))
        self.assertEqual(results.get("second"), ("http://127.0.0.1:12345", node.node_id))

    def test_missing_singbox_reports_actionable_error(self):
        manager = ProtocolRuntimeManager({"proxy_protocol_backend": "auto", "proxy_singbox_path": ""})
        with patch("proxy_protocol_runtime.shutil.which", return_value=None):
            with self.assertRaises(ProxyRuntimeError) as raised:
                manager._find_executable()
        self.assertIn("sing-box", str(raised.exception))
        self.assertIn("proxy_singbox_path", str(raised.exception))


class MihomoBackendTests(unittest.TestCase):
    def test_backend_selection_follows_configuration(self):
        # 显式指定优先
        self.assertEqual(ProtocolRuntimeManager({"proxy_protocol_backend": "mihomo"}).core_name, "mihomo")
        self.assertEqual(ProtocolRuntimeManager({"proxy_protocol_backend": "sing-box"}).core_name, "sing-box")
        # auto:配了哪个路径就用哪个,sing-box 优先
        self.assertEqual(
            ProtocolRuntimeManager({"proxy_protocol_backend": "auto", "proxy_mihomo_path": "/opt/mihomo"}).core_name, "mihomo",
        )
        self.assertEqual(
            ProtocolRuntimeManager(
                {"proxy_protocol_backend": "auto", "proxy_singbox_path": "/opt/sing-box", "proxy_mihomo_path": "/opt/mihomo"}
            ).core_name,
            "sing-box",
        )
        # native-only 不解析核心
        self.assertEqual(ProtocolRuntimeManager({"proxy_protocol_backend": "native-only"}).core_name, "")
        # 未知取值直接报错
        with self.assertRaises(ProxyRuntimeError):
            ProtocolRuntimeManager({"proxy_protocol_backend": "bogus"})._resolve_backend()

    def test_mihomo_check_and_run_use_config_directory(self):
        from proxy_protocol_runtime import MihomoBackend

        backend = MihomoBackend({})
        path = os.path.join(tempfile.gettempdir(), "grok-register-mihomo-test", "config.yaml")
        self.assertEqual(backend.check_args("mihomo", path), ["mihomo", "-t", "-d", os.path.dirname(path)])
        self.assertEqual(backend.run_args("mihomo", path), ["mihomo", "-d", os.path.dirname(path)])

    def test_mihomo_writes_config_into_temp_directory_and_cleans_up(self):
        from proxy_protocol_runtime import MihomoBackend

        backend = MihomoBackend({})
        path = backend.write_config({"mixed-port": 1234})
        directory = os.path.dirname(path)
        try:
            self.assertTrue(path.endswith("config.yaml"))
            self.assertTrue(os.path.isdir(directory))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["mixed-port"], 1234)
        finally:
            backend.cleanup_config(path)
        self.assertFalse(os.path.exists(directory))

    def test_start_entry_uses_mihomo_commands_and_generated_config(self):
        manager = ProtocolRuntimeManager({"proxy_protocol_backend": "mihomo", "proxy_mihomo_path": "/fake/mihomo"})
        node = parse_proxy_line("trojan://secret@a.example.com:443?sni=a.example.com")
        recorded = {}

        def fake_popen(args, **kwargs):
            recorded["args"] = list(args)
            return FakeProcess()

        with patch.object(manager, "_find_executable", return_value="/fake/mihomo"), \
                patch.object(manager, "_check_config") as check, \
                patch.object(manager, "_free_port", return_value=32123), \
                patch.object(manager, "_port_ready", return_value=True), \
                patch("proxy_protocol_runtime.subprocess.Popen", side_effect=fake_popen):
            entry = manager._start_entry(node)
        try:
            self.assertEqual(entry.kind, "mihomo")
            self.assertEqual(entry.port, 32123)
            self.assertEqual(recorded["args"], ["/fake/mihomo", "-d", os.path.dirname(entry.config_path)])
            check.assert_called_once_with("/fake/mihomo", entry.config_path)
            with open(entry.config_path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
            self.assertEqual(config["mixed-port"], 32123)
            self.assertEqual(config["bind-address"], "127.0.0.1")
            self.assertEqual(config["rules"], ["MATCH,node"])
            self.assertEqual(config["proxies"][0]["type"], "trojan")
        finally:
            manager._stop_entry(entry)
        self.assertFalse(os.path.exists(entry.config_path))

    def test_mihomo_reports_actionable_error_when_binary_is_missing(self):
        manager = ProtocolRuntimeManager({"proxy_protocol_backend": "mihomo", "proxy_mihomo_path": ""})
        with patch("proxy_protocol_runtime.shutil.which", return_value=None):
            with self.assertRaises(ProxyRuntimeError) as raised:
                manager._find_executable()
        message = str(raised.exception)
        self.assertIn("mihomo", message)
        self.assertIn("proxy_mihomo_path", message)


if __name__ == "__main__":
    unittest.main()
