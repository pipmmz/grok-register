"""代理池节点存储:把用户维护的节点清单持久化为 JSON。

存储只负责"节点清单"(用户添加的节点 + 对文件/订阅节点的启用/禁用覆盖),
健康计数仍然由 ``proxy_pool_state_file`` 单独保存。两者都是 JSON,但职责分离:

- ``proxy_pool.json``     : 用户数据,可手工编辑,改动立即生效。
- ``proxy_pool_state.json``: 运行期缓存,由池子自动写入。

文件格式::

    {
      "version": 1,
      "updated_at": 1759...,
      "nodes": {
        "<canonical_uri>": {
          "uri": "http://user:pass@127.0.0.1:7890",
          "canonical": "http://user:pass@127.0.0.1:7890",
          "node_id": "9f2c...",
          "enabled": true,
          "origin": "user",          // user = 用户添加; source = 对文件/订阅节点的覆盖
          "added_at": 1759...,
          "note": ""
        }
      }
    }

节点身份使用 ``canonical_uri``(解析器规范化后的路由 URL),所以
``HTTP://127.0.0.1:8001`` 与 ``http://127.0.0.1:7890`` 不会变成两个节点。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from proxy_protocols import ProxyProtocolError, parse_proxy_line

_ROOT = Path(__file__).resolve().parent
DEFAULT_STORE_FILE = "./proxy_pool.json"
STORE_VERSION = 1
MAX_STORE_NODES = 10000

ORIGIN_USER = "user"
ORIGIN_SOURCE = "source"


class ProxyPoolStoreError(RuntimeError):
    """节点存储读写或校验失败。"""


@dataclass
class StoredNode:
    uri: str
    canonical: str
    node_id: str
    enabled: bool = True
    origin: str = ORIGIN_USER
    added_at: float = 0.0
    note: str = ""

    def as_dict(self) -> Dict:
        return {
            "uri": self.uri,
            "canonical": self.canonical,
            "node_id": self.node_id,
            "enabled": bool(self.enabled),
            "origin": self.origin,
            "added_at": self.added_at,
            "note": self.note,
        }


def resolve_store_path(value: str) -> str:
    path = os.path.expanduser(str(value or "").strip() or DEFAULT_STORE_FILE)
    return path if os.path.isabs(path) else str(_ROOT / path)


def describe_uri(uri: str) -> StoredNode:
    """解析一个代理 URI,返回可入库的节点描述(不落盘)。"""
    raw = str(uri or "").strip()
    if not raw:
        raise ProxyPoolStoreError("代理地址不能为空")
    if len(raw) > 4096:
        raise ProxyPoolStoreError("代理地址超过 4096 字符限制")
    try:
        descriptor = parse_proxy_line(raw)
    except ProxyProtocolError as exc:
        raise ProxyPoolStoreError(str(exc)) from exc
    return StoredNode(uri=raw, canonical=descriptor.canonical_uri, node_id=descriptor.node_id)


class ProxyPoolStore:
    """JSON 节点清单;所有写操作都是原子替换。"""

    def __init__(self, path: str = DEFAULT_STORE_FILE, log=None):
        self.path = resolve_store_path(path)
        self.log = log or (lambda message: None)
        self._lock = threading.RLock()
        self._nodes: Dict[str, StoredNode] = {}
        self._stamp: Optional[tuple] = None
        self._loaded = False

    # ---------------------------------------------------------------- 读
    def stamp(self) -> Optional[tuple]:
        """文件当前的时间戳标记(不存在时为 None),用于变更检测。"""
        with self._lock:
            return self._stamp_of()

    def _stamp_of(self) -> Optional[tuple]:
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProxyPoolStoreError("代理池文件不可读: %s: %s" % (self.path, exc)) from exc
        return (stat.st_mtime_ns, stat.st_size)

    def load(self, legacy_manual=None, legacy_disabled=None) -> "ProxyPoolStore":
        """读取节点清单;文件不存在时用旧配置键做一次性迁移。

        迁移只在内存里完成,磁盘文件在第一次增删改(用户真的动过节点)时才创建:
        只读场景不会因为默认路径就写出一个文件来。
        """
        with self._lock:
            stamp = self._stamp_of()
            if stamp is not None:
                self._read_file(stamp)
                self._loaded = True
                return self
            nodes: Dict[str, StoredNode] = {}
            migrated = self._migrate_legacy(nodes, legacy_manual, legacy_disabled)
            self._nodes = nodes
            self._stamp = None
            self._loaded = True
            if migrated:
                self.log("[*] 代理池已载入 %s 个旧配置节点(首次增删改时写入 %s)" % (len(nodes), self.path))
            return self

    def _read_file(self, stamp: tuple) -> None:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ProxyPoolStoreError("代理池文件不是合法 JSON: %s: %s" % (self.path, exc)) from exc
        except OSError as exc:
            raise ProxyPoolStoreError("代理池文件读取失败: %s: %s" % (self.path, exc)) from exc
        raw_nodes = payload.get("nodes") if isinstance(payload, dict) else None
        if raw_nodes is None:
            raw_nodes = {}
        if not isinstance(raw_nodes, dict):
            raise ProxyPoolStoreError("代理池文件 nodes 字段必须是对象: %s" % self.path)
        nodes: Dict[str, StoredNode] = {}
        for key, value in raw_nodes.items():
            if not isinstance(value, dict):
                continue
            uri = str(value.get("uri") or "").strip()
            if not uri:
                continue
            try:
                node = describe_uri(uri)
            except ProxyPoolStoreError as exc:
                self.log("[!] 代理池文件跳过无法解析的节点 %s: %s" % (key, exc))
                continue
            node.enabled = bool(value.get("enabled", True))
            origin = str(value.get("origin") or ORIGIN_USER).strip().lower()
            node.origin = origin if origin in (ORIGIN_USER, ORIGIN_SOURCE) else ORIGIN_USER
            try:
                node.added_at = float(value.get("added_at") or 0.0)
            except (TypeError, ValueError):
                node.added_at = 0.0
            node.note = str(value.get("note") or "")
            nodes[node.canonical] = node
        self._nodes = nodes
        self._stamp = stamp

    def _migrate_legacy(self, nodes: Dict[str, StoredNode], legacy_manual, legacy_disabled) -> bool:
        now = time.time()
        changed = False
        for item in list(legacy_manual or []):
            try:
                node = describe_uri(str(item))
            except ProxyPoolStoreError as exc:
                self.log("[!] 旧配置 proxy_pool_manual_entries 跳过无法解析的条目 %r: %s" % (item, exc))
                continue
            if node.canonical in nodes:
                continue
            node.origin, node.added_at = ORIGIN_USER, now
            nodes[node.canonical] = node
            changed = True
        for item in list(legacy_disabled or []):
            try:
                node = describe_uri(str(item))
            except ProxyPoolStoreError as exc:
                self.log("[!] 旧配置 proxy_pool_disabled_nodes 跳过无法解析的条目 %r: %s" % (item, exc))
                continue
            existing = nodes.get(node.canonical)
            if existing is not None:
                if existing.enabled:
                    existing.enabled = False
                    changed = True
                continue
            node.enabled, node.origin, node.added_at = False, ORIGIN_SOURCE, now
            nodes[node.canonical] = node
            changed = True
        return changed

    def reload_if_changed(self) -> bool:
        """文件被外部(手工编辑 / 另一个进程)改动时重新载入。"""
        with self._lock:
            stamp = self._stamp_of()
            if stamp == self._stamp:
                return False
            if stamp is None:
                self._nodes = {}
                self._stamp = None
                return True
            self._read_file(stamp)
            return True

    def entries(self) -> List[StoredNode]:
        with self._lock:
            self.reload_if_changed()
            return [StoredNode(**node.as_dict()) for node in self._nodes.values()]

    def get(self, uri: str) -> Optional[StoredNode]:
        with self._lock:
            self.reload_if_changed()
            try:
                canonical = describe_uri(uri).canonical
            except ProxyPoolStoreError:
                canonical = str(uri or "").strip()
            node = self._nodes.get(canonical)
            return StoredNode(**node.as_dict()) if node is not None else None

    def disabled_canonicals(self) -> set:
        with self._lock:
            self.reload_if_changed()
            return {node.canonical for node in self._nodes.values() if not node.enabled}

    def enabled_user_entries(self) -> List[StoredNode]:
        with self._lock:
            self.reload_if_changed()
            return [
                StoredNode(**node.as_dict())
                for node in self._nodes.values()
                if node.enabled and node.origin == ORIGIN_USER
            ]

    # ---------------------------------------------------------------- 写
    def add(self, uri: str, enabled: bool = True, origin: str = ORIGIN_USER, note: str = "") -> StoredNode:
        """新增或重新启用一个节点;重复添加不会产生第二个节点。"""
        candidate = describe_uri(uri)
        with self._lock:
            self.reload_if_changed()
            existing = self._nodes.get(candidate.canonical)
            if existing is None and len(self._nodes) >= MAX_STORE_NODES:
                raise ProxyPoolStoreError("代理池节点数量超过 %s 上限" % MAX_STORE_NODES)
            now = time.time()
            if existing is None:
                candidate.enabled = bool(enabled)
                candidate.origin = origin if origin in (ORIGIN_USER, ORIGIN_SOURCE) else ORIGIN_USER
                candidate.added_at = now
                candidate.note = note
                self._nodes[candidate.canonical] = candidate
            else:
                existing.uri = candidate.uri
                existing.node_id = candidate.node_id
                existing.enabled = bool(enabled)
                existing.origin = origin if origin in (ORIGIN_USER, ORIGIN_SOURCE) else existing.origin
                if note:
                    existing.note = note
                candidate = existing
            self._write()
            return StoredNode(**candidate.as_dict())

    def set_enabled(self, uri: str, enabled: bool) -> StoredNode:
        """启用/禁用一个节点;文件/订阅节点会写入一条覆盖记录。"""
        candidate = describe_uri(uri)
        with self._lock:
            self.reload_if_changed()
            existing = self._nodes.get(candidate.canonical)
            if existing is None:
                if enabled:
                    raise ProxyPoolStoreError("节点不在代理池中: %s" % candidate.canonical)
                if len(self._nodes) >= MAX_STORE_NODES:
                    raise ProxyPoolStoreError("代理池节点数量超过 %s 上限" % MAX_STORE_NODES)
                candidate.enabled, candidate.origin, candidate.added_at = False, ORIGIN_SOURCE, time.time()
                self._nodes[candidate.canonical] = candidate
            else:
                existing.enabled = bool(enabled)
                candidate = existing
                if enabled and existing.origin == ORIGIN_SOURCE:
                    # 来源节点的默认状态就是启用,恢复后不再需要覆盖记录。
                    self._nodes.pop(existing.canonical, None)
            self._write()
            return StoredNode(**candidate.as_dict())

    def remove(self, uri: str) -> Optional[StoredNode]:
        """删除用户添加的节点;文件/订阅节点只写禁用覆盖(节点本身不归存储所有)。"""
        candidate = describe_uri(uri)
        with self._lock:
            self.reload_if_changed()
            existing = self._nodes.get(candidate.canonical)
            if existing is None:
                return None
            removed = StoredNode(**existing.as_dict())
            if existing.origin == ORIGIN_USER:
                self._nodes.pop(existing.canonical, None)
            else:
                existing.enabled = False
            self._write()
            return removed

    def _write(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "updated_at": time.time(),
            "nodes": {node.canonical: node.as_dict() for node in self._nodes.values()},
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd = temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".proxy-pool-", suffix=".json.tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except Exception:
                pass
            os.replace(temp_path, self.path)
            temp_path = None
        except OSError as exc:
            raise ProxyPoolStoreError("代理池文件写入失败: %s: %s" % (self.path, exc)) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            if temp_path:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
        self._stamp = self._stamp_of()

    def as_dict(self) -> Dict:
        with self._lock:
            self.reload_if_changed()
            nodes = [StoredNode(**node.as_dict()) for node in self._nodes.values()]
            return {
                "path": self.path,
                "nodes": [node.as_dict() for node in sorted(nodes, key=lambda item: item.canonical)],
                "total": len(nodes),
                "disabled": sum(1 for node in nodes if not node.enabled),
                "user_nodes": sum(1 for node in nodes if node.origin == ORIGIN_USER),
            }
