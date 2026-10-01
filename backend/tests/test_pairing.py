"""扫码地址与重连回归：python -m unittest backend.tests.test_pairing -v"""
import asyncio
import contextlib
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from backend.pairing import build_pair_url, pairing_relay_url


class PairingUrlTests(unittest.TestCase):
    def phone_url(self, relay_url, *, public_url="", lan_ip="192.168.1.5", controller_id="new-id"):
        cfg = {"relay": {"url": relay_url, "public_url": public_url}}
        qr_url = build_pair_url(cfg, lan_ip, controller_id)
        outer = urlsplit(qr_url)
        self.assertEqual((outer.scheme, outer.netloc, outer.path), ("https", "dungeon-lab.cn", "/s/"))
        query = parse_qs(outer.query)
        self.assertEqual(query["v"], ["1"])
        self.assertEqual(query["action"], ["socket"])
        return query["url"][0]

    def test_default_local_relay_uses_lan_address(self):
        self.assertEqual(
            self.phone_url("ws://127.0.0.1:9998"),
            "ws://192.168.1.5:9998?tid=new-id",
        )

    def test_local_custom_port_path_and_tls_survive(self):
        for host in ("localhost", "127.0.0.2", "0.0.0.0", "[::]", "[::1]"):
            with self.subTest(host=host):
                self.assertEqual(
                    self.phone_url(f"wss://{host}:12000/relay/v4"),
                    "wss://192.168.1.5:12000/relay/v4?tid=new-id",
                )

    def test_remote_relay_survives_without_public_override(self):
        self.assertEqual(
            self.phone_url("wss://relay.example/v4"),
            "wss://relay.example/v4?tid=new-id",
        )

    def test_public_override_preserves_queries_and_replaces_both_old_id_names(self):
        phone = self.phone_url(
            "ws://127.0.0.1:9998",
            public_url="wss://relay.example:8443/v4?token=a%2Bb&tag=one&tag=two&empty=&tid=old&targetId=older",
        )
        parts = urlsplit(phone)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("wss", "relay.example:8443", "/v4"))
        self.assertEqual(parse_qs(parts.query, keep_blank_values=True), {
            "token": ["a+b"], "tag": ["one", "two"], "empty": [""], "tid": ["new-id"],
        })

    def test_controller_id_is_encoded_separately(self):
        phone = self.phone_url("ws://127.0.0.1:9998", controller_id="id+with&reserved?chars")
        self.assertEqual(parse_qs(urlsplit(phone).query)["tid"], ["id+with&reserved?chars"])

    def test_ipv6_lan_address_is_bracketed(self):
        self.assertEqual(
            self.phone_url("ws://[::1]:9998/v4", lan_ip="fd00::1234"),
            "ws://[fd00::1234]:9998/v4?tid=new-id",
        )

    def test_explicit_lan_relay_is_not_replaced(self):
        cfg = {"relay": {"url": "ws://192.168.20.4:9998/v4"}}
        self.assertEqual(pairing_relay_url(cfg, "192.168.1.5"), cfg["relay"]["url"])


class FakeSocket:
    """接收队列模拟服务器握手与断线；没有网络，也没有设备发送方法。"""
    def __init__(self, controller_id):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"type": "hello", "clientId": controller_id}))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if isinstance(frame, Exception):
            raise frame
        return frame


class RelayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_invalidates_id_and_reconnect_clears_error(self):
        from backend.relay_client import RelayClient

        events = asyncio.Queue()
        first, second = FakeSocket("old-id"), FakeSocket("new-id")

        async def on_event(event, payload):
            await events.put((event, client.to_state()))

        async def next_event(name):
            while True:
                event, state = await asyncio.wait_for(events.get(), timeout=2)
                if event == name:
                    return state

        client = RelayClient("ws://fake.invalid:9998", reconnect_delay_s=0, on_event=on_event)
        client.last_error = "previous connection failed"
        with patch("backend.relay_client.websockets.connect", side_effect=[first, second]):
            task = asyncio.create_task(client.run())
            try:
                connected = await next_event("hello")
                self.assertEqual(connected["controller_id"], "old-id")
                self.assertEqual(connected["last_error"], "")

                first.incoming.put_nowait(json.dumps({"type": "client_attached", "clientId": "phone"}))
                self.assertEqual((await next_event("client_attached"))["status"], "paired")
                first.incoming.put_nowait(ConnectionError("relay restarted"))

                disconnected = await next_event("disconnected")
                self.assertIsNone(disconnected["controller_id"])
                self.assertEqual(disconnected["clients"], [])
                self.assertEqual(disconnected["status"], "disconnected")
                self.assertEqual(disconnected["last_error"], "relay restarted")

                reconnected = await next_event("hello")
                self.assertEqual(reconnected["controller_id"], "new-id")
                self.assertEqual(reconnected["last_error"], "")
                self.assertEqual(reconnected["status"], "waiting")
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self.assertIsNone(client.controller_id)
            self.assertIsNone(client.ws)
            self.assertEqual(client.status, "disconnected")


if __name__ == "__main__":
    unittest.main()
