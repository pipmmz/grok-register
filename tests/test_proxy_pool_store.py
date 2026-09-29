"""代理池 JSON 节点清单的单元测试。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from proxy_pool_store import (
    ORIGIN_USER,
    ProxyPoolStore,
    ProxyPoolStoreError,
)


class ProxyPoolStoreTests(unittest.TestCase):
    def _store(self, tmp, name="proxy_pool.json"):
        return ProxyPoolStore(os.path.join(tmp, name))

    def test_add_persists_nodes_and_rejects_invalid_uris(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.load()
            self.assertFalse(os.path.exists(store.path))          # 只读不会写文件

            node = store.add("http://user:pass@127.0.0.1:7890")
            self.assertTrue(os.path.isfile(store.path))
            self.assertEqual(node.origin, ORIGIN_USER)

            payload = json.loads(Path(store.path).read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(list(payload["nodes"]), ["http://user:pass@127.0.0.1:7890"])
            self.assertEqual(payload["nodes"]["http://user:pass@127.0.0.1:7890"]["origin"], ORIGIN_USER)
            self.assertNotIn("enabled", payload["nodes"]["http://user:pass@127.0.0.1:7890"], "清单里不再有启用/禁用状态")

            with self.assertRaises(ProxyPoolStoreError):
                store.add("   ")
            with self.assertRaises(ProxyPoolStoreError):
                store.add("not-a-proxy")
            self.assertEqual(len(store.entries()), 1)

    def test_canonical_identity_dedupes_and_readding_clears_the_removal_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.load()
            store.add("http://127.0.0.1:8001")
            store.remove("http://127.0.0.1:8001")
            store.mark_removed("http://127.0.0.1:8001")
            self.assertEqual(store.entries(), [])
            self.assertEqual(store.removed_canonicals(), {"http://127.0.0.1:8001"})

            again = store.add("HTTP://127.0.0.1:8001")
            self.assertEqual(again.canonical, "http://127.0.0.1:8001")
            self.assertEqual(len(store.entries()), 1)
            self.assertEqual(store.removed_canonicals(), set(), "重新添加同一个节点等于撤销删除记录")

    def test_remove_deletes_user_entries_and_ignores_source_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.load()
            store.add("http://127.0.0.1:8001")
            # 文件/订阅来源的节点不归存储所有:既没有条目,也不会留下任何"禁用"状态。
            self.assertIsNone(store.remove("socks5://127.0.0.1:1080"))
            self.assertEqual(store.removed_canonicals(), set())
            self.assertEqual([node.canonical for node in store.entries()], ["http://127.0.0.1:8001"])

            removed = store.remove("http://127.0.0.1:8001")
            self.assertEqual(removed.origin, ORIGIN_USER)
            self.assertEqual(store.entries(), [])
            self.assertIsNone(store.remove("http://127.0.0.1:8001"))
            payload = json.loads(Path(store.path).read_text(encoding="utf-8"))
            self.assertEqual(payload["nodes"], {})

    def test_legacy_config_lists_migrate_in_memory_then_persist_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.load(
                legacy_manual=["http://127.0.0.1:8001", "socks5://127.0.0.1:1080"],
                legacy_disabled=["http://127.0.0.1:8001"],
            )
            # 旧键里的"屏蔽"在新语义下等价于"已移除":不进入清单,但留下删除记录。
            self.assertEqual([node.canonical for node in store.entries()], ["socks5://127.0.0.1:1080"])
            self.assertEqual(store.removed_canonicals(), {"http://127.0.0.1:8001"})
            self.assertFalse(os.path.exists(store.path))          # 迁移本身不落盘

            store.add("http://127.0.0.1:8002")
            payload = json.loads(Path(store.path).read_text(encoding="utf-8"))
            self.assertEqual(sorted(payload["nodes"]), ["http://127.0.0.1:8002", "socks5://127.0.0.1:1080"])
            self.assertEqual(sorted(payload["removed"]), ["http://127.0.0.1:8001"])

    def test_existing_file_wins_over_legacy_lists_and_external_edits_are_picked_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proxy_pool.json")
            store = ProxyPoolStore(path)
            store.load(legacy_manual=["http://127.0.0.1:8001"])
            store.add("http://127.0.0.1:8002")

            reopened = ProxyPoolStore(path)
            reopened.load(legacy_manual=["http://127.0.0.1:9999"])
            # 首次写入时把迁移进来的节点一起落盘;旧键里新增的 9999 不再生效。
            self.assertEqual([node.canonical for node in reopened.entries()], ["http://127.0.0.1:8001", "http://127.0.0.1:8002"])

            Path(path).write_text(json.dumps({
                "version": 1,
                "nodes": {"http://127.0.0.1:8003": {"uri": "http://127.0.0.1:8003"}},
            }), encoding="utf-8")
            self.assertEqual([node.canonical for node in reopened.entries()], ["http://127.0.0.1:8003"])

    def test_legacy_disabled_entries_in_the_file_become_removal_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proxy_pool.json")
            Path(path).write_text(json.dumps({
                "version": 1,
                "nodes": {"http://127.0.0.1:8001": {"uri": "http://127.0.0.1:8001", "enabled": False}},
            }), encoding="utf-8")
            store = ProxyPoolStore(path)
            store.load()
            self.assertEqual(store.entries(), [])
            self.assertEqual(store.removed_canonicals(), {"http://127.0.0.1:8001"})

    def test_corrupt_store_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proxy_pool.json")
            Path(path).write_text("{not json", encoding="utf-8")
            store = ProxyPoolStore(path)
            with self.assertRaises(ProxyPoolStoreError):
                store.load()

    def test_unparsable_entries_are_skipped_without_losing_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proxy_pool.json")
            Path(path).write_text(json.dumps({
                "version": 1,
                "nodes": {
                    "http://127.0.0.1:8001": {"uri": "http://127.0.0.1:8001", "enabled": True},
                    "broken": {"uri": "not-a-proxy", "enabled": True},
                },
            }), encoding="utf-8")
            store = ProxyPoolStore(path)
            store.load()
            self.assertEqual([node.canonical for node in store.entries()], ["http://127.0.0.1:8001"])


if __name__ == "__main__":
    unittest.main()
