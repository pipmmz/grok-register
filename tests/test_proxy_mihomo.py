"""mihomo(Clash.Meta)后端:节点 → Clash 配置的映射测试。"""
import base64
import json
import unittest

from proxy_mihomo import ProxyMihomoError, build_config, build_proxy
from proxy_protocols import parse_proxy_line


def _vmess_uri(**overrides):
    payload = {"v": "2", "ps": "vm", "add": "vm.example.com", "port": "443",
               "id": "11111111-1111-1111-1111-111111111111", "aid": "0", "scy": "auto",
               "net": "ws", "type": "none", "host": "h.example.com", "path": "/vm", "tls": "tls",
               "sni": "vm.example.com", "fp": "chrome"}
    payload.update(overrides)
    return "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()


class MihomoProxyMappingTests(unittest.TestCase):
    def _proxy(self, uri):
        return build_proxy(parse_proxy_line(uri))

    def test_vless_reality_grpc_maps_tls_flow_and_transport(self):
        proxy = self._proxy(
            "vless://11111111-1111-1111-1111-111111111111@a.example.com:443"
            "?security=reality&pbk=KEY&sid=ab&fp=chrome&type=grpc&serviceName=gs&flow=xtls-rprx-vision#node"
        )
        self.assertEqual(proxy["type"], "vless")
        self.assertEqual(proxy["uuid"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(proxy["flow"], "xtls-rprx-vision")
        self.assertTrue(proxy["tls"])
        self.assertEqual(proxy["client-fingerprint"], "chrome")
        self.assertEqual(proxy["reality-opts"], {"public-key": "KEY", "short-id": "ab"})
        self.assertEqual(proxy["network"], "grpc")
        self.assertEqual(proxy["grpc-opts"], {"grpc-service-name": "gs"})
        self.assertEqual((proxy["server"], proxy["port"]), ("a.example.com", 443))

    def test_vless_websocket_transport_maps_path_and_host_header(self):
        proxy = self._proxy(
            "vless://11111111-1111-1111-1111-111111111111@b.example.com:443"
            "?security=tls&sni=b.example.com&type=ws&path=%2Fws&host=h.example.com"
        )
        self.assertEqual(proxy["network"], "ws")
        self.assertEqual(proxy["ws-opts"], {"path": "/ws", "headers": {"Host": "h.example.com"}})
        self.assertEqual(proxy["servername"], "b.example.com")

    def test_vmess_maps_cipher_alter_id_and_transport(self):
        proxy = self._proxy(_vmess_uri(aid="4"))
        self.assertEqual(proxy["type"], "vmess")
        self.assertEqual(proxy["cipher"], "auto")
        self.assertEqual(proxy["alterId"], 4)
        self.assertTrue(proxy["tls"])
        self.assertEqual(proxy["servername"], "vm.example.com")
        self.assertEqual(proxy["ws-opts"], {"path": "/vm", "headers": {"Host": "h.example.com"}})

    def test_vmess_always_emits_alter_id(self):
        # mihomo 把 alterId 当必填:aid 缺省时也必须写 0,否则 -t 直接失败
        proxy = self._proxy(_vmess_uri(aid="0"))
        self.assertEqual(proxy["alterId"], 0)

    def test_trojan_maps_password_sni_and_skip_cert_verify(self):
        proxy = self._proxy("trojan://secret@c.example.com:443?sni=c.example.com&allowInsecure=1")
        self.assertEqual(proxy["type"], "trojan")
        self.assertEqual(proxy["password"], "secret")
        self.assertEqual(proxy["sni"], "c.example.com")
        self.assertTrue(proxy["skip-cert-verify"])

    def test_hysteria2_maps_obfs_bandwidth_and_sni(self):
        proxy = self._proxy(
            "hysteria2://pw@d.example.com:443?sni=d.example.com&obfs=salamander&obfs-password=xyz&upmbps=50&downmbps=200"
        )
        self.assertEqual(proxy["type"], "hysteria2")
        self.assertEqual(proxy["password"], "pw")
        self.assertEqual(proxy["obfs"], "salamander")
        self.assertEqual(proxy["obfs-password"], "xyz")
        self.assertEqual((proxy["up"], proxy["down"]), (50, 200))
        self.assertEqual(proxy["sni"], "d.example.com")

    def test_tuic_maps_congestion_relay_and_heartbeat(self):
        proxy = self._proxy(
            "tuic://11111111-1111-1111-1111-111111111111:pw@e.example.com:443"
            "?sni=e.example.com&congestion_control=bbr&udp_relay_mode=native&heartbeat=10000"
        )
        self.assertEqual(proxy["type"], "tuic")
        self.assertEqual(proxy["congestion-controller"], "bbr")
        self.assertEqual(proxy["udp-relay-mode"], "native")
        self.assertEqual(proxy["heartbeat-interval"], 10000)
        self.assertEqual(proxy["sni"], "e.example.com")

    def test_tuic_rejects_non_integer_heartbeat(self):
        descriptor = parse_proxy_line(
            "tuic://11111111-1111-1111-1111-111111111111:pw@e.example.com:443?sni=e.example.com&heartbeat=10s"
        )
        with self.assertRaises(ProxyMihomoError):
            build_proxy(descriptor)

    def test_shadowsocks_maps_method_to_cipher(self):
        proxy = self._proxy("ss://YWVzLTI1Ni1nY206cGFzcw==@f.example.com:8388#ss")
        self.assertEqual(proxy["type"], "ss")
        self.assertEqual(proxy["cipher"], "aes-256-gcm")
        self.assertEqual(proxy["password"], "pass")

    def test_unsupported_transport_reports_actionable_error(self):
        descriptor = parse_proxy_line(
            "vless://11111111-1111-1111-1111-111111111111@a.example.com:443?security=tls&type=httpupgrade&host=h.example.com&path=%2Fup"
        )
        with self.assertRaises(ProxyMihomoError) as raised:
            build_proxy(descriptor)
        self.assertIn("sing-box", str(raised.exception))


class MihomoConfigTests(unittest.TestCase):
    def test_config_exposes_one_local_mixed_port_and_match_rule(self):
        descriptor = parse_proxy_line("trojan://secret@a.example.com:443?sni=a.example.com")
        config = build_config(descriptor, 32123)
        self.assertEqual(config["mixed-port"], 32123)
        self.assertEqual(config["bind-address"], "127.0.0.1")
        self.assertFalse(config["allow-lan"])
        self.assertEqual(config["mode"], "rule")
        self.assertEqual(config["rules"], ["MATCH,node"])
        self.assertEqual(len(config["proxies"]), 1)
        self.assertEqual(config["proxies"][0]["name"], "node")
        # 配置直接用 JSON 写(mihomo 能解析),所以必须是可序列化的
        json.dumps(config)

    def test_missing_outbound_config_reports_error(self):
        descriptor = parse_proxy_line("http://127.0.0.1:8080")
        with self.assertRaises(ProxyMihomoError):
            build_config(descriptor, 1234)


if __name__ == "__main__":
    unittest.main()
