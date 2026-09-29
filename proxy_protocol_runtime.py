"""Lazy local runtime that exposes every managed proxy through a common endpoint."""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from proxy_bridge import LocalProxyBridge, proxy_has_auth
from proxy_mihomo import ProxyMihomoError, build_config as build_mihomo_config
from proxy_protocols import ProxyDescriptor


class ProxyRuntimeError(RuntimeError):
    pass


class _CoreBackend:
    """一个本地 HTTP 出口核心(sing-box / mihomo)的统一适配。"""

    name = ""
    binary = ""
    path_key = ""
    config_name = "config.json"
    alternative_hint = ""

    def __init__(self, config, log=None):
        self.config = dict(config or {})
        self.log = log or (lambda _message: None)

    @property
    def configured_path(self):
        return str(self.config.get(self.path_key) or "").strip()

    def executable(self):
        candidate = os.path.expanduser(self.configured_path) if self.configured_path else shutil.which(self.binary)
        if not candidate:
            raise ProxyRuntimeError("检测到高级代理节点，但未找到 %s；请安装到 PATH 或配置 %s%s" % (
                self.binary, self.path_key, self.alternative_hint,
            ))
        if not os.path.isfile(candidate):
            resolved = shutil.which(candidate)
            if not resolved:
                raise ProxyRuntimeError("%s 可执行文件不存在: %s" % (self.binary, candidate))
            candidate = resolved
        return candidate

    def build_config(self, descriptor, port):     # pragma: no cover - 由子类实现
        raise NotImplementedError

    def write_config(self, value):
        fd, path = tempfile.mkstemp(prefix="grok-register-proxy-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            return path
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                os.unlink(path)
            except Exception:
                pass
            raise

    def cleanup_config(self, path):
        if not path:
            return
        try:
            os.unlink(path)
        except Exception:
            pass

    def check_args(self, executable, path):       # pragma: no cover - 由子类实现
        raise NotImplementedError

    def run_args(self, executable, path):         # pragma: no cover - 由子类实现
        raise NotImplementedError


class SingBoxBackend(_CoreBackend):
    name = "sing-box"
    binary = "sing-box"
    path_key = "proxy_singbox_path"
    config_name = "config.json"
    alternative_hint = "(或安装 mihomo 并配置 proxy_mihomo_path)"

    @staticmethod
    def build_config(descriptor, port):
        outbound = copy.deepcopy(descriptor.outbound_config or {})
        if not outbound:
            raise ProxyRuntimeError("高级代理节点缺少 outbound 配置")
        outbound["tag"] = "proxy"
        return {
            "log": {"level": "warn", "timestamp": True},
            "inbounds": [{"type": "http", "tag": "local-http", "listen": "127.0.0.1", "listen_port": int(port)}],
            "outbounds": [outbound],
            "route": {"final": "proxy"},
        }

    @staticmethod
    def check_args(executable, path):
        return [executable, "check", "-c", path]

    @staticmethod
    def run_args(executable, path):
        return [executable, "run", "-c", path]


class MihomoBackend(_CoreBackend):
    name = "mihomo"
    binary = "mihomo"
    path_key = "proxy_mihomo_path"
    config_name = "config.yaml"

    @staticmethod
    def build_config(descriptor, port):
        try:
            return build_mihomo_config(descriptor, port)
        except ProxyMihomoError as exc:
            raise ProxyRuntimeError(str(exc)) from exc

    def write_config(self, value):
        """mihomo 用 `-d <目录>` 读取目录里的 config.yaml,所以配置写进临时目录。"""
        directory = tempfile.mkdtemp(prefix="grok-register-mihomo-")
        path = os.path.join(directory, self.config_name)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            return path
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def cleanup_config(self, path):
        if not path:
            return
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    @staticmethod
    def check_args(executable, path):
        return [executable, "-t", "-d", os.path.dirname(path)]

    @staticmethod
    def run_args(executable, path):
        return [executable, "-d", os.path.dirname(path)]


@dataclass
class RuntimeEntry:
    node_id: str
    process: Optional[subprocess.Popen]
    port: int
    config_path: str
    refcount: int = 0
    bridge: Optional[LocalProxyBridge] = None
    kind: str = "sing-box"
    idle_since: Optional[float] = None
    last_used: float = 0.0

    @property
    def proxy_url(self):
        return "http://127.0.0.1:%s" % self.port

    @property
    def alive(self):
        if self.kind == "bridge":
            return bool(self.bridge is not None and self.bridge.server is not None)
        return bool(self.process is not None and self.process.poll() is None)

    def diagnostic(self):
        if self.bridge is None:
            return None
        return self.bridge.diagnostic()


class ProtocolRuntimeManager:
    """Resolve native and advanced nodes into consumer-compatible endpoints."""

    def __init__(self, config=None, log=None):
        self.config = dict(config or {})
        self.log = log or (lambda _message: None)
        self.backend = str(self.config.get("proxy_protocol_backend") or "auto").strip().lower()
        self.executable = str(self.config.get("proxy_singbox_path") or "").strip()
        self.start_timeout = max(3, int(self.config.get("proxy_protocol_start_timeout_sec") or 10))
        self.idle_ttl = max(0, int(self.config.get("proxy_runtime_idle_ttl_sec", 120)))
        self.cache_max = max(1, int(self.config.get("proxy_runtime_cache_max", 32)))
        self._condition = threading.Condition(threading.RLock())
        self._entries = {}
        self._starting = set()
        self._backends = {
            "sing-box": SingBoxBackend(self.config, log=self.log),
            "mihomo": MihomoBackend(self.config, log=self.log),
        }
        self._selected_backend = None

    def _resolve_backend(self):
        """按配置选中核心:显式指定优先,auto 依次尝试 sing-box / mihomo。"""
        if self._selected_backend is not None:
            return self._selected_backend
        if self.backend == "native-only":
            raise ProxyRuntimeError("高级代理协议已被 proxy_protocol_backend=native-only 禁用")
        if self.backend in self._backends:
            self._selected_backend = self._backends[self.backend]
            return self._selected_backend
        if self.backend not in ("", "auto"):
            raise ProxyRuntimeError("未知的 proxy_protocol_backend: %s" % self.backend)
        for name in ("sing-box", "mihomo"):
            backend = self._backends[name]
            if backend.configured_path or shutil.which(backend.binary):
                self._selected_backend = backend
                return backend
        # 两个核心都没装:保持 sing-box 作为默认,由 executable() 抛出可执行的错误信息。
        self._selected_backend = self._backends["sing-box"]
        return self._selected_backend

    @property
    def core_name(self):
        """当前选中的核心名(sing-box / mihomo);不可用时返回空字符串。"""
        try:
            return self._resolve_backend().name
        except ProxyRuntimeError:
            return ""

    def _find_executable(self):
        return self._resolve_backend().executable()

    @staticmethod
    def _free_port():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
        finally:
            sock.close()

    def _build_config(self, descriptor, port):
        return self._resolve_backend().build_config(descriptor, port)

    def _write_config(self, value):
        return self._resolve_backend().write_config(value)

    def _check_config(self, executable, path):
        backend = self._resolve_backend()
        try:
            completed = subprocess.run(
                backend.check_args(executable, path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=self.start_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProxyRuntimeError("%s 配置检查超时" % backend.name) from exc
        except Exception as exc:
            raise ProxyRuntimeError("无法执行 %s 配置检查: %s" % (backend.name, exc)) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "configuration rejected").strip()
            raise ProxyRuntimeError("%s 配置检查失败: %s" % (backend.name, detail[:500]))

    @staticmethod
    def _port_ready(port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            return sock.connect_ex(("127.0.0.1", int(port))) == 0
        finally:
            sock.close()

    def _start_entry(self, descriptor):
        backend = self._resolve_backend()
        executable = self._find_executable()
        errors = []
        for attempt in range(1, 5):
            port = self._free_port()
            path = self._write_config(self._build_config(descriptor, port))
            process = None
            try:
                self._check_config(executable, path)
                process = subprocess.Popen(backend.run_args(executable, path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.time() + self.start_timeout
                while time.time() < deadline:
                    code = process.poll()
                    if code is not None:
                        raise ProxyRuntimeError("%s 在本地代理就绪前退出，code=%s" % (backend.name, code))
                    if self._port_ready(port):
                        self.log("[*] 高级代理运行时已就绪(%s): %s -> 127.0.0.1:%s" % (backend.name, descriptor.protocol, port))
                        now = time.time()
                        return RuntimeEntry(descriptor.node_id, process, port, path, 0, kind=backend.name, last_used=now)
                    time.sleep(0.05)
                raise ProxyRuntimeError("等待 %s 本地代理启动超时" % backend.name)
            except Exception as exc:
                errors.append("attempt=%s port=%s: %s" % (attempt, port, exc))
                if process is not None:
                    try:
                        process.terminate(); process.wait(timeout=2)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
                backend.cleanup_config(path)
        raise ProxyRuntimeError("%s startup failed after 4 attempts: %s" % (backend.name, "; ".join(errors)))

    def _start_bridge_entry(self, descriptor):
        bridge = LocalProxyBridge(descriptor.canonical_uri)
        try:
            endpoint = bridge.start()
        except Exception as exc:
            raise ProxyRuntimeError("本地 HTTP 代理桥启动失败: %s" % exc) from exc
        try:
            port = int(urllib.parse.urlsplit(endpoint).port or 0)
        except Exception:
            port = 0
        if port <= 0:
            bridge.stop()
            raise ProxyRuntimeError("本地 HTTP 代理桥未返回有效端口")
        self.log("[*] 原生代理已标准化为本地 HTTP 出口: %s -> %s" % (descriptor.protocol, endpoint))
        return RuntimeEntry(descriptor.node_id, None, port, "", 0, bridge=bridge, kind="bridge", last_used=time.time())

    def _stop_entry(self, entry):
        if entry.kind == "bridge":
            if entry.bridge is not None:
                try:
                    entry.bridge.stop()
                except Exception:
                    pass
            return
        try:
            if entry.process is not None and entry.process.poll() is None:
                entry.process.terminate()
                try:
                    entry.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    entry.process.kill(); entry.process.wait(timeout=2)
        except Exception:
            pass
        backend = self._backends.get(entry.kind)
        if backend is not None:
            backend.cleanup_config(entry.config_path)
        elif entry.config_path:
            try:
                os.unlink(entry.config_path)
            except Exception:
                pass

    @staticmethod
    def _native_requires_bridge(descriptor):
        parsed = urllib.parse.urlsplit(descriptor.canonical_uri)
        scheme = (parsed.scheme or "http").lower()
        return scheme != "http" or proxy_has_auth(descriptor.canonical_uri)

    def _cleanup_locked(self, now=None):
        now = time.time() if now is None else float(now)
        stale = []
        for key, entry in list(self._entries.items()):
            if not entry.alive:
                stale.append(self._entries.pop(key))
            elif entry.refcount == 0 and entry.idle_since is not None and (self.idle_ttl == 0 or now - entry.idle_since >= self.idle_ttl):
                stale.append(self._entries.pop(key))
        idle = sorted(
            ((key, value) for key, value in self._entries.items() if value.refcount == 0),
            key=lambda pair: pair[1].last_used,
        )
        while len(self._entries) > self.cache_max and idle:
            key, entry = idle.pop(0)
            if self._entries.pop(key, None) is entry:
                stale.append(entry)
        return stale

    def cleanup_idle(self):
        with self._condition:
            stale = self._cleanup_locked()
        for entry in stale:
            self._stop_entry(entry)
        return len(stale)

    def _acquire_runtime_entry(self, descriptor, starter):
        while True:
            stale = []
            endpoint = runtime_key = None
            with self._condition:
                stale = self._cleanup_locked()
                current = self._entries.get(descriptor.node_id)
                if current is not None and current.alive:
                    current.refcount += 1
                    current.idle_since = None
                    current.last_used = time.time()
                    endpoint = current.proxy_url
                    runtime_key = descriptor.node_id
                    self._condition.notify_all()
                    break
                if descriptor.node_id not in self._starting:
                    self._starting.add(descriptor.node_id)
                    endpoint = runtime_key = None
                    break
                self._condition.wait(timeout=0.2)
            for entry in stale:
                self._stop_entry(entry)
            if endpoint is not None:
                return endpoint, runtime_key
        for entry in stale:
            self._stop_entry(entry)
        if endpoint is not None:
            return endpoint, runtime_key
        try:
            entry = starter(descriptor)
            entry.refcount = 1
            entry.last_used = time.time()
            with self._condition:
                self._entries[descriptor.node_id] = entry
                return entry.proxy_url, descriptor.node_id
        finally:
            with self._condition:
                self._starting.discard(descriptor.node_id)
                self._condition.notify_all()

    def acquire(self, descriptor):
        if descriptor.backend == "native":
            if not self._native_requires_bridge(descriptor):
                return descriptor.canonical_uri, None
            return self._acquire_runtime_entry(descriptor, self._start_bridge_entry)
        if descriptor.backend != "sing-box":
            raise ProxyRuntimeError("未知高级代理后端: %s" % descriptor.backend)
        return self._acquire_runtime_entry(descriptor, self._start_entry)

    def release(self, runtime_key):
        if not runtime_key:
            return
        stop_now = None
        with self._condition:
            current = self._entries.get(runtime_key)
            if current is None:
                return
            current.refcount = max(0, current.refcount - 1)
            current.last_used = time.time()
            if current.refcount == 0:
                current.idle_since = current.last_used
                if self.idle_ttl == 0:
                    stop_now = self._entries.pop(runtime_key, None)
            self._condition.notify_all()
        if stop_now is not None:
            self._stop_entry(stop_now)

    def diagnostic_for(self, runtime_key, max_age=15):
        if not runtime_key:
            return None
        with self._condition:
            entry = self._entries.get(runtime_key)
            diagnostic = None if entry is None else entry.diagnostic()
        if not diagnostic:
            return None
        if time.time() - float(diagnostic.get("at") or 0) > float(max_age):
            return None
        return diagnostic

    def active_snapshot(self):
        self.cleanup_idle()
        with self._condition:
            now = time.time()
            return {
                key: {
                    "port": value.port,
                    "refcount": value.refcount,
                    "alive": value.alive,
                    "kind": value.kind,
                    "idle_sec": int(max(0, now - value.idle_since)) if value.idle_since else 0,
                    "diagnostic": value.diagnostic(),
                }
                for key, value in self._entries.items()
            }

    def shutdown(self):
        with self._condition:
            entries = list(self._entries.values())
            self._entries.clear()
            self._starting.clear()
            self._condition.notify_all()
        for entry in entries:
            self._stop_entry(entry)
