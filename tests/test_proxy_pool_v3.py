import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import proxy_bridge
import registration_flow
from app_config import DEFAULT_CONFIG
from proxy_bridge import LocalProxyBridge
from proxy_pool import ProxyPoolError, ProxyPoolManager, ProxyTransportError
from proxy_pool_store import ProxyPoolStore
from proxy_protocol_runtime import ProtocolRuntimeManager, RuntimeEntry
from proxy_protocols import ProxyProtocolError, parse_proxy_line
from registration_flow import (
    OUTCOME_UNCERTAIN, SAFE_NEW_LEASE, STAGE_PAGE_OPEN, STAGE_PROFILE_SUBMIT,
    RegistrationCallbacks, RegistrationOperations, registration_retry_disposition, run_batch,
)


class Cancelled(Exception):
    pass


class RetryNeeded(Exception):
    pass


class ProxyPoolV3Tests(unittest.TestCase):
    def cfg(self, **updates):
        value = dict(DEFAULT_CONFIG)
        value.update(updates)
        return value

    def test_strict_native_proxy_rejects_paths_queries_and_bad_placeholder_location(self):
        with self.assertRaises(ProxyProtocolError):
            parse_proxy_line("http://127.0.0.1:8080/path")
        with self.assertRaises(ProxyProtocolError):
            parse_proxy_line("http://127.0.0.1:8080/?x=1")
        with self.assertRaises(ProxyProtocolError):
            parse_proxy_line("http://proxy-{account}.example.com:8080")
        descriptor = parse_proxy_line("http://user-{account}:pass@127.0.0.1:8080")
        self.assertIn("{account}", descriptor.canonical_uri)

    def test_shadowsocks_sip002_is_parsed_for_singbox(self):
        descriptor = parse_proxy_line("ss://YWVzLTI1Ni1nY206c2VjcmV0@127.0.0.1:8388#SS")
        self.assertEqual(descriptor.protocol, "ss")
        self.assertEqual(descriptor.backend, "sing-box")
        self.assertEqual(descriptor.outbound_config["type"], "shadowsocks")
        self.assertEqual(descriptor.outbound_config["method"], "aes-256-gcm")
        self.assertEqual(descriptor.outbound_config["password"], "secret")

    def test_socks5_resolves_locally_while_socks5h_sends_hostname(self):
        class FakeSock:
            def __init__(self): self.sent = []
            def sendall(self, value): self.sent.append(value)
        responses = [b"\x05\x00", b"\x05\x00\x00\x01", b"\x00\x00\x00\x00", b"\x00\x00"]
        local = LocalProxyBridge("socks5://127.0.0.1:1080")
        sock = FakeSock()
        with patch.object(proxy_bridge, "_recv_exact", side_effect=responses), patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.10", 443))]) as resolver:
            local._socks5_connect(sock, "example.com", 443)
        resolver.assert_called_once()
        self.assertIn(b"\x01\xc6\x33\x64\x0a", sock.sent[-1])

        remote = LocalProxyBridge("socks5h://127.0.0.1:1080")
        sock2 = FakeSock()
        with patch.object(proxy_bridge, "_recv_exact", side_effect=responses), patch.object(socket, "getaddrinfo") as resolver2:
            remote._socks5_connect(sock2, "example.com", 443)
        resolver2.assert_not_called()
        self.assertIn(b"\x03\x0bexample.com", sock2.sent[-1])

    def test_runtime_idle_cache_reuses_entry_and_expires_by_ttl(self):
        manager = ProtocolRuntimeManager({"proxy_runtime_idle_ttl_sec": 120, "proxy_runtime_cache_max": 4})
        descriptor = parse_proxy_line("socks5://127.0.0.1:1080")
        fake_bridge = Mock(); fake_bridge.server = object(); fake_bridge.diagnostic.return_value = None
        entry = RuntimeEntry(descriptor.node_id, None, 31234, "", bridge=fake_bridge, kind="bridge", last_used=time.time())
        with patch.object(manager, "_start_bridge_entry", return_value=entry) as starter:
            endpoint, key = manager.acquire(descriptor)
            manager.release(key)
            self.assertIn(key, manager._entries)
            endpoint2, key2 = manager.acquire(descriptor)
            self.assertEqual(endpoint2, endpoint)
            self.assertEqual(key2, key)
            self.assertEqual(starter.call_count, 1)
            manager.release(key)
            manager._entries[key].idle_since = time.time() - 121
            manager.cleanup_idle()
            self.assertNotIn(key, manager._entries)
        fake_bridge.stop.assert_called()

    def test_health_state_persistence_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            cfg = self.cfg(proxy_mode="single", proxy="http://127.0.0.1:8001", proxy_pool_persist_health=True, proxy_pool_state_file=state, proxy_pool_probe_interval_sec=0)
            manager = ProxyPoolManager(cfg)
            lease = manager.acquire("a", "w", 1, 1, "s", timeout=1)
            manager.report_success(lease); manager.release(lease); manager.shutdown()
            restored = ProxyPoolManager(cfg)
            node = restored.snapshot()["nodes"][0]
            self.assertEqual(node["registration_successes"], 1)
            self.assertEqual(node["business_samples"], 1)
            restored.shutdown()

    def test_pool_file_rejects_subscription_url(self):
        """把订阅 URL 填进代理池文件(本地路径)时要给出明确提示。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.cfg(
                proxy_mode="pool", proxy_pool_file="http://127.0.0.1:10808",
                proxy_pool_store_file=os.path.join(tmp, "proxy_pool.json"), proxy_pool_probe_interval_sec=0,
            )
            manager = ProxyPoolManager(cfg)
            try:
                error = manager.snapshot()["error"]
                self.assertIn("代理池文件应为本地路径", error)
                self.assertIn("proxy_pool_subscription_url", error)
            finally:
                manager.shutdown()

    def test_config_change_while_leases_are_held_is_flagged_and_applied_later(self):
        """租约占用期间配置变更不能静默丢弃:标记待生效,租约释放后自动重建。"""
        import proxy_pool
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.cfg(proxy_mode="pool", proxy_pool_store_file=os.path.join(tmp, "proxy_pool.json"), proxy_pool_probe_interval_sec=0)
            proxy_pool.reset_manager()
            manager = proxy_pool.get_manager(config=cfg)
            try:
                manager.add_node("http://127.0.0.1:8001")
                lease = manager.acquire("a", "w", 1, 1, "s", timeout=1)
                updated = dict(cfg, proxy_pool_subscription_url="http://sub.test/list")

                same = proxy_pool.get_manager(config=updated)
                self.assertIs(same, manager)                       # 有租约:不能中途换 Manager
                self.assertTrue(same.snapshot()["config_pending"])  # 但变更不能被静默吞掉
                self.assertEqual([node["canonical"] for node in same.snapshot()["nodes"]], ["http://127.0.0.1:8001"])

                same.release(lease)
                response = Mock(status_code=200, text="socks5://9.10.11.12:1080\n", headers={})
                with patch("proxy_pool.requests.get", return_value=response):
                    rebuilt = proxy_pool.get_manager(config=updated)
                self.assertIsNot(rebuilt, manager)
                self.assertFalse(rebuilt.snapshot()["config_pending"])
                self.assertEqual(
                    sorted(node["canonical"] for node in rebuilt.snapshot()["nodes"]),
                    ["http://127.0.0.1:8001", "socks5://9.10.11.12:1080"],
                )
            finally:
                proxy_pool.reset_manager()

    def test_empty_pool_error_names_the_failing_source(self):
        """订阅已配置但没解析出节点时,错误要指向订阅来源,而不是笼统地说"未配置"。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.cfg(
                proxy_mode="pool", proxy_pool_subscription_url="http://sub.test/list",
                proxy_pool_store_file=os.path.join(tmp, "proxy_pool.json"), proxy_pool_probe_interval_sec=0,
            )
            response = Mock(status_code=200, text="<html>not a proxy list</html>\n", headers={})
            with patch("proxy_pool.requests.get", return_value=response):
                manager = ProxyPoolManager(cfg)
                try:
                    error = manager.snapshot()["error"]
                    self.assertTrue(error.startswith("代理池没有可用节点: subscription: "), error)
                    self.assertIn("proxy URL is missing scheme or port", error)
                finally:
                    manager.shutdown()

    def test_subscription_public_only_rejects_loopback(self):
        cfg = self.cfg(
            proxy_mode="pool", proxy_pool_subscription_url="http://local.test/list",
            proxy_pool_subscription_public_only=True,
            proxy_pool_store_file=os.path.join(tempfile.gettempdir(), "missing-pool-store.json"),
        )
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):
            manager = ProxyPoolManager(cfg)
            try:
                self.assertIn("public-only", manager.snapshot()["error"])
                self.assertIn("public-only", manager.snapshot()["sources"]["subscription"]["error"])
                with self.assertRaises(ProxyPoolError):
                    manager.acquire("a", "w", 1, 1, "s", timeout=1)
            finally:
                manager.shutdown()

    def test_registration_path_preflight_is_non_destructive(self):
        manager = ProxyPoolManager(self.cfg(proxy_mode="single", proxy="http://127.0.0.1:8001", proxy_pool_probe_interval_sec=0))
        node_id = manager.snapshot()["nodes"][0]["id"]
        response = Mock(status_code=302, text="", headers={})
        with patch.object(manager._runtime, "acquire", return_value=("http://127.0.0.1:3128", "key")), patch.object(manager._runtime, "release") as release, patch("proxy_pool.requests.get", return_value=response) as get:
            result = manager.preflight_node(node_id)
        self.assertTrue(result["ok"])
        self.assertEqual(get.call_count, 2)
        release.assert_called_once_with("key")

    def test_retry_disposition_marks_only_pre_submit_stages_safe(self):
        self.assertEqual(registration_retry_disposition(STAGE_PAGE_OPEN), SAFE_NEW_LEASE)
        self.assertEqual(registration_retry_disposition(STAGE_PROFILE_SUBMIT), OUTCOME_UNCERTAIN)

    def test_snapshot_counts_attempts_and_registration_successes_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool_file = os.path.join(tmp, "proxies.txt")
            Path(pool_file).write_text("http://127.0.0.1:8001\n", encoding="utf-8")
            manager = ProxyPoolManager(
                self.cfg(
                    proxy_mode="pool",
                    proxy_pool_file=pool_file,
                    proxy_pool_probe_interval_sec=0,
                    proxy_pool_endpoint_mode="fixed",
                    proxy_pool_max_concurrent_per_node=1,
                )
            )
            try:
                self.assertEqual(manager.snapshot()["nodes"][0]["attempts"], 0)
                self.assertIsNone(manager.snapshot()["nodes"][0]["success_rate"])

                first = manager.acquire("a", "w", 1, 1, "s1", timeout=1)
                manager.report_success(first)
                manager.release(first)
                second = manager.acquire("a", "w", 2, 1, "s2", timeout=1)
                manager.report_transport_failure(second, ProxyTransportError("connection refused"))
                manager.release(second)

                node = manager.snapshot()["nodes"][0]
                self.assertEqual(node["attempts"], 2)
                self.assertEqual(node["registration_successes"], 1)
                self.assertEqual(node["success_rate"], 0.5)
            finally:
                manager.shutdown()

    def test_attempts_counter_survives_persisted_health_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool_file = os.path.join(tmp, "proxies.txt")
            state_file = os.path.join(tmp, "state.json")
            Path(pool_file).write_text("http://127.0.0.1:8001\n", encoding="utf-8")
            cfg = self.cfg(
                proxy_mode="pool",
                proxy_pool_file=pool_file,
                proxy_pool_probe_interval_sec=0,
                proxy_pool_endpoint_mode="fixed",
                proxy_pool_max_concurrent_per_node=1,
                proxy_pool_persist_health=True,
                proxy_pool_state_file=state_file,
            )
            manager = ProxyPoolManager(cfg)
            lease = manager.acquire("a", "w", 1, 1, "s1", timeout=1)
            manager.report_success(lease); manager.release(lease); manager.shutdown()

            restored = ProxyPoolManager(cfg)
            try:
                node = restored.snapshot()["nodes"][0]
                self.assertEqual(node["attempts"], 1)
                self.assertEqual(node["registration_successes"], 1)
            finally:
                restored.shutdown()

    def test_advanced_nodes_accept_canonical_and_node_id_references(self):
        """高级协议节点的 canonical 是 `proto://<sha256>`,UI 发的就是它,不能当 URI 解析。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.cfg(
                proxy_mode="pool", proxy_pool_store_file=os.path.join(tmp, "proxy_pool.json"),
                proxy_pool_probe_interval_sec=0, proxy_pool_endpoint_mode="fixed",
            )
            manager = ProxyPoolManager(cfg)
            try:
                uri = "vless://11111111-1111-1111-1111-111111111111@a.example.com:443?security=tls&sni=a.example.com#n"
                added = manager.add_node(uri)
                canonical = added["node"]["canonical"]
                node_id = added["node"]["id"]
                self.assertTrue(canonical.startswith("vless://"))

                for reference in (canonical, node_id):
                    manager.set_node_enabled(reference, False)
                    self.assertFalse(manager.snapshot()["nodes"][0]["enabled"])
                    manager.set_node_enabled(reference, True)
                    self.assertTrue(manager.snapshot()["nodes"][0]["enabled"])
                    manager.remove_node(reference)
                    self.assertEqual(manager.snapshot()["nodes"], [])
                    self.assertEqual(manager.snapshot()["store"]["total"], 0)
                    manager.add_node(uri)

                with self.assertRaises(ProxyPoolError):
                    manager.remove_node("vless://deadbeef")
            finally:
                manager.shutdown()

    def test_enabling_a_source_node_without_store_entry_only_restores_scheduling(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_file = os.path.join(tmp, "proxy_pool.json")
            pool_file = os.path.join(tmp, "proxies.txt")
            Path(pool_file).write_text("http://127.0.0.1:8001\n", encoding="utf-8")
            cfg = self.cfg(
                proxy_mode="pool", proxy_pool_file=pool_file, proxy_pool_store_file=store_file,
                proxy_pool_probe_interval_sec=0, proxy_pool_endpoint_mode="fixed",
            )
            manager = ProxyPoolManager(cfg)
            try:
                node = manager.snapshot()["nodes"][0]
                with manager._condition:            # 模拟运行时失败把来源节点禁用
                    manager._nodes[node["id"]].enabled = False
                self.assertFalse(manager.snapshot()["nodes"][0]["enabled"])
                result = manager.set_node_enabled(node["canonical"], True)
                self.assertTrue(result["enabled"])
                self.assertTrue(manager.snapshot()["nodes"][0]["enabled"])
                self.assertEqual(manager.snapshot()["store"]["total"], 0)   # 不写清单
            finally:
                manager.shutdown()

    def test_local_store_edits_sync_without_refetching_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_file = os.path.join(tmp, "proxy_pool.json")
            pool_file = os.path.join(tmp, "proxies.txt")
            Path(pool_file).write_text("http://127.0.0.1:8001\n", encoding="utf-8")
            cfg = self.cfg(
                proxy_mode="pool", proxy_pool_file=pool_file, proxy_pool_store_file=store_file,
                proxy_pool_refresh_interval_sec=900, proxy_pool_probe_interval_sec=0,
            )
            seed = ProxyPoolStore(store_file)
            seed.load()
            seed.add("http://127.0.0.1:8002")
            manager = ProxyPoolManager(cfg)
            try:
                self.assertEqual(
                    sorted(node["canonical"] for node in manager.snapshot()["nodes"]),
                    ["http://127.0.0.1:8001", "http://127.0.0.1:8002"],
                )

                # 手工/别的进程往清单里加节点:同步前调度里看不到
                editor = ProxyPoolStore(store_file)
                editor.load()
                editor.add("http://127.0.0.1:8003")
                self.assertEqual(len(manager.snapshot()["nodes"]), 2)

                # 同步本地来源(不重新拉订阅),立刻可见
                with patch.object(manager, "_fetch_subscription", side_effect=AssertionError("不应重新拉取订阅")):
                    self.assertTrue(manager.sync_local_sources())
                self.assertEqual(
                    sorted(node["canonical"] for node in manager.snapshot()["nodes"]),
                    ["http://127.0.0.1:8001", "http://127.0.0.1:8002", "http://127.0.0.1:8003"],
                )

                # 手工禁用同样即时生效
                editor2 = ProxyPoolStore(store_file)
                editor2.load()
                editor2.set_enabled("http://127.0.0.1:8002", False)
                self.assertTrue(manager.sync_local_sources())
                self.assertEqual(
                    sorted(node["canonical"] for node in manager.snapshot()["nodes"] if node["enabled"]),
                    ["http://127.0.0.1:8001", "http://127.0.0.1:8003"],
                )

                # 没有变化时不做多余工作
                self.assertFalse(manager.sync_local_sources())
            finally:
                manager.shutdown()

    def test_local_sync_retries_when_refresh_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_file = os.path.join(tmp, "proxy_pool.json")
            seed = ProxyPoolStore(store_file)
            seed.load()
            seed.add("http://127.0.0.1:8001")
            cfg = self.cfg(
                proxy_mode="pool", proxy_pool_store_file=store_file,
                proxy_pool_refresh_interval_sec=900, proxy_pool_probe_interval_sec=0,
            )
            manager = ProxyPoolManager(cfg)
            try:
                manager._refresh_lock.acquire()
                try:
                    editor = ProxyPoolStore(store_file)
                    editor.load()
                    editor.add("http://127.0.0.1:8002")
                    self.assertFalse(manager.sync_local_sources())    # 锁被占用,这次先不同步
                finally:
                    manager._refresh_lock.release()
                # 关键:改动不能被吞掉,下一次同步必须补上
                self.assertTrue(manager.sync_local_sources())
                self.assertEqual(
                    sorted(node["canonical"] for node in manager.snapshot()["nodes"]),
                    ["http://127.0.0.1:8001", "http://127.0.0.1:8002"],
                )
            finally:
                manager.shutdown()

    def test_store_nodes_are_pooled_immediately_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_file = os.path.join(tmp, "proxy_pool.json")
            cfg = self.cfg(
                proxy_mode="pool", proxy_pool_store_file=store_file,
                proxy_pool_probe_interval_sec=0, proxy_pool_endpoint_mode="fixed",
            )
            seed = ProxyPoolStore(store_file)
            seed.load()
            seed.add("http://127.0.0.1:8001")
            manager = ProxyPoolManager(cfg)
            try:
                # 构造 manager 时就要从 JSON 清单装载节点(重启后的正常路径)
                self.assertEqual([node["canonical"] for node in manager.snapshot()["nodes"]], ["http://127.0.0.1:8001"])
                added = manager.add_node("http://127.0.0.1:8001")
                self.assertEqual(added["node"]["source"], "manual")
                self.assertEqual(added["node"]["origin"], "user")
                self.assertTrue(os.path.isfile(store_file))

                # 添加即入池:不需要 reload,也不需要重建 manager
                lease = manager.acquire("a", "w", 1, 1, "s", timeout=1)
                self.assertEqual(lease.node_id, added["node"]["id"])
                manager.release(lease)

                # 不同写法解析为同一个节点,不会重复入池
                manager.add_node("HTTP://127.0.0.1:8001")
                self.assertEqual(len(manager.snapshot()["nodes"]), 1)

                # 禁用后立即不参与调度,但保留在清单里可恢复
                manager.set_node_enabled("http://127.0.0.1:8001", False)
                snapshot = manager.snapshot()
                self.assertFalse(snapshot["nodes"][0]["enabled"])
                self.assertEqual(snapshot["store"]["disabled"], 1)
                manager.set_node_enabled("http://127.0.0.1:8001", True)
                self.assertTrue(manager.snapshot()["nodes"][0]["enabled"])

                # 用户添加的节点:移除即从清单删除
                manager.remove_node("http://127.0.0.1:8001")
                self.assertEqual(manager.snapshot()["nodes"], [])

                # 文件源节点:移除写禁用覆盖,恢复后重新可用
                pool_file = os.path.join(tmp, "proxies.txt")
                Path(pool_file).write_text("http://127.0.0.1:8002\n", encoding="utf-8")
                manager.config["proxy_pool_file"] = pool_file
                manager.reload_sources(force=True)
                self.assertEqual([node["canonical"] for node in manager.snapshot()["nodes"]], ["http://127.0.0.1:8002"])
                manager.remove_node("http://127.0.0.1:8002")
                disabled = manager.snapshot()["nodes"]
                self.assertEqual([node["canonical"] for node in disabled], ["http://127.0.0.1:8002"])
                self.assertFalse(disabled[0]["enabled"])
                manager.set_node_enabled("http://127.0.0.1:8002", True)
                self.assertEqual([node["canonical"] for node in manager.snapshot()["nodes"]], ["http://127.0.0.1:8002"])
                self.assertTrue(manager.snapshot()["nodes"][0]["enabled"])
            finally:
                manager.shutdown()

            restored = ProxyPoolManager(dict(cfg, proxy_pool_file=os.path.join(tmp, "proxies.txt")))
            try:
                nodes = restored.snapshot()["nodes"]
                self.assertEqual([node["canonical"] for node in nodes], ["http://127.0.0.1:8002"])
                self.assertTrue(nodes[0]["enabled"])
            finally:
                restored.shutdown()

    def test_disabling_every_node_surfaces_an_error_and_still_allows_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.cfg(
                proxy_mode="pool",
                proxy_pool_store_file=os.path.join(tmp, "proxy_pool.json"),
                proxy_pool_manual_entries=["http://127.0.0.1:8001"],
                proxy_pool_disabled_nodes=["http://127.0.0.1:8001"],
                proxy_pool_probe_interval_sec=0,
            )
            manager = ProxyPoolManager(cfg)
            try:
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["nodes"], [])
                self.assertIn("代理池", snapshot["error"])
                # 旧配置键过滤掉的节点必须能在快照里看到,否则节点"凭空消失"无从排查
                self.assertEqual(snapshot["disabled_legacy"], ["http://127.0.0.1:8001"])
                with self.assertRaises(ProxyPoolError):
                    manager.acquire("a", "w", 1, 1, "s", timeout=1)

                # 空池 / 全部禁用不再是死局:添加节点后立即恢复可用
                manager.add_node("http://127.0.0.1:8002")
                self.assertEqual(manager.snapshot()["error"], "")
                self.assertEqual([node["canonical"] for node in manager.snapshot()["nodes"]], ["http://127.0.0.1:8002"])
                lease = manager.acquire("a", "w", 1, 1, "s", timeout=1)
                self.assertEqual(lease.source_uri, "http://127.0.0.1:8002")
                manager.release(lease)
            finally:
                manager.shutdown()

    def test_config_validation_accepts_proxy_lists_and_rejects_bad_shapes(self):
        from app_config import ConfigError, validate_config_structure

        cfg = validate_config_structure({
            **dict(DEFAULT_CONFIG),
            "proxy_pool_manual_entries": ["  http://127.0.0.1:8001  ", "", "vless://uuid@host:443"],
            "proxy_pool_disabled_nodes": ["http://127.0.0.1:8002"],
        })
        self.assertEqual(cfg["proxy_pool_manual_entries"], ["http://127.0.0.1:8001", "vless://uuid@host:443"])
        self.assertEqual(cfg["proxy_pool_disabled_nodes"], ["http://127.0.0.1:8002"])

        with self.assertRaises(ConfigError):
            validate_config_structure({**dict(DEFAULT_CONFIG), "proxy_pool_manual_entries": "http://127.0.0.1:8001"})
        with self.assertRaises(ConfigError):
            validate_config_structure({**dict(DEFAULT_CONFIG), "proxy_pool_disabled_nodes": [123]})

        from app_config import validate_run_requirements

        manual_only = validate_run_requirements({
            **dict(DEFAULT_CONFIG),
            "proxy_mode": "pool",
            "proxy_pool_manual_entries": ["http://127.0.0.1:8001"],
            "email_provider": "yyds",
            "yyds_api_key": "k",
        })
        self.assertEqual(manual_only["proxy_mode"], "pool")
        # 节点清单 JSON 同样是合法来源:静态校验不再要求 file/subscription/manual。
        store_only = validate_run_requirements({
            **dict(DEFAULT_CONFIG),
            "proxy_mode": "pool",
            "email_provider": "yyds",
            "yyds_api_key": "k",
        })
        self.assertEqual(store_only["proxy_mode"], "pool")
        self.assertEqual(store_only["proxy_pool_store_file"], "./proxy_pool.json")
        self.assertEqual(
            validate_config_structure({**dict(DEFAULT_CONFIG), "proxy_pool_store_file": "  ./pool.json  "})["proxy_pool_store_file"],
            "./pool.json",
        )

    def _ops(self, failure_stage=None, state=None):
        state = state or {"profile_calls": 0, "page_calls": 0}
        def page():
            state["page_calls"] += 1
            if failure_stage == "page" and state["page_calls"] == 1:
                raise ProxyTransportError("connection refused")
        def profile():
            state["profile_calls"] += 1
            if failure_stage == "profile":
                raise ProxyTransportError("connection reset")
            return {"given_name": "A", "family_name": "B", "password": "pw"}
        return RegistrationOperations(
            start_browser=lambda: None, restart_browser=lambda: None, browser_missing=lambda: False,
            open_signup_page=page, fill_email_and_submit=lambda: ("a@example.com", "token"),
            save_mail_credential=lambda *_: True, fill_code_and_submit=lambda *_: "123456",
            fill_profile_and_submit=profile, wait_for_sso_cookie=lambda: "sso", enable_nsfw=lambda _: (True, "ok"),
            persist_account_line=lambda *_: None, queue_unsaved_result=lambda *_: True, add_tokens=lambda *_: {},
            export_cpa=lambda *_: {"ok": True, "skipped": False}, cleanup=lambda _: None, sleep=lambda _: None,
            cancelled_exception=Cancelled, retry_exception=RetryNeeded,
        )

    def test_profile_transport_failure_is_not_replayed_with_new_lease(self):
        callbacks = RegistrationCallbacks(log=lambda _: None, cancelled=lambda: False)
        begins = []
        with patch.dict(registration_flow.app_config, {"proxy_mode": "single"}, clear=False), patch("registration_flow.begin_registration_slot", side_effect=lambda **kw: begins.append(kw)), patch("registration_flow.end_registration_slot"), patch("registration_flow.current_proxy_lease", return_value=object()):
            result = run_batch(1, callbacks, lambda *_: None, self._ops("profile"), enable_nsfw=False)
        self.assertEqual(len(begins), 1)
        self.assertEqual(result.processed_count, 1)
        self.assertEqual(result.uncertain_count, 1)

    def test_page_transport_failure_can_retry_with_new_lease(self):
        callbacks = RegistrationCallbacks(log=lambda _: None, cancelled=lambda: False)
        begins = []; state = {"profile_calls": 0, "page_calls": 0}
        with patch.dict(registration_flow.app_config, {"proxy_mode": "single"}, clear=False), patch("registration_flow.begin_registration_slot", side_effect=lambda **kw: begins.append(kw)), patch("registration_flow.end_registration_slot"), patch("registration_flow.current_proxy_lease", return_value=object()):
            result = run_batch(1, callbacks, lambda *_: None, self._ops("page", state), enable_nsfw=False, max_slot_retry=2)
        self.assertEqual(len(begins), 2)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.uncertain_count, 0)


if __name__ == "__main__":
    unittest.main()
