"""mihomo(Clash.Meta)后端:把高级协议节点渲染成 Clash 配置。

与 sing-box 后端并列使用:sing-box 走 JSON 配置 + `sing-box run -c`;
mihomo 走 Clash 配置 + `mihomo -d <目录>`(目录里的 `config.yaml`)。
两者都只暴露一个本地 mixed-port(HTTP + SOCKS),上层 ProxyLease 和所有
消费者(Chromium / curl_cffi / probe / preflight)完全不用区分。

配置直接用 JSON 写:YAML 1.2 是 JSON 的超集,mihomo 能原样解析,所以这里
不需要引入 PyYAML。
"""

from __future__ import annotations

from typing import Callable, Dict

from proxy_protocols import ProxyDescriptor

# 本地代理在生成配置里的固定名称:必须与 rules 里的 MATCH 一致,
# 不取用户的节点名,避免逗号/特殊字符破坏规则。
LOCAL_PROXY_NAME = "node"

_SUPPORTED = ("vless", "vmess", "trojan", "hysteria2", "tuic", "shadowsocks")


class ProxyMihomoError(RuntimeError):
    """节点无法映射成 mihomo 配置。"""


def _transport_options(outbound: Dict) -> Dict:
    transport = outbound.get("transport") or {}
    kind = str(transport.get("type") or "").strip().lower()
    if not kind:
        return {}
    if kind == "ws":
        options: Dict = {}
        if transport.get("path"):
            options["path"] = transport["path"]
        headers = transport.get("headers") or {}
        if headers:
            options["headers"] = dict(headers)
        return {"network": "ws", "ws-opts": options}
    if kind == "grpc":
        options = {}
        if transport.get("service_name"):
            options["grpc-service-name"] = transport["service_name"]
        return {"network": "grpc", "grpc-opts": options}
    if kind in ("http", "h2"):
        options = {}
        if transport.get("host"):
            options["host"] = list(transport["host"])
        if transport.get("path"):
            options["path"] = transport["path"]
        return {"network": "h2", "h2-opts": options}
    raise ProxyMihomoError(
        "mihomo 后端不支持 transport=%s,该节点请改用 proxy_protocol_backend=sing-box" % (kind or "?")
    )


def _apply_tls(proxy: Dict, outbound: Dict, server_name_key: str) -> None:
    tls = outbound.get("tls") or {}
    if not tls.get("enabled"):
        return
    proxy["tls"] = True
    if tls.get("server_name"):
        proxy[server_name_key] = tls["server_name"]
    if tls.get("insecure"):
        proxy["skip-cert-verify"] = True
    if tls.get("alpn"):
        proxy["alpn"] = list(tls["alpn"])
    fingerprint = (tls.get("utls") or {}).get("fingerprint")
    if fingerprint:
        proxy["client-fingerprint"] = fingerprint
    reality = tls.get("reality") or {}
    if reality.get("enabled"):
        public_key = str(reality.get("public_key") or "").strip()
        if not public_key:
            raise ProxyMihomoError("Reality 节点缺少 public key")
        options = {"public-key": public_key}
        if reality.get("short_id"):
            options["short-id"] = reality["short_id"]
        proxy["reality-opts"] = options


def _vless(outbound: Dict, proxy: Dict) -> None:
    proxy.update({"type": "vless", "uuid": outbound["uuid"]})
    if outbound.get("flow"):
        proxy["flow"] = outbound["flow"]
    if outbound.get("packet_encoding"):
        proxy["packet-encoding"] = outbound["packet_encoding"]
    proxy.update(_transport_options(outbound))
    _apply_tls(proxy, outbound, "servername")


def _vmess(outbound: Dict, proxy: Dict) -> None:
    proxy.update({"type": "vmess", "uuid": outbound["uuid"], "cipher": outbound.get("security") or "auto"})
    # mihomo 把 alterId 当作必填字段(即使为 0),缺失会直接拒绝配置。
    proxy["alterId"] = int(outbound.get("alter_id") or 0)
    proxy.update(_transport_options(outbound))
    _apply_tls(proxy, outbound, "servername")


def _trojan(outbound: Dict, proxy: Dict) -> None:
    proxy.update({"type": "trojan", "password": outbound["password"]})
    proxy.update(_transport_options(outbound))
    _apply_tls(proxy, outbound, "sni")


def _hysteria2(outbound: Dict, proxy: Dict) -> None:
    proxy.update({"type": "hysteria2", "password": outbound["password"]})
    obfs = outbound.get("obfs") or {}
    if obfs.get("type"):
        proxy["obfs"] = obfs["type"]
    if obfs.get("password"):
        proxy["obfs-password"] = obfs["password"]
    if outbound.get("up_mbps"):
        proxy["up"] = int(outbound["up_mbps"])
    if outbound.get("down_mbps"):
        proxy["down"] = int(outbound["down_mbps"])
    _apply_tls(proxy, outbound, "sni")


def _tuic(outbound: Dict, proxy: Dict) -> None:
    proxy.update({"type": "tuic", "uuid": outbound["uuid"], "password": outbound["password"]})
    if outbound.get("congestion_control"):
        proxy["congestion-controller"] = outbound["congestion_control"]
    if outbound.get("udp_relay_mode"):
        proxy["udp-relay-mode"] = outbound["udp_relay_mode"]
    if outbound.get("zero_rtt_handshake"):
        proxy["reduce-rtt"] = True
    heartbeat = outbound.get("heartbeat")
    if heartbeat:
        if not str(heartbeat).strip().isdigit():
            raise ProxyMihomoError("mihomo 的 tuic heartbeat 需要毫秒整数,收到: %r" % heartbeat)
        proxy["heartbeat-interval"] = int(str(heartbeat).strip())
    _apply_tls(proxy, outbound, "sni")


def _shadowsocks(outbound: Dict, proxy: Dict) -> None:
    proxy.update({"type": "ss", "cipher": outbound["method"], "password": outbound["password"]})


_HANDLERS: Dict[str, Callable[[Dict, Dict], None]] = {
    "vless": _vless, "vmess": _vmess, "trojan": _trojan,
    "hysteria2": _hysteria2, "tuic": _tuic, "shadowsocks": _shadowsocks,
}


def build_proxy(descriptor: ProxyDescriptor) -> Dict:
    """把一个高级协议节点渲染成 Clash `proxies:` 条目。"""
    outbound = dict(descriptor.outbound_config or {})
    if not outbound:
        raise ProxyMihomoError("高级代理节点缺少 outbound 配置")
    protocol = str(outbound.get("type") or descriptor.protocol or "").strip().lower()
    handler = _HANDLERS.get(protocol)
    if handler is None:
        raise ProxyMihomoError("mihomo 后端不支持协议: %s" % (protocol or "?"))
    server = str(outbound.get("server") or "").strip()
    try:
        port = int(outbound.get("server_port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not server or not port:
        raise ProxyMihomoError("节点缺少 server 或 port")
    proxy: Dict = {"name": LOCAL_PROXY_NAME}
    handler(outbound, proxy)
    proxy["server"] = server
    proxy["port"] = port
    proxy["udp"] = True
    return proxy


def build_config(descriptor: ProxyDescriptor, port: int) -> Dict:
    """生成只暴露一个本地 mixed-port 的 mihomo 配置。"""
    return {
        "mixed-port": int(port),
        "bind-address": "127.0.0.1",
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": True,
        "proxies": [build_proxy(descriptor)],
        "rules": ["MATCH,%s" % LOCAL_PROXY_NAME],
    }
