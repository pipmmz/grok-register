"""代理池节点增删改接口测试:添加的节点必须立刻入池、落盘并可恢复。

engine.load_config / save_config 都被替换掉,测试绝不读写仓库里的 config.json。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertTrue(payload["added"]["enabled"])
        self.assertEqual(self.saved[-1]["proxy_mode"], "pool")
        self.assertEqual(self._store_payload()["nodes"]["http://127.0.0.1:8001"]["enabled"], True)

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

        disabled = self.client.post("/api/proxy-pool/nodes/enabled", json={"canonical": canonical, "enabled": False})
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["nodes"][0]["enabled"])

        restored = self.client.post("/api/proxy-pool/nodes/enabled", json={"canonical": canonical, "enabled": True})
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()["nodes"][0]["enabled"])

        removed = self.client.delete("/api/proxy-pool/nodes", params={"canonical": canonical})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["nodes"], [])
        self.assertEqual(removed.json()["store"]["total"], 0)

    def test_remove_and_restore_node_round_trip(self):
        self.client.post("/api/proxy-pool/nodes", json={"uri": "http://127.0.0.1:8001"})
        removed = self.client.delete("/api/proxy-pool/nodes?canonical=http%3A%2F%2F127.0.0.1%3A8001")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["nodes"], [])
        self.assertEqual(self._store_payload()["nodes"], {})

        self.client.post("/api/proxy-pool/nodes", json={"uri": "http://127.0.0.1:8001"})
        disabled = self.client.post(
            "/api/proxy-pool/nodes/enabled", json={"canonical": "http://127.0.0.1:8001", "enabled": False}
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["nodes"][0]["enabled"])
        self.assertEqual(disabled.json()["store"]["disabled"], 1)
        # WebUI 的"已移除"chips 依赖清单条目列表
        entries = {item["canonical"]: item for item in disabled.json()["store"]["nodes"]}
        self.assertEqual(entries["http://127.0.0.1:8001"]["uri"], "http://127.0.0.1:8001")
        self.assertFalse(entries["http://127.0.0.1:8001"]["enabled"])

        restored = self.client.post(
            "/api/proxy-pool/nodes/enabled", json={"canonical": "http://127.0.0.1:8001", "enabled": True}
        )
        self.assertTrue(restored.json()["nodes"][0]["enabled"])

    def test_file_source_node_can_be_disabled_and_restored(self):
        Path(self.pool_file).write_text("http://127.0.0.1:8002\n", encoding="utf-8")
        self._apply_config({"proxy_mode": "pool", "proxy_pool_file": self.pool_file})
        self._reset_manager()

        status = self.client.get("/api/proxy-pool/status").json()
        self.assertEqual([node["canonical"] for node in status["nodes"]], ["http://127.0.0.1:8002"])

        removed = self.client.delete("/api/proxy-pool/nodes?canonical=http%3A%2F%2F127.0.0.1%3A8002")
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(removed.json()["nodes"][0]["enabled"])
        self.assertEqual(self._store_payload()["nodes"]["http://127.0.0.1:8002"]["origin"], "source")

        restored = self.client.post(
            "/api/proxy-pool/nodes/enabled", json={"canonical": "http://127.0.0.1:8002", "enabled": True}
        )
        self.assertTrue(restored.json()["nodes"][0]["enabled"])
        # 恢复后不再需要覆盖记录
        self.assertEqual(self._store_payload()["nodes"], {})

    def test_node_endpoints_are_rejected_while_a_job_runs(self):
        with self.server._job_lock:
            self.server._job_state["running"] = True
        try:
            self.assertEqual(self.client.post("/api/proxy-pool/nodes", json={"uri": "http://127.0.0.1:8001"}).status_code, 409)
            self.assertEqual(self.client.delete("/api/proxy-pool/nodes?canonical=http://127.0.0.1:8001").status_code, 409)
            self.assertEqual(
                self.client.post("/api/proxy-pool/nodes/enabled", json={"canonical": "http://127.0.0.1:8001"}).status_code, 409
            )
        finally:
            with self.server._job_lock:
                self.server._job_state["running"] = False


if __name__ == "__main__":
    unittest.main()
