"""Proxy pool V3: resilient sources, precise health semantics and safe registration leases."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import socket
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from curl_cffi import requests

from proxy_protocol_runtime import ProtocolRuntimeManager
from proxy_protocols import ProxyDescriptor, ProxyProtocolError, parse_proxy_line, parse_subscription_source
from proxy_pool_store import (
    ORIGIN_SOURCE,
    ORIGIN_USER,
    ProxyPoolStore,
    ProxyPoolStoreError,
    resolve_store_path,
)

_ROOT = os.path.dirname(os.path.abspath(__file__))
_MAX_SOURCE_BYTES = 2 << 20
_TLS = threading.local()
_MANAGER_LOCK = threading.RLock()
_MANAGER = None


class ProxyPoolError(RuntimeError):
    pass


class ProxyAcquireTimeout(ProxyPoolError):
    pass


class ProxyAcquireCancelled(ProxyPoolError):
    pass


class ProxyTransportError(ProxyPoolError):
    pass


class ProxyConfigurationError(ProxyPoolError):
    pass


@dataclass
class ProbeFamilyState:
    status: str = "unknown"
    tested_at: Optional[float] = None
    latency_ms: int = 0
    exit_ip: str = ""
    error: str = ""


@dataclass
class ProxyNode:
    id: str
    source: str
    proxy_url: str
    descriptor: ProxyDescriptor
    protocol: str = "http"
    name: str = ""
    backend: str = "native"
    enabled: bool = True
    rotating: bool = False
    health: float = 1.0
    attempts: int = 0
    business_samples: int = 0
    registration_successes: int = 0
    transport_failures: int = 0
    suspected_failures: int = 0
    configuration_failures: int = 0
    exit_successes: int = 0
    exit_failures: int = 0
    failure_count: int = 0
    cooldown_until: Optional[float] = None
    last_error: str = ""
    last_success_at: Optional[float] = None
    last_failure_at: Optional[float] = None
    probe_status: str = "unknown"
    last_probed_at: Optional[float] = None
    probe_latency_ms: int = 0
    probe_error: str = ""
    exit_ip: str = ""
    ipv4_probe: ProbeFamilyState = field(default_factory=ProbeFamilyState)
    ipv6_probe: ProbeFamilyState = field(default_factory=ProbeFamilyState)
    inflight: int = 0
    retired: bool = False


@dataclass
class ProxyLease:
    node_id: str
    proxy_url: str
    worker_key: str
    slot_index: int
    attempt_index: int
    affinity: str
    session_key: str
    source_uri: str = ""
    protocol: str = ""
    runtime_key: Optional[str] = None
    released: bool = False
    feedback_sampled: bool = False
    suspected_feedback: bool = False


@dataclass
class SourceState:
    descriptors: List[ProxyDescriptor] = field(default_factory=list)
    last_success_at: Optional[float] = None
    last_error: str = ""
    generation: int = 0
    diagnostics: Dict = field(default_factory=dict)
    configured: bool = False


def _config_signature(config):
    keys = (
        "proxy_mode", "proxy", "proxy_fallback", "proxy_pool_file",
        "proxy_pool_subscription_url", "proxy_pool_subscription_proxy",
        "proxy_pool_endpoint_mode", "proxy_pool_refresh_interval_sec",
        "proxy_pool_probe_interval_sec", "proxy_pool_probe_timeout_sec",
        "proxy_pool_probe_provider", "proxy_pool_probe_dual_stack",
        "proxy_pool_max_concurrent_per_node", "proxy_pool_acquire_timeout_sec",
        "proxy_protocol_backend", "proxy_singbox_path", "proxy_mihomo_path", "proxy_protocol_start_timeout_sec",
        "proxy_runtime_idle_ttl_sec", "proxy_runtime_cache_max",
        "proxy_pool_persist_health", "proxy_pool_state_file",
        "proxy_pool_subscription_public_only", "proxy_pool_store_file",
    )
    return tuple((key, config.get(key)) for key in keys)


def pool_sources_configured(config) -> bool:
    """配置了订阅 / 代理池文件 / 节点清单文件中的任意一个来源。"""
    data = config or {}
    if str(data.get("proxy_pool_subscription_url") or "").strip():
        return True
    if str(data.get("proxy_pool_file") or "").strip():
        return True
    store_file = str(data.get("proxy_pool_store_file") or "").strip()
    if not store_file:
        return False
    try:
        return os.path.exists(resolve_store_path(store_file))
    except Exception:
        return False


def normalize_proxy_url(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        descriptor = parse_proxy_line(raw)
    except ProxyProtocolError as exc:
        raise ProxyPoolError(str(exc)) from exc
    if descriptor.backend != "native":
        raise ProxyPoolError("该配置项只接受 HTTP/HTTPS/SOCKS 代理")
    return descriptor.canonical_uri


def proxy_log_label(value):
    raw = str(value or "").strip()
    return raw or "direct"


def safe_proxy_error_text(value):
    return str(value or "")


def _node_id(proxy_url):
    try:
        return parse_proxy_line(proxy_url).node_id
    except Exception:
        return hashlib.sha256(str(proxy_url).encode("utf-8")).hexdigest()[:20]


def parse_proxy_source(text):
    try:
        result = parse_subscription_source(text)
    except ProxyProtocolError as exc:
        raise ProxyPoolError(str(exc)) from exc
    return [node.canonical_uri for node in result.nodes], result.skipped


def _expand_account_placeholder(proxy_url, session_key):
    return proxy_url.replace("{account}", session_key) if "{account}" in proxy_url else proxy_url


def classify_proxy_network_error(value):
    """Return compatibility/configuration/hard_transport/suspected_transport/application."""
    kind = getattr(value, "kind", "")
    if kind in {"socks_auth", "http_proxy_auth", "configuration"}:
        return "configuration"
    if kind in {"upstream_connect", "http_connect", "socks_connect"}:
        return "hard_transport"
    if kind in {"https_proxy_tls", "remote_reset", "local_dns", "remote_dns", "bridge"}:
        return "suspected_transport"
    text = str(value or "").lower()
    if not text:
        return "application"
    compatibility = (
        "unknown url type", "unsupported proxy scheme", "http-compatible proxy endpoint",
        "does not support scheme", "代理协议不受", "proxy scheme is unsupported",
        "unsupported proxy protocol", "native-only",
    )
    if any(marker in text for marker in compatibility):
        return "compatibility"
    configuration = (
        "proxy authentication", "proxy auth", "authentication failed", "authentication method rejected",
        "credentials rejected", "credential", "http_proxy_auth", "socks_auth", "407 proxy authentication",
    )
    if any(marker in text for marker in configuration):
        return "configuration"
    hard = (
        "socks4 connect failed", "socks5 connect failed", "proxy connection failed", "proxy server refused",
        "tunnel connection failed", "could not connect to proxy", "failed to connect to proxy",
        "err_proxy_connection_failed", "err_tunnel_connection_failed", "connection refused",
        "no route to host", "network is unreachable", "upstream_connect", "http_connect", "socks_connect",
    )
    if any(marker in text for marker in hard):
        return "hard_transport"
    suspected = (
        "tls connect error", "ssl", "handshake", "unexpected eof", "unexpected_eof",
        "connection reset", "connection aborted", "remote end closed", "broken pipe",
        "timed out", "timeout", "temporarily unavailable", "connect error", "failed to connect",
        "could not connect", "remote_reset", "https_proxy_tls", "local_dns", "remote_dns",
    )
    if any(marker in text for marker in suspected):
        return "suspected_transport"
    return "application"


def _is_transport_error_text(value):
    return classify_proxy_network_error(value) in ("hard_transport", "suspected_transport")


def is_proxy_transport_exception(exc):
    return isinstance(exc, ProxyTransportError) or _is_transport_error_text(exc)


def _public_ip(address):
    try:
        value = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    return not (
        value.is_private or value.is_loopback or value.is_link_local or value.is_multicast
        or value.is_reserved or value.is_unspecified
    )


def _validate_public_url(url):
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProxyPoolError("代理订阅必须是有效的 http/https URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except Exception as exc:
        raise ProxyPoolError("代理订阅域名解析失败: %s" % exc) from exc
    addresses = {item[4][0] for item in infos}
    if not addresses or any(not _public_ip(item) for item in addresses):
        raise ProxyPoolError("代理订阅 public-only 模式拒绝非公网目标")


_SOURCE_CONFIG_KEYS = {
    "file": "proxy_pool_file",
    "subscription": "proxy_pool_subscription_url",
    "manual": "proxy_pool_manual_entries",
}


class ProxyPoolManager:
    def __init__(self, config, log=None):
        self.config = dict(config or {})
        self.log = log or (lambda message: None)
        self.signature = _config_signature(self.config)
        self.mode = str(self.config.get("proxy_mode") or "auto").strip().lower()
        self.fallback = str(self.config.get("proxy_fallback") or "none").strip().lower()
        self.endpoint_mode = str(self.config.get("proxy_pool_endpoint_mode") or "auto").strip().lower()
        self.probe_provider = str(self.config.get("proxy_pool_probe_provider") or "cloudflare").strip().lower()
        self.dual_stack = bool(self.config.get("proxy_pool_probe_dual_stack", True))
        self.capacity = max(1, int(self.config.get("proxy_pool_max_concurrent_per_node") or 1))
        self.acquire_timeout = max(1, int(self.config.get("proxy_pool_acquire_timeout_sec") or 30))
        self.refresh_interval = max(0, int(self.config.get("proxy_pool_refresh_interval_sec", 900)))
        self.probe_interval = max(0, int(self.config.get("proxy_pool_probe_interval_sec", 900)))
        self.probe_timeout = max(3, int(self.config.get("proxy_pool_probe_timeout_sec") or 15))
        self.persist_health = bool(self.config.get("proxy_pool_persist_health", False))
        self.subscription_public_only = bool(self.config.get("proxy_pool_subscription_public_only", False))
        state_file = str(self.config.get("proxy_pool_state_file") or "./proxy_pool_state.json").strip()
        state_file = os.path.expanduser(state_file)
        self.state_path = state_file if os.path.isabs(state_file) else os.path.join(_ROOT, state_file)
        store_file = str(self.config.get("proxy_pool_store_file") or "").strip()
        self.store_path = resolve_store_path(store_file)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._refresh_lock = threading.Lock()
        self._nodes = {}
        self._probe_events = {}
        self._last_refresh = 0.0
        self._last_probe_all = 0.0
        self._probe_all_running = False
        self._source_states = {"file": SourceState(), "subscription": SourceState(), "manual": SourceState()}
        self._source_diagnostics = {}
        self._persisted_state = self._load_state_file()
        self.store = ProxyPoolStore(self.store_path, log=self.log)
        self._store_ready = False
        self._assembly_error = ""
        self.config_pending = False
        self._local_stamp = None
        self._runtime = ProtocolRuntimeManager(self.config, log=self.log)
        try:
            self.reload_sources(force=True)
        except ProxyPoolError as exc:
            # 空池 / 全部禁用不应该让 manager 无法创建,否则 WebUI 连"添加第一个节点"都做不到。
            # 错误在这里记录,snapshot() 会展示,acquire() 会立即抛出。
            self._assembly_error = str(exc)
            self.log("[!] 代理池暂不可用: %s" % exc)

    def _ensure_store(self) -> bool:
        """首次使用时载入 JSON 节点清单(含旧配置键的一次性迁移)。"""
        if self._store_ready:
            return True
        try:
            self.store.load(
                legacy_manual=self.config.get("proxy_pool_manual_entries"),
                legacy_disabled=self.config.get("proxy_pool_disabled_nodes"),
            )
        except ProxyPoolStoreError as exc:
            self.log("[!] 代理池节点清单不可用: %s" % exc)
            return False
        self._store_ready = True
        return True

    @property
    def managed(self):
        return self.mode in ("single", "pool")

    def total_inflight(self):
        with self._lock:
            return sum(node.inflight for node in self._nodes.values())

    def shutdown(self):
        self._save_state_file()
        self._runtime.shutdown()

    def _load_state_file(self):
        if not self.persist_health:
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value.get("nodes", {}) if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.log("[!] 代理健康状态读取失败，忽略旧状态: %s" % exc)
            return {}

    def _save_state_file(self):
        if not self.persist_health:
            return
        with self._lock:
            nodes = {}
            for node in self._nodes.values():
                if node.retired:
                    continue
                nodes[node.id] = {
                    "health": node.health, "attempts": node.attempts, "business_samples": node.business_samples,
                    "registration_successes": node.registration_successes, "transport_failures": node.transport_failures,
                    "suspected_failures": node.suspected_failures, "configuration_failures": node.configuration_failures,
                    "exit_successes": node.exit_successes, "exit_failures": node.exit_failures,
                    "failure_count": node.failure_count, "cooldown_until": node.cooldown_until,
                    "last_error": node.last_error, "last_success_at": node.last_success_at, "last_failure_at": node.last_failure_at,
                }
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        fd = path = None
        try:
            fd, path = tempfile.mkstemp(prefix=".proxy-state-", suffix=".json.tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                json.dump({"version": 1, "saved_at": time.time(), "nodes": nodes}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush(); os.fsync(handle.fileno())
            os.replace(path, self.state_path)
            path = None
        finally:
            if fd is not None:
                try: os.close(fd)
                except Exception: pass
            if path:
                try: os.unlink(path)
                except Exception: pass

    def _restore_node_state(self, node):
        saved = self._persisted_state.get(node.id)
        if not isinstance(saved, dict):
            return
        for key in (
            "health", "attempts", "business_samples", "registration_successes", "transport_failures", "suspected_failures",
            "configuration_failures", "exit_successes", "exit_failures", "failure_count", "cooldown_until",
            "last_error", "last_success_at", "last_failure_at",
        ):
            if key in saved:
                setattr(node, key, saved[key])

    def _rotating_for(self, descriptor):
        if self.endpoint_mode == "rotating":
            return True
        if self.endpoint_mode == "fixed":
            return False
        return descriptor.backend == "native" and "{account}" in descriptor.canonical_uri

    def _read_file_source(self):
        path = str(self.config.get("proxy_pool_file") or "").strip()
        if not path:
            return None
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(_ROOT, path)
        with open(path, "r", encoding="utf-8-sig") as handle:
            return parse_subscription_source(handle.read())

    def _read_store_source(self):
        """JSON 节点清单中用户添加且启用的节点(代理池的手动来源)。"""
        if not self._ensure_store():
            raise ProxyPoolError("代理池节点清单不可用: %s" % self.store_path)
        entries = [node.uri for node in self.store.enabled_user_entries()]
        if not entries:
            return None
        return parse_subscription_source("\n".join(entries))

    def _store_source_configured(self) -> bool:
        """节点清单文件存在或已有条目(含旧配置迁移结果)时才算配置了该来源。"""
        if not self._ensure_store():
            return True      # 读取失败时让 loader 抛出错误,诊断里能看到原因
        if os.path.exists(self.store_path):
            return True
        return bool(self.store.entries())

    def _fetch_subscription(self):
        url = str(self.config.get("proxy_pool_subscription_url") or "").strip()
        if not url:
            return None
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ProxyPoolError("代理订阅必须是有效的 http/https URL")
        if self.subscription_public_only:
            _validate_public_url(url)
        via = str(self.config.get("proxy_pool_subscription_proxy") or "").strip()
        if via:
            via = normalize_proxy_url(via)
        proxies = {"http": via, "https": via} if via else {}
        current_url = url
        response = None
        for redirect_count in range(4):
            try:
                response = requests.get(
                    current_url, proxies=proxies, timeout=min(max(self.probe_timeout, 5), 60),
                    allow_redirects=False, headers={"Accept": "text/plain, text/*;q=0.9, */*;q=0.1"},
                )
            except Exception as exc:
                raise ProxyPoolError("代理订阅请求失败: %s" % safe_proxy_error_text(exc)) from exc
            status_code = int(response.status_code)
            if status_code not in (301, 302, 303, 307, 308):
                break
            if redirect_count >= 3:
                raise ProxyPoolError("代理订阅重定向次数超过 3 次")
            location = str((getattr(response, "headers", {}) or {}).get("location") or "").strip()
            if not location:
                raise ProxyPoolError("代理订阅重定向缺少 Location")
            current_url = urllib.parse.urljoin(current_url, location)
            redirected = urllib.parse.urlsplit(current_url)
            if redirected.scheme not in ("http", "https") or not redirected.netloc:
                raise ProxyPoolError("代理订阅重定向地址无效")
            if self.subscription_public_only:
                _validate_public_url(current_url)
        if response is None or not 200 <= int(response.status_code) < 300:
            raise ProxyPoolError("代理订阅返回 HTTP %s" % int(getattr(response, "status_code", 0) or 0))
        body = str(response.text or "")
        if len(body.encode("utf-8", "ignore")) > _MAX_SOURCE_BYTES:
            raise ProxyPoolError("代理订阅内容超过 2 MiB 限制")
        return parse_subscription_source(body)

    def _refresh_source(self, name, loader, configured=None):
        state = self._source_states[name]
        if configured is None:
            configured = bool(self.config.get(_SOURCE_CONFIG_KEYS.get(name, "")))
        state.configured = bool(configured)
        if not configured:
            state.descriptors = []
            state.last_error = ""
            state.diagnostics = {}
            return
        try:
            result = loader()
            if result is None:
                state.descriptors = []
                return
            state.descriptors = list(result.nodes)
            state.last_success_at = time.time()
            state.last_error = ""
            state.generation += 1
            state.diagnostics = result.as_dict()
            state.diagnostics.update({"stale": False, "generation": state.generation, "last_success_at": state.last_success_at})
            if result.skipped:
                self.log("[!] %s 跳过 %s 个无法解析的节点" % (name, result.skipped))
        except Exception as exc:
            state.last_error = safe_proxy_error_text(exc)
            if state.descriptors:
                state.diagnostics = dict(state.diagnostics)
                state.diagnostics.update({"stale": True, "error": state.last_error, "generation": state.generation})
                self.log("[!] %s 刷新失败，继续使用最近一次成功节点: %s" % (name, state.last_error))
            else:
                state.diagnostics = {"stale": True, "error": state.last_error, "generation": state.generation}

    def _source_entries(self, refresh=("file", "subscription", "manual")):
        if self.mode == "single":
            try:
                return [("single", parse_proxy_line(self.config.get("proxy")))]
            except ProxyProtocolError as exc:
                raise ProxyPoolError(str(exc)) from exc
        if self.mode != "pool":
            return []
        if "file" in refresh:
            self._refresh_source("file", self._read_file_source)
        if "subscription" in refresh:
            self._refresh_source("subscription", self._fetch_subscription)
        if "manual" in refresh:
            # 节点清单没有对应的配置键(旧键只用于迁移),有内容或文件存在时就要读取。
            self._refresh_source("manual", self._read_store_source, configured=self._store_source_configured())
        values = []
        for name in ("file", "subscription", "manual"):
            values.extend((name, item) for item in self._source_states[name].descriptors)
        unique, seen = [], set()
        for source, descriptor in values:
            if descriptor.node_id not in seen:
                seen.add(descriptor.node_id); unique.append((source, descriptor))
        self._source_diagnostics = {name: dict(state.diagnostics) for name, state in self._source_states.items() if state.configured}
        disabled = self._disabled_uris() | self._store_disabled_uris()
        if disabled:
            unique = [(source, descriptor) for source, descriptor in unique if descriptor.canonical_uri not in disabled]
        if not unique:
            if values and disabled:
                raise ProxyPoolError("代理池节点均已被禁用: %s 个节点在代理池清单或 proxy_pool_disabled_nodes 中" % len(disabled))
            errors = [
                "%s: %s" % (name, state.last_error)
                for name, state in self._source_states.items() if state.last_error
            ]
            detail = "; ".join(errors) if errors else "未配置代理池文件、订阅或节点清单"
            raise ProxyPoolError("代理池没有可用节点: %s" % detail)
        return unique

    def _disabled_uris(self):
        return {str(item).strip() for item in (self.config.get("proxy_pool_disabled_nodes") or []) if str(item).strip()}

    def _store_disabled_uris(self):
        if not self._ensure_store():
            return set()
        try:
            return set(self.store.disabled_canonicals())
        except ProxyPoolStoreError as exc:
            self.log("[!] 代理池节点清单读取失败: %s" % exc)
            return set()

    def _reload_sources_locked(self, force=False, refresh=("file", "subscription", "manual")):
        """Refresh sources while the dedicated refresh lock is held."""
        now = time.time()
        with self._lock:
            if not force and self.refresh_interval > 0 and now - self._last_refresh < self.refresh_interval:
                return self.snapshot()
        entries = self._source_entries(refresh=refresh)
        with self._condition:
            previous, updated = self._nodes, {}
            for source, descriptor in entries:
                node_id = descriptor.node_id
                old = previous.get(node_id)
                if old is not None:
                    old.source, old.proxy_url, old.descriptor = source, descriptor.canonical_uri, descriptor
                    old.protocol, old.name, old.backend = descriptor.protocol, descriptor.name, descriptor.backend
                    old.rotating, old.retired = self._rotating_for(descriptor), False
                    updated[node_id] = old
                else:
                    node = ProxyNode(
                        id=node_id, source=source, proxy_url=descriptor.canonical_uri, descriptor=descriptor,
                        protocol=descriptor.protocol, name=descriptor.name, backend=descriptor.backend,
                        rotating=self._rotating_for(descriptor),
                    )
                    self._restore_node_state(node)
                    updated[node_id] = node
            for node_id, old in previous.items():
                if node_id not in updated and old.inflight > 0:
                    old.retired = True; updated[node_id] = old
            self._nodes = updated
            self._last_refresh = now
            self._assembly_error = ""
            self._local_stamp = self._local_source_stamp()
            self._condition.notify_all()
        self._save_state_file()
        return self.snapshot()

    def reload_sources(self, force=False):
        if not self.managed:
            return self.snapshot()
        with self._refresh_lock:
            return self._reload_sources_locked(force=force)

    def _local_source_stamp(self):
        """本地来源(代理池文件 + JSON 节点清单)的变更标记。"""
        stamp = [None, None]
        path = str(self.config.get("proxy_pool_file") or "").strip()
        if path:
            path = os.path.expanduser(path)
            if not os.path.isabs(path):
                path = os.path.join(_ROOT, path)
            try:
                stat = os.stat(path)
                stamp[0] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp[0] = None
        if self._store_ready:
            try:
                stamp[1] = self.store.stamp()
            except ProxyPoolStoreError:
                stamp[1] = None
        return tuple(stamp)

    def sync_local_sources(self) -> bool:
        """本地来源被改动(手工编辑/别的进程写入)后即时同步,不重新拉订阅。"""
        if not self.managed or self.mode != "pool":
            return False
        if not self._ensure_store():
            return False
        stamp = self._local_source_stamp()
        if stamp == self._local_stamp:
            return False
        if not self._refresh_lock.acquire(blocking=False):
            # 锁被占用时不要记录 stamp:留给下一次调用重试,否则这次改动会被吞掉。
            return False
        try:
            self._reload_sources_locked(force=True, refresh=("file", "manual"))
        except Exception as exc:
            self._assembly_error = str(exc)
            self.log("[!] 本地代理源同步失败，继续使用当前节点: %s" % safe_proxy_error_text(exc))
            return False
        finally:
            self._refresh_lock.release()
        return True

    def refresh_if_due(self):
        if not self.managed:
            return
        self.sync_local_sources()
        now = time.time()
        with self._lock:
            due = self.refresh_interval > 0 and now - self._last_refresh >= self.refresh_interval
        if not due or not self._refresh_lock.acquire(blocking=False):
            return
        try:
            # Double-check after acquiring the refresh gate: another worker may
            # have completed the refresh while this worker was scheduling.
            now = time.time()
            with self._lock:
                due = self.refresh_interval > 0 and now - self._last_refresh >= self.refresh_interval
            if not due:
                return
            self._reload_sources_locked(force=True)
        except Exception as exc:
            self.log("[!] 代理池刷新失败，继续使用当前节点: %s" % safe_proxy_error_text(exc))
        finally:
            self._refresh_lock.release()

    # ------------------------------------------------------------ 节点管理(JSON 清单)
    def _require_pool_mode(self):
        if self.mode != "pool":
            raise ProxyPoolError("当前代理模式是 %s,只有 pool 模式使用代理池节点" % (self.mode or "auto"))

    def _insert_node(self, descriptor, source="manual", enabled=True):
        """把节点就地加入调度(不重建 manager,不等空闲)。"""
        with self._condition:
            node = self._nodes.get(descriptor.node_id)
            if node is None:
                node = ProxyNode(
                    id=descriptor.node_id, source=source, proxy_url=descriptor.canonical_uri, descriptor=descriptor,
                    protocol=descriptor.protocol, name=descriptor.name, backend=descriptor.backend,
                    rotating=self._rotating_for(descriptor),
                )
                self._restore_node_state(node)
                self._nodes[node.id] = node
            else:
                node.source, node.proxy_url, node.descriptor = source, descriptor.canonical_uri, descriptor
                node.protocol, node.name, node.backend = descriptor.protocol, descriptor.name, descriptor.backend
                node.rotating, node.retired = self._rotating_for(descriptor), False
            node.enabled = bool(enabled)
            self._assembly_error = ""
            self._condition.notify_all()
            return node

    def _source_descriptor_for(self, canonical):
        for name in ("file", "subscription", "manual"):
            for descriptor in self._source_states[name].descriptors:
                if descriptor.canonical_uri == canonical:
                    return name, descriptor
        return "", None

    def _resolve_node_reference(self, reference):
        """把节点标识解析成 (node_id, raw_uri, canonical)。

        UI 表格/移除按钮发的是 canonical,而高级协议节点的 canonical 是
        `vless://<sha256>` 这种伪 URI,不能直接当 URI 解析,所以这里支持三种写法:
        原始 URI、canonical、node_id。
        """
        text = str(reference or "").strip()
        if not text:
            raise ProxyPoolError("节点标识不能为空")
        try:
            descriptor = parse_proxy_line(text)
            return descriptor.node_id, descriptor.raw_uri, descriptor.canonical_uri
        except ProxyProtocolError:
            pass
        with self._lock:
            for node in self._nodes.values():
                if text in (node.id, node.descriptor.canonical_uri, node.descriptor.raw_uri):
                    return node.id, node.descriptor.raw_uri, node.descriptor.canonical_uri
        if self._ensure_store():
            try:
                for entry in self.store.entries():
                    if text in (entry.node_id, entry.canonical, entry.uri):
                        return entry.node_id, entry.uri, entry.canonical
            except ProxyPoolStoreError as exc:
                raise ProxyPoolError(str(exc)) from exc
        raise ProxyPoolError("节点不在代理池中: %s" % text)

    def add_node(self, uri, enabled=True):
        """写入 JSON 节点清单并立即参与调度。"""
        self._require_pool_mode()
        if not self._ensure_store():
            raise ProxyPoolError("代理池节点清单不可用: %s" % self.store_path)
        try:
            entry = self.store.add(uri, enabled=enabled, origin=ORIGIN_USER)
            descriptor = parse_proxy_line(entry.uri)
        except (ProxyPoolStoreError, ProxyProtocolError) as exc:
            raise ProxyPoolError(str(exc)) from exc
        node = self._insert_node(descriptor, source="manual", enabled=enabled)
        self._local_stamp = self._local_source_stamp()
        self.log("[*] 代理池已添加节点: %s" % proxy_log_label(entry.canonical))
        return {"node": self._node_snapshot(node, origin=ORIGIN_USER), "store": self.store.as_dict()}

    def remove_node(self, uri):
        """移除节点:用户添加的从清单删除,文件/订阅节点写禁用覆盖。

        `uri` 可以是原始 URI、canonical 或 node_id(WebUI 发的是 canonical)。
        """
        self._require_pool_mode()
        if not self._ensure_store():
            raise ProxyPoolError("代理池节点清单不可用: %s" % self.store_path)
        node_id, raw_uri, canonical = self._resolve_node_reference(uri)
        try:
            entry = self.store.remove(raw_uri)
            if entry is None:
                # 文件/订阅节点(或未登记节点)没有存储条目:写一条禁用覆盖。
                entry = self.store.set_enabled(raw_uri, False)
        except ProxyPoolStoreError as exc:
            raise ProxyPoolError(str(exc)) from exc
        with self._condition:
            node = self._nodes.get(node_id)
            if node is not None:
                if entry.origin == ORIGIN_USER:
                    if node.inflight > 0:
                        node.retired = True
                    else:
                        self._nodes.pop(node.id, None)
                else:
                    node.enabled = False
            self._condition.notify_all()
        self._local_stamp = self._local_source_stamp()
        self.log("[*] 代理池已移除节点: %s" % proxy_log_label(canonical))
        return {"canonical": canonical, "origin": entry.origin, "in_pool": node is not None, "store": self.store.as_dict()}

    def set_node_enabled(self, uri, enabled):
        """启用/禁用节点;启用时节点不在池中会就地补入。

        `uri` 可以是原始 URI、canonical 或 node_id(WebUI 发的是 canonical)。
        """
        self._require_pool_mode()
        if not self._ensure_store():
            raise ProxyPoolError("代理池节点清单不可用: %s" % self.store_path)
        node_id, raw_uri, canonical = self._resolve_node_reference(uri)
        try:
            existing = self.store.get(raw_uri)
        except ProxyPoolStoreError as exc:
            raise ProxyPoolError(str(exc)) from exc
        if existing is None and enabled:
            # 清单里没有条目(例如来源节点被运行时失败禁用):只恢复调度状态,不写清单。
            with self._condition:
                node = self._nodes.get(node_id)
                if node is not None:
                    node.enabled = True
                    node.retired = False
                    self._condition.notify_all()
            return {"canonical": canonical, "enabled": True, "in_pool": node is not None, "store": self.store.as_dict()}
        try:
            entry = self.store.set_enabled(raw_uri, enabled)
        except ProxyPoolStoreError as exc:
            raise ProxyPoolError(str(exc)) from exc
        with self._condition:
            node = self._nodes.get(node_id)
        if enabled and node is None:
            source, source_descriptor = self._source_descriptor_for(canonical)
            node = self._insert_node(source_descriptor or parse_proxy_line(raw_uri), source=source or "manual", enabled=True)
        elif node is not None:
            with self._condition:
                node.enabled = bool(enabled)
                if enabled:
                    node.retired = False
                self._condition.notify_all()
        self._local_stamp = self._local_source_stamp()
        return {"canonical": canonical, "enabled": bool(enabled), "in_pool": node is not None, "store": self.store.as_dict()}

    def _schedule_periodic_probe_if_due(self):
        if not self.managed or self.probe_interval <= 0:
            return
        now = time.time()
        with self._lock:
            if self._probe_all_running or now - self._last_probe_all < self.probe_interval:
                return
            self._probe_all_running = True; self._last_probe_all = now
        def runner():
            try: self.probe_all(force=True)
            finally:
                with self._lock: self._probe_all_running = False
        threading.Thread(target=runner, name="proxy-probe-all", daemon=True).start()

    def _eligible_locked(self, now):
        return [
            node for node in self._nodes.values()
            if node.enabled and not node.retired and node.inflight < self.capacity
            and (node.rotating or node.cooldown_until is None or now >= node.cooldown_until)
        ]

    def _probe_tier(self, node, now):
        freshness = max(60, (self.probe_interval * 2) if self.probe_interval > 0 else 300)
        if not node.last_probed_at or now - node.last_probed_at > freshness:
            return 1
        if node.probe_status == "healthy":
            return 0
        if node.probe_status == "unhealthy":
            return 2
        return 1

    def _select_locked(self, nodes, affinity):
        now = time.time()
        best_tier = min(self._probe_tier(node, now) for node in nodes)
        pool = sorted((node for node in nodes if self._probe_tier(node, now) == best_tier), key=lambda value: value.id)
        digest = hashlib.sha256(str(affinity or "").encode("utf-8")).digest()
        selected = pool[int.from_bytes(digest[:8], "big") % len(pool)]
        if selected.rotating or selected.health >= 0.8 or len(pool) == 1:
            return selected
        return max(pool, key=lambda value: (value.health, -value.inflight, value.id))

    def _descriptor_for_session(self, descriptor, session_key):
        if descriptor.backend != "native" or "{account}" not in descriptor.canonical_uri:
            return descriptor
        try: return parse_proxy_line(_expand_account_placeholder(descriptor.canonical_uri, session_key))
        except ProxyProtocolError as exc: raise ProxyPoolError(str(exc)) from exc

    def _fallback_lease_locked(self, worker_key, slot_index, attempt_index, affinity, session_key):
        if self.fallback == "direct":
            return ProxyLease("direct", "", worker_key, slot_index, attempt_index, affinity, session_key, protocol="direct")
        if self.fallback == "single":
            proxy_url = normalize_proxy_url(self.config.get("proxy"))
            if proxy_url:
                descriptor = parse_proxy_line(_expand_account_placeholder(proxy_url, session_key))
                endpoint, runtime_key = self._runtime.acquire(descriptor)
                return ProxyLease("fallback-single", endpoint, worker_key, slot_index, attempt_index, affinity, session_key, source_uri=proxy_url, protocol=descriptor.protocol, runtime_key=runtime_key)
        return None

    def _resolve_node_endpoint(self, node, session_key):
        return self._runtime.acquire(self._descriptor_for_session(node.descriptor, session_key))

    def _rollback_runtime_failure(self, node_id, error):
        kind = classify_proxy_network_error(error)
        with self._condition:
            node = self._nodes.get(node_id)
            if node is not None:
                node.inflight = max(0, node.inflight - 1)
                node.enabled = False
                node.probe_status = "unavailable"
                node.probe_error = safe_proxy_error_text(error)[:300]
                node.configuration_failures += 1 if kind in ("configuration", "compatibility") else 0
                node.last_error = "%s: %s" % ("configuration" if kind in ("configuration", "compatibility") else "backend", safe_proxy_error_text(error)[:260])
            self._condition.notify_all()
        self._save_state_file()

    def acquire(self, affinity, worker_key, slot_index, attempt_index, session_key, timeout=None, cancel_callback=None):
        if not self.managed:
            return None
        self.refresh_if_due(); self._schedule_periodic_probe_if_due(); self._runtime.cleanup_idle()
        if not self._nodes and self._assembly_error:
            # 装配阶段就失败(空池/全部禁用)时直接报错,不要等到租约超时。
            raise ProxyPoolError("代理池没有可用节点: %s" % self._assembly_error)
        deadline = time.time() + float(timeout if timeout is not None else self.acquire_timeout)
        last_runtime_error = None
        while True:
            selected = None
            with self._condition:
                while selected is None:
                    if cancel_callback and cancel_callback():
                        raise ProxyAcquireCancelled("代理租约等待已取消")
                    now = time.time(); eligible = self._eligible_locked(now)
                    if eligible:
                        selected = self._select_locked(eligible, affinity); selected.inflight += 1; selected.attempts += 1; break
                    active_nodes = [node for node in self._nodes.values() if node.enabled and not node.retired]
                    if not active_nodes:
                        fallback = self._fallback_lease_locked(worker_key, slot_index, attempt_index, affinity, session_key)
                        if fallback is not None: return fallback
                        detail = ": %s" % last_runtime_error if last_runtime_error else ""
                        raise ProxyPoolError("代理池当前没有可用节点%s" % detail)
                    remaining = deadline - now
                    if remaining <= 0:
                        fallback = self._fallback_lease_locked(worker_key, slot_index, attempt_index, affinity, session_key)
                        if fallback is not None: return fallback
                        raise ProxyAcquireTimeout("等待可用代理超时")
                    wake_after = min(remaining, 1.0)
                    cooldowns = [node.cooldown_until for node in active_nodes if node.cooldown_until and node.cooldown_until > now]
                    if cooldowns: wake_after = min(wake_after, max(0.05, min(cooldowns) - now))
                    self._condition.wait(timeout=wake_after)
            try:
                endpoint, runtime_key = self._resolve_node_endpoint(selected, session_key)
                return ProxyLease(selected.id, endpoint, worker_key, slot_index, attempt_index, affinity, session_key, source_uri=selected.descriptor.raw_uri, protocol=selected.protocol, runtime_key=runtime_key)
            except Exception as exc:
                last_runtime_error = safe_proxy_error_text(exc); self._rollback_runtime_failure(selected.id, exc)
                if time.time() >= deadline:
                    raise ProxyPoolError("代理协议运行时不可用: %s" % last_runtime_error) from exc

    def release(self, lease):
        if lease is None or lease.released:
            return
        lease.released = True
        if lease.runtime_key: self._runtime.release(lease.runtime_key)
        if lease.node_id in ("direct", "fallback-single"): return
        with self._condition:
            node = self._nodes.get(lease.node_id)
            if node is not None:
                node.inflight = max(0, node.inflight - 1)
                if node.retired and node.inflight == 0: self._nodes.pop(node.id, None)
            self._condition.notify_all()

    def _count_feedback_sample(self, node, lease):
        if lease is not None and lease.feedback_sampled:
            return False
        if lease is not None: lease.feedback_sampled = True
        node.business_samples += 1
        return True

    def report_success(self, lease):
        if lease is None or lease.node_id in ("direct", "fallback-single"): return
        with self._condition:
            node = self._nodes.get(lease.node_id)
            if node is None: return
            self._count_feedback_sample(node, lease)
            node.registration_successes += 1; node.last_success_at = time.time()
            if node.rotating:
                node.exit_successes += 1
                node.last_error = ""
            else:
                node.health = min(1.0, node.health + 0.1); node.failure_count = 0; node.cooldown_until = None
                if not node.last_error.startswith("backend:") and not node.last_error.startswith("configuration:"): node.last_error = ""
            self._condition.notify_all()
        self._save_state_file()

    def report_soft_failure(self, lease, error):
        if lease is None or lease.node_id in ("direct", "fallback-single"): return
        with self._lock:
            node = self._nodes.get(lease.node_id)
            if node is not None: node.last_error = "soft: %s" % safe_proxy_error_text(error)[:300]

    def _apply_configuration_failure(self, node_id, error, lease=None):
        with self._condition:
            node = self._nodes.get(node_id)
            if node is None: return
            node.configuration_failures += 1; node.last_failure_at = time.time(); node.enabled = False
            node.last_error = "configuration: %s" % safe_proxy_error_text(error)[:260]
            self._condition.notify_all()
        self._save_state_file()

    def _apply_transport_failure(self, node_id, error, schedule_probe=True, lease=None, reason=None):
        node_for_probe = None
        with self._condition:
            node = self._nodes.get(node_id)
            if node is None: return
            self._count_feedback_sample(node, lease)
            node.transport_failures += 1; node.last_failure_at = time.time()
            if node.rotating:
                node.exit_failures += 1; node.last_error = "transport: rotating exit"; self._condition.notify_all()
            else:
                node.failure_count += 1; node.health = max(0.05, node.health * 0.7)
                cooldown = min(600, 30 * (2 ** min(max(node.failure_count - 1, 0), 4)))
                node.cooldown_until = time.time() + cooldown
                node.last_error = ("transport: %s" % safe_proxy_error_text(reason)[:200]) if reason else "transport"
                node_for_probe = node.id
                self._condition.notify_all()
        self._save_state_file()
        if node_for_probe and schedule_probe: self._schedule_failure_probe(node_for_probe)

    def classify_error(self, lease, error):
        if lease is not None and lease.runtime_key:
            diagnostic = self._runtime.diagnostic_for(lease.runtime_key)
            if diagnostic:
                return classify_proxy_network_error(type("BridgeDiagnostic", (), diagnostic)())
        return classify_proxy_network_error(error)

    def report_transport_failure(self, lease, error):
        if lease is None or lease.node_id in ("direct", "fallback-single"): return
        kind = self.classify_error(lease, error)
        if kind in ("configuration", "compatibility"):
            self._apply_configuration_failure(lease.node_id, error, lease=lease)
        elif kind == "suspected_transport":
            self.report_suspected_transport_failure(lease, error)
        else:
            self._apply_transport_failure(lease.node_id, error, schedule_probe=True, lease=lease)

    def report_suspected_transport_failure(self, lease, error):
        if lease is None or lease.node_id in ("direct", "fallback-single"): return
        with self._lock:
            node = self._nodes.get(lease.node_id)
            if node is not None: node.suspected_failures += 1
        lease.suspected_feedback = True
        self._schedule_failure_probe(lease.node_id, penalize_on_failure=True, suspected_error=error, lease=lease)

    def report_code_wait_failure(self, lease, error):
        """取码失败：当前出口按传输失败处理并进入冷却，冷却期内不再被调度选中。

        这不是连通性故障，所以不安排探测：节点必须等冷却自然到期（或后续注册成功）才恢复，
        避免“探测一通过就立刻又被同一出口选走”。
        """
        if lease is None or lease.node_id in ("direct", "fallback-single"):
            return
        self._apply_transport_failure(
            lease.node_id,
            error,
            schedule_probe=False,
            lease=lease,
            reason="code_wait: %s" % safe_proxy_error_text(error),
        )

    def _probe_endpoint(self, family="ipv4"):
        if self.probe_provider == "ipinfo":
            return "https://v6.ipinfo.io/json" if family == "ipv6" else "https://ipinfo.io/json"
        return "https://[2606:4700:4700::1111]/cdn-cgi/trace" if family == "ipv6" else "https://1.1.1.1/cdn-cgi/trace"

    def _parse_probe_ip(self, response):
        text = str(response.text or "")
        if self.probe_provider == "ipinfo":
            try:
                value = response.json()
                return str(value.get("ip") or "").strip() if isinstance(value, dict) else ""
            except Exception:
                return ""
        for line in text.splitlines():
            if line.startswith("ip="): return line[3:].strip()
        return ""

    def _probe_family(self, descriptor, session_key, family):
        runtime_key = None; started = time.monotonic(); status = "unhealthy"; exit_ip = ""; error = ""
        try:
            resolved = self._descriptor_for_session(descriptor, session_key)
            proxy_url, runtime_key = self._runtime.acquire(resolved)
            response = requests.get(self._probe_endpoint(family), proxies={"http": proxy_url, "https": proxy_url}, timeout=self.probe_timeout, allow_redirects=False)
            if not 200 <= int(response.status_code) < 300:
                raise ProxyPoolError("HTTP %s" % response.status_code)
            exit_ip = self._parse_probe_ip(response)
            try: parsed_ip = ipaddress.ip_address(exit_ip)
            except ValueError: raise ProxyPoolError("探测服务返回 2xx 但没有有效 IP")
            expected = 6 if family == "ipv6" else 4
            if parsed_ip.version != expected:
                raise ProxyPoolError("探测服务返回的 IP family 与 %s 不匹配" % family)
            exit_ip = str(parsed_ip); status = "healthy"
        except Exception as exc:
            error = safe_proxy_error_text(exc)
        finally:
            if runtime_key: self._runtime.release(runtime_key)
        return ProbeFamilyState(status=status, tested_at=time.time(), latency_ms=max(1, int((time.monotonic() - started) * 1000)), exit_ip=exit_ip if status == "healthy" else "", error=error[:300] if status != "healthy" else "")

    def probe_node(self, node_id):
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None: raise ProxyPoolError("代理节点不存在")
            descriptor = node.descriptor; session_key = secrets.token_hex(8)
        families = ["ipv6", "ipv4"] if self.dual_stack else ["ipv4"]
        outcomes = {}
        if len(families) == 1:
            outcomes[families[0]] = self._probe_family(descriptor, session_key, families[0])
        else:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="proxy-family-probe") as executor:
                futures = {executor.submit(self._probe_family, descriptor, session_key, family): family for family in families}
                for future in as_completed(futures): outcomes[futures[future]] = future.result()
        ipv4 = outcomes.get("ipv4", ProbeFamilyState()); ipv6 = outcomes.get("ipv6", ProbeFamilyState())
        healthy = [value for value in (ipv4, ipv6) if value.status == "healthy"]
        status = "healthy" if healthy else "unhealthy"
        chosen = ipv4 if ipv4.status == "healthy" else ipv6 if ipv6.status == "healthy" else ipv4
        error = "; ".join("%s: %s" % (name.upper(), value.error) for name, value in (("ipv4", ipv4), ("ipv6", ipv6)) if value.error)
        with self._condition:
            node = self._nodes.get(node_id)
            if node is not None:
                node.ipv4_probe, node.ipv6_probe = ipv4, ipv6
                node.probe_status = status; node.last_probed_at = time.time()
                node.probe_latency_ms = max(ipv4.latency_ms, ipv6.latency_ms); node.probe_error = error[:300] if status != "healthy" else ""
                node.exit_ip = chosen.exit_ip if status == "healthy" else ""
                if status == "healthy":
                    node.enabled = True
                    if node.last_error == "transport" or node.last_error.startswith("backend:"):
                        if not node.rotating:
                            node.health = 1.0; node.failure_count = 0; node.cooldown_until = None
                        node.last_error = ""
                self._condition.notify_all()
        self._save_state_file()
        return {
            "id": node_id, "status": status, "latency_ms": max(ipv4.latency_ms, ipv6.latency_ms),
            "exit_ip": chosen.exit_ip if status == "healthy" else "", "error": error,
            "ipv4": self._family_dict(ipv4), "ipv6": self._family_dict(ipv6),
        }

    @staticmethod
    def _family_dict(value):
        return {"status": value.status, "tested_at": value.tested_at, "latency_ms": value.latency_ms, "exit_ip": value.exit_ip, "error": value.error}

    def _schedule_failure_probe(self, node_id, penalize_on_failure=False, suspected_error=None, lease=None):
        with self._lock:
            existing = self._probe_events.get(node_id)
            if existing is not None and not existing.is_set(): return
            event = threading.Event(); self._probe_events[node_id] = event
        def runner():
            try:
                result = self.probe_node(node_id)
                if penalize_on_failure and result.get("status") != "healthy":
                    self._apply_transport_failure(node_id, suspected_error or result.get("error") or "probe failed", schedule_probe=False, lease=lease)
            except Exception:
                if penalize_on_failure:
                    self._apply_transport_failure(node_id, suspected_error or "probe failed", schedule_probe=False, lease=lease)
            finally:
                event.set()
                with self._lock:
                    if self._probe_events.get(node_id) is event: self._probe_events.pop(node_id, None)
        threading.Thread(target=runner, name="proxy-probe-%s" % node_id[:8], daemon=True).start()

    def probe_all(self, force=False):
        now = time.time()
        with self._lock:
            if not force and self.probe_interval > 0 and now - self._last_probe_all < self.probe_interval: return []
            node_ids = [node.id for node in self._nodes.values() if not node.retired]; self._last_probe_all = now
        results = []
        if not node_ids: return results
        with ThreadPoolExecutor(max_workers=min(8, len(node_ids)), thread_name_prefix="proxy-probe") as executor:
            futures = {executor.submit(self.probe_node, node_id): node_id for node_id in node_ids}
            for future in as_completed(futures):
                try: results.append(future.result())
                except Exception as exc: results.append({"id": futures[future], "status": "unhealthy", "error": safe_proxy_error_text(exc)})
        return results

    def preflight_node(self, node_id):
        """Non-destructive reachability test against registration-path origins."""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None: raise ProxyPoolError("代理节点不存在")
            descriptor = node.descriptor; session_key = secrets.token_hex(8)
        runtime_key = None
        try:
            proxy_url, runtime_key = self._runtime.acquire(self._descriptor_for_session(descriptor, session_key))
            results = []
            for url in ("https://accounts.x.ai/", "https://grok.com/"):
                started = time.monotonic()
                try:
                    response = requests.get(url, proxies={"http": proxy_url, "https": proxy_url}, timeout=self.probe_timeout, allow_redirects=False)
                    status_code = int(response.status_code)
                    text = str(getattr(response, "text", "") or "")[:4096].lower()
                    headers = {str(k).lower(): str(v).lower() for k, v in dict(getattr(response, "headers", {}) or {}).items()}
                    cloudflare = "cloudflare" in headers.get("server", "") or "cf-error" in text or "__cf_chl" in text
                    reachable = 100 <= status_code < 600
                    cloudflare_block = bool(cloudflare and status_code in (403, 429, 503))
                    usable = bool(reachable and not cloudflare_block and 200 <= status_code < 400)
                    results.append({"url": url, "reachable": reachable, "usable": usable, "status_code": status_code, "latency_ms": max(1, int((time.monotonic()-started)*1000)), "cloudflare_block": cloudflare_block, "error": ""})
                except Exception as exc:
                    results.append({"url": url, "reachable": False, "usable": False, "status_code": 0, "latency_ms": max(1, int((time.monotonic()-started)*1000)), "cloudflare_block": False, "error": safe_proxy_error_text(exc)[:300]})
            return {"id": node_id, "ok": all(item["usable"] for item in results), "targets": results}
        finally:
            if runtime_key: self._runtime.release(runtime_key)

    def _node_snapshot(self, node, now=None, origin=""):
        now = time.time() if now is None else now
        cooldown = int(max(1, node.cooldown_until - now)) if node.cooldown_until and node.cooldown_until > now else 0
        gateway_samples = node.exit_successes + node.exit_failures
        gateway_success_rate = round(node.exit_successes / gateway_samples, 4) if gateway_samples else None
        return {
            "id": node.id, "source": node.source, "proxy": node.descriptor.raw_uri, "name": node.name,
            "canonical": node.descriptor.canonical_uri,
            "protocol": node.protocol, "backend": node.backend, "enabled": bool(node.enabled), "rotating": bool(node.rotating),
            "health_model": "gateway" if node.rotating else "fixed", "health": None if node.rotating else round(float(node.health), 3),
            "business_samples": int(node.business_samples), "registration_successes": node.registration_successes,
            "attempts": int(node.attempts), "success_rate": round(node.registration_successes / node.attempts, 4) if node.attempts else None,
            "transport_failures": node.transport_failures, "suspected_failures": node.suspected_failures,
            "configuration_failures": node.configuration_failures, "exit_successes": node.exit_successes, "exit_failures": node.exit_failures,
            "gateway_success_rate": gateway_success_rate, "failure_count": int(node.failure_count), "cooldown_sec": 0 if node.rotating else cooldown,
            "last_error": str(node.last_error or "")[:300], "last_success_at": node.last_success_at, "last_failure_at": node.last_failure_at,
            "probe_status": node.probe_status, "last_probed_at": node.last_probed_at, "probe_latency_ms": int(node.probe_latency_ms or 0),
            "probe_error": str(node.probe_error or "")[:300], "exit_ip": node.exit_ip,
            "ipv4_probe": self._family_dict(node.ipv4_probe), "ipv6_probe": self._family_dict(node.ipv6_probe),
            "inflight": int(node.inflight), "retired": bool(node.retired), "origin": origin,
            "core": "" if node.backend == "native" else self._runtime.core_name,
        }

    def _store_origins(self):
        """canonical -> origin(user/source),用于标记节点是否由用户添加。"""
        if not self._ensure_store():
            return {}
        try:
            return {entry.canonical: entry.origin for entry in self.store.entries()}
        except ProxyPoolStoreError as exc:
            self.log("[!] 代理池节点清单读取失败: %s" % exc)
            return {}

    def snapshot(self):
        with self._lock:
            now = time.time()
            origins = self._store_origins()
            nodes = [
                self._node_snapshot(node, now, origins.get(node.descriptor.canonical_uri, ""))
                for node in sorted(self._nodes.values(), key=lambda value: value.id)
            ]
            store = {"path": self.store_path, "total": 0, "disabled": 0, "user_nodes": 0, "nodes": []}
            try:
                summary = self.store.as_dict()
                store.update({key: summary[key] for key in ("total", "disabled", "user_nodes", "nodes")})
            except ProxyPoolStoreError as exc:
                store["error"] = str(exc)
            return {"mode": self.mode, "managed": self.managed, "fallback": self.fallback, "capacity": self.capacity, "nodes": nodes, "sources": dict(self._source_diagnostics), "runtime": self._runtime.active_snapshot(), "persist_health": self.persist_health, "store": store, "error": self._assembly_error, "config_pending": bool(self.config_pending)}


def get_manager(config=None, log=None):
    global _MANAGER
    if config is None:
        from app_config import config as app_config
        config = app_config
    signature = _config_signature(config)
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = ProxyPoolManager(config, log=log)
        elif _MANAGER.signature != signature:
            if _MANAGER.total_inflight() == 0:
                old = _MANAGER; _MANAGER = ProxyPoolManager(config, log=log); old.shutdown()
            else:
                # 有租约在使用:不能中途换 Manager,但也不能静默丢掉这次配置变更。
                # 标记待生效,租约释放后的下一次 get_manager 会自动重建。
                _MANAGER.config_pending = True
        if log is not None:
            _MANAGER.log = log; _MANAGER._runtime.log = log
        return _MANAGER


def reset_manager():
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None and _MANAGER.total_inflight() > 0:
            raise ProxyPoolError("仍有代理租约使用中，不能重置代理池")
        old = _MANAGER; _MANAGER = None
    if old is not None: old.shutdown()


def current_proxy_lease(): return getattr(_TLS, "lease", None)
def current_proxy_url():
    lease = current_proxy_lease(); return None if lease is None else str(lease.proxy_url or "")
def managed_proxy_active(): return current_proxy_lease() is not None


def begin_registration_slot(slot_index, attempt_index=1, worker_key=None, log=None, cancel_callback=None):
    if current_proxy_lease() is not None: raise ProxyPoolError("当前线程已有未释放的代理租约")
    manager = get_manager(log=log)
    if not manager.managed: return None
    worker = str(worker_key or threading.current_thread().name or "worker"); slot = int(slot_index); attempt = int(attempt_index)
    affinity = "%s:slot:%s" % (worker, slot)
    session_seed = "%s:%s:%s:%s" % (worker, slot, attempt, secrets.token_hex(8))
    session_key = hashlib.sha256(session_seed.encode("utf-8")).hexdigest()[:16]
    lease = manager.acquire(affinity=affinity, worker_key=worker, slot_index=slot, attempt_index=attempt, session_key=session_key, cancel_callback=cancel_callback)
    _TLS.lease = lease
    if log is not None:
        label = lease.source_uri or lease.proxy_url; log("[*] 当前账号代理: %s" % proxy_log_label(label))
        if lease.source_uri and lease.proxy_url and lease.source_uri != lease.proxy_url: log("[*] 当前代理本地出口: %s" % lease.proxy_url)
    return lease


def end_registration_slot(success=False, transport_error=None):
    lease = current_proxy_lease()
    if lease is None: return
    manager = get_manager()
    try:
        if transport_error is not None: manager.report_transport_failure(lease, transport_error)
        elif success: manager.report_success(lease)
    finally:
        manager.release(lease); _TLS.lease = None


def report_current_transport_failure(error):
    lease = current_proxy_lease()
    if lease is not None: get_manager().report_transport_failure(lease, error)


def report_current_code_wait_failure(error):
    """当前租约在取码阶段失败：冷却该出口，后续调度不再选它。"""
    lease = current_proxy_lease()
    if lease is not None: get_manager().report_code_wait_failure(lease, error)


def report_current_suspected_transport_failure(error):
    lease = current_proxy_lease()
    if lease is not None: get_manager().report_suspected_transport_failure(lease, error)


def manager_snapshot(config=None):
    try:
        manager = get_manager(config=config)
        manager.sync_local_sources()
        return manager.snapshot()
    except Exception as exc:
        return {"mode": str((config or {}).get("proxy_mode") or "auto"), "managed": False, "nodes": [], "sources": {}, "error": safe_proxy_error_text(exc)}
