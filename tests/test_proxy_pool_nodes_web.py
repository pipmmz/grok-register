"""代理池节点增删改接口测试:添加的节点必须立刻入池、落盘并可恢复。

engine.load_config / save_config 都被替换掉,测试绝不读写仓库里的 config.json。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "web dependencies not installed")
class ProxyPoolNodesWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import server
        cls.server = server
        cls.client = TestClient(server.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_file = os.path.join(self.tmp.name, "proxy_pool.json")
        self.pool_file = os.path.join(self.tmp.name, "proxies.txt")
        self.saved = []
        self.cfg = {
            **dict(self.server.engine.DEFAULT_CONFIG),
            "proxy_mode": "auto", "proxy": "", "proxy_pool_store_file": self.store_file,
        }
        with self.server._job_lock:
            self.server._job_state["running"] = False
        self._patches = [
            patch.object(self.server.engine, "load_config", side_effect=self._load_config),
            patch.object(self.server.engine, "save_config", side_effect=self._save_config),
        ]
        for item in self._patches:
            item.start()
        self.server.engine.config.clear()
        self.server.engine.config.update(self.cfg)
        self._reset_manager()

    def tearDown(self):
        for item in self._patches:
            item.stop()
        self._reset_manager()
        self.server.engine.config.clear()
        self.server.engine.config.update(self.server.engine.DEFAULT_CONFIG)
        self.tmp.cleanup()

    def _reset_manager(self):
        try:
            import proxy_pool
            proxy_pool.reset_manager()
        except Exception:
            pass

    def _load_config(self):
        self.server.engine.config.clear()
        self.server.engine.config.update(self.cfg)
        return self.server.engine.config

    def _save_config(self):
        self.cfg = dict(self.server.engine.config)
        self.saved.append(dict(self.cfg))
        return self.server.engine.config

    def _apply_config(self, updates):
        self.cfg.update(updates)
        self.server.engine.config.clear()
        self.server.engine.config.update(self.cfg)

    def _store_payload(self):
        return json.loads(Path(self.store_file).read_text(encoding="utf-8"))

    def test_add_node_switches_mode_persists_and_pools_immediately(self):
        # 默认是 auto 模式:添加代理必须真的生效,而不是写进配置就结束。
        self.assertEqual(self.cfg["proxy_mode"], "auto")
        response = self.client.post("/api/proxy-pool/nodes", json={"uri": "http://127.0.0.1:8001"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["notice"], "代理模式已切换为 pool")
        self.assertEqual(payload["added"]["canonical"], "http://127.0.0.1:8001")
        self.assertEqual([node["canonical"] for node in payload["nodes"]], ["http://127.0.0.1:8001"])
        self.assertEqual(payload["added"]["origin"], "user")
        self.assertEqual(self.saved[-1]["proxy_mode"], "pool")
        stored = self._store_payload()["nodes"]["http://127.0.0.1:8001"]
        self.assertEqual(stored["origin"], "user")
        self.assertNotIn("enabled", stored, "清单里不再保存启用/禁用状态")

        # 不同写法是同一个节点,不会重复入池
        again = self.client.post("/api/proxy-pool/nodes", json={"uri": "HTTP://127.0.0.1:8001"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(again.json()["nodes"]), 1)

        # 重启(新 Manager)后仍然从 JSON 清单恢复该节点
        self._reset_manager()
        status = self.client.get("/api/proxy-pool/status").json()
        self.assertEqual([node["canonical"] for node in status["nodes"]], ["http://127.0.0.1:8001"])
        self.assertEqual(status["store"]["user_nodes"], 1)

    def test_add_node_rejects_invalid_and_missing_uri(self):
        bad = self.client.post("/api/proxy-pool/nodes", json={"uri": "not-a-proxy"})
        self.assertEqual(bad.status_code, 400)
        empty = self.client.post("/api/proxy-pool/nodes", json={"uri": "  "})
        self.assertEqual(empty.status_code, 400)
        self.assertFalse(os.path.exists(self.store_file))

    def test_advanced_node_can_be_removed_by_returned_canonical(self):
        """WebUI 表格的移除按钮发的是 canonical,高级协议节点的 canonical 不是可解析 URI。"""
        uri = "vless://11111111-1111-1111-1111-111111111111@a.example.com:443?security=tls&sni=a.example.com#n"
        added = self.client.post("/api/proxy-pool/nodes", json={"uri": uri})
        self.assertEqual(added.status_code, 200)
        canonical = added.json()["added"]["canonical"]
        self.assertTrue(canonical.startswith("vless://"))

        removed = self.client.delete("/api/proxy-pool/nodes", params={"canonical": canonical})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["nodes"], [])
        self.assertEqual(removed.json()["store"]["total"], 0)

    def test_removed_node_is_gone_for_good(self):
        """移除即删除:池里没有、清单里也不留条目,再查状态也不会回来。"""
        self.client.post("/api/proxy-pool/nodes", json={"uri": "http://127.0.0.1:8001"})
        removed = self.client.delete("/api/proxy-pool/nodes?canonical=http%3A%2F%2F127.0.0.1%3A8001")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["nodes"], [])
        self.assertEqual(self._store_payload()["nodes"], {})

        status = self.client.get("/api/proxy-pool/status").json()
        self.assertEqual(status["nodes"], [])
        self.assertEqual(status["store"]["total"], 0)

    def test_removed_file_source_node_is_not_readded(self):
        """文件来源的节点移除后是彻底删除:清单不留条目,重新加载来源也不回来。"""
        Path(self.pool_file).write_text("http://127.0.0.1:8002\nhttp://127.0.0.1:8003\n", encoding="utf-8")
        self._apply_config({"proxy_mode": "pool", "proxy_pool_file": self.pool_file})
        self._reset_manager()

        status = self.client.get("/api/proxy-pool/status").json()
        self.assertEqual(sorted(node["canonical"] for node in status["nodes"]), ["http://127.0.0.1:8002", "http://127.0.0.1:8003"])

        removed = self.client.delete("/api/proxy-pool/nodes?canonical=http%3A%2F%2F127.0.0.1%3A8002")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual([node["canonical"] for node in removed.json()["nodes"]], ["http://127.0.0.1:8003"])
        self.assertEqual(self._store_payload()["nodes"], {})   # 不留禁用条目
        self.assertEqual(
            [node["canonical"] for node in self.client.get("/api/proxy-pool/status").json()["nodes"]],
            ["http://127.0.0.1:8003"],
        )

    def test_reload_switches_auto_mode_to_pool_and_loads_subscription(self):
        """订阅已配置但模式不是 pool 时,重新加载必须真的加载节点,而不是静默空操作。"""
        self._apply_config({"proxy_pool_subscription_url": "http://sub.test/list"})
        self._reset_manager()
        response = Mock(status_code=200, text="http://1.2.3.4:8080\nsocks5://5.6.7.8:1080\n", headers={})
        with patch("proxy_pool.requests.get", return_value=response):
            reloaded = self.client.post("/api/proxy-pool/reload")
        self.assertEqual(reloaded.status_code, 200)
        payload = reloaded.json()
        self.assertEqual(payload["notice"], "代理模式已切换为 pool")
        self.assertEqual(payload["mode"], "pool")
        self.assertEqual(
            sorted(node["canonical"] for node in payload["nodes"]),
            ["http://1.2.3.4:8080", "socks5://5.6.7.8:1080"],
        )
        self.assertEqual(payload["sources"]["subscription"]["supported"], 2)
        self.assertEqual(self.saved[-1]["proxy_mode"], "pool")

    def test_reload_without_any_source_reports_instead_of_silent_noop(self):
        response = self.client.post("/api/proxy-pool/reload")
        self.assertEqual(response.status_code, 400)
        self.assertIn("未配置任何来源", response.json()["detail"])

    def test_reload_reports_when_a_lease_blocks_the_config_change(self):
        """有租约占用时无法重建 Manager:重新加载要明确报错,不能静默沿用旧配置。"""
        import proxy_pool
        self.client.post("/api/proxy-pool/nodes", json={"uri": "http://127.0.0.1:8001"})
        manager = proxy_pool.get_manager()
        lease = manager.acquire("a", "w", 1, 1, "s", timeout=1)
        try:
            self._apply_config({"proxy_pool_subscription_url": "http://sub.test/list"})
            response = self.client.post("/api/proxy-pool/reload")
            self.assertEqual(response.status_code, 409)
            self.assertIn("租约", response.json()["detail"])
        finally:
            manager.release(lease)

    def test_node_endpoints_are_rejected_while_a_job_runs(self):
        with self.server._job_lock:
            self.server._job_state["running"] = True
        try:
            self.assertEqual(self.client.post("/api/proxy-pool/nodes", json={"uri": "http://127.0.0.1:8001"}).status_code, 409)
            self.assertEqual(self.client.delete("/api/proxy-pool/nodes?canonical=http://127.0.0.1:8001").status_code, 409)
        finally:
            with self.server._job_lock:
                self.server._job_state["running"] = False


if __name__ == "__main__":
    unittest.main()
