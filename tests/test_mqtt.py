"""MiniMQTT, the bridge's own MQTT 3.1.1 client, against an in-process fake broker on loopback.

The broker decodes packets from the specification (see fake_broker.py), so these tests check what goes
on the wire, not merely that the client agrees with itself.
"""
from __future__ import annotations

import socket
import time
import unittest

from tests import fake_broker as fb
from tests.support import dhb

WILL_TOPIC = "deck-ha-bridge/steamdeck/availability"


class WireCase(unittest.TestCase):
    def broker(self, **kw) -> fb.FakeBroker:
        b = fb.FakeBroker(**kw)
        self.addCleanup(b.close)
        return b

    def client(self, broker: fb.FakeBroker, **kw) -> dhb.MiniMQTT:
        kw.setdefault("client_id", "deck-ha-bridge-test")
        c = dhb.MiniMQTT("127.0.0.1", broker.port, **kw)
        self.addCleanup(c.close)
        return c

    def connected(self, **broker_kw):
        b = self.broker(**broker_kw)
        c = self.client(b)
        c.connect(WILL_TOPIC, "offline")
        self.assertTrue(b.wait_for(lambda: b.of_type(fb.CONNECT)))
        return b, c

    def inbox(self, c: dhb.MiniMQTT) -> list:
        got: list = []
        c.on_message = lambda topic, payload: got.append((topic, payload))
        return got


class LengthEncoding(unittest.TestCase):
    # Table 2.4 of the MQTT 3.1.1 specification: the smallest and largest value of each length.
    SPEC = [(0, b"\x00"), (127, b"\x7f"), (128, b"\x80\x01"), (16383, b"\xff\x7f"),
            (16384, b"\x80\x80\x01"), (2097151, b"\xff\xff\x7f"), (2097152, b"\x80\x80\x80\x01"),
            (268435455, b"\xff\xff\xff\x7f")]

    def test_spec_vectors(self):
        for n, wire in self.SPEC:
            with self.subTest(n=n):
                self.assertEqual(dhb._enc_len(n), wire)

    def test_every_boundary_decodes_back(self):
        for n in (0, 1, 127, 128, 129, 16383, 16384, 70_000):
            with self.subTest(n=n):
                c = dhb.MiniMQTT("h", 1, "c")
                c.buf = b"\x30" + dhb._enc_len(n) + b"x" * n
                header, body = c._try_parse()
                self.assertEqual((header, len(body)), (0x30, n))
                self.assertEqual(c.buf, b"")

    def test_an_incomplete_packet_is_left_in_the_buffer(self):
        c = dhb.MiniMQTT("h", 1, "c")
        for partial in (b"", b"\x30", b"\x30\x80", b"\x30\x05ab"):
            with self.subTest(partial=partial):
                c.buf = partial
                self.assertIsNone(c._try_parse())
                self.assertEqual(c.buf, partial)

    def test_a_length_of_more_than_four_bytes_is_malformed(self):
        c = dhb.MiniMQTT("h", 1, "c")
        c.buf = b"\x30\xff\xff\xff\xff\x00"
        with self.assertRaisesRegex(dhb.MQTTError, "malformed packet length"):
            c._try_parse()

    def test_strings_are_prefixed_with_their_length_in_bytes(self):
        self.assertEqual(dhb._enc_str("abc"), b"\x00\x03abc")
        self.assertEqual(dhb._enc_str("°C"), b"\x00\x03\xc2\xb0C")  # two bytes for the degree sign
        self.assertEqual(dhb._enc_str(b"\x01\x02"), b"\x00\x02\x01\x02")


class Connect(WireCase):
    def test_connect_with_credentials_and_a_will(self):
        b = self.broker()
        c = self.client(b, username="deck", password="s3cret", keepalive=30)
        c.connect(WILL_TOPIC, "offline")
        self.assertTrue(b.wait_for(lambda: b.of_type(fb.CONNECT)))
        info = b.of_type(fb.CONNECT)[0].info
        self.assertEqual(info["protocol"], b"MQTT")
        self.assertEqual(info["level"], 4)  # 3.1.1
        self.assertEqual(info["flags"], 0x80 | 0x40 | 0x20 | 0x04 | 0x02)
        self.assertEqual(info["keepalive"], 30)
        self.assertEqual(info["client_id"], "deck-ha-bridge-test")
        self.assertEqual((info["will_topic"], info["will_payload"]), (WILL_TOPIC, "offline"))
        self.assertTrue(info["will_retain"])
        self.assertEqual(info["will_qos"], 0)
        self.assertTrue(info["clean_session"])
        self.assertEqual((info["username"], info["password"]), ("deck", "s3cret"))

    def test_connect_without_credentials_sends_neither(self):
        b = self.broker()
        self.client(b).connect(WILL_TOPIC, "offline")
        self.assertTrue(b.wait_for(lambda: b.of_type(fb.CONNECT)))
        info = b.of_type(fb.CONNECT)[0].info
        self.assertEqual(info["flags"], 0x20 | 0x04 | 0x02)
        self.assertEqual((info["username"], info["password"]), (None, None))

    def test_a_username_without_a_password(self):
        b = self.broker()
        self.client(b, username="deck").connect(WILL_TOPIC, "offline")
        self.assertTrue(b.wait_for(lambda: b.of_type(fb.CONNECT)))
        info = b.of_type(fb.CONNECT)[0].info
        self.assertEqual(info["flags"], 0x80 | 0x20 | 0x04 | 0x02)
        self.assertEqual((info["username"], info["password"]), ("deck", None))

    def test_accepted_connection_is_blocking_and_not_waiting_for_a_ping_reply(self):
        _, c = self.connected()
        self.assertIsNone(c.sock.gettimeout())
        self.assertFalse(c.ping_outstanding)

    def test_every_refusal_code_has_a_readable_reason(self):
        reasons = {1: "unsupported protocol", 2: "client id rejected", 3: "broker unavailable",
                   4: "bad username or password", 5: "not authorized", 9: "9"}
        for rc, text in reasons.items():
            with self.subTest(rc=rc):
                b = self.broker(connack_rcs=[rc])
                c = self.client(b)
                with self.assertRaisesRegex(dhb.MQTTError, f"broker refused connection: {text}$"):
                    c.connect(WILL_TOPIC, "offline")

    def test_a_reply_that_is_not_a_connack(self):
        for bad in (b"\xd0\x00", b"\x20\x01\x00"):  # a PINGRESP; a CONNACK that is one byte short
            with self.subTest(bad=bad):
                b = self.broker(connack_bytes=bad)
                with self.assertRaisesRegex(dhb.MQTTError, "did not answer CONNECT"):
                    self.client(b).connect(WILL_TOPIC, "offline")

    def test_a_broker_that_hangs_up_on_connect(self):
        b = self.broker(connack_bytes=b"")
        b.connect_hook = lambda n: b.drop()
        with self.assertRaisesRegex(dhb.MQTTError, "connection closed by broker"):
            self.client(b).connect(WILL_TOPIC, "offline")

    def test_a_port_nobody_listens_on_is_an_oserror(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OSError):
            dhb.MiniMQTT("127.0.0.1", port, "c").connect(WILL_TOPIC, "offline")


class Publish(WireCase):
    def test_qos0_publish_with_and_without_retain(self):
        b, c = self.connected()
        c.publish("a/b", "one", retain=True)
        c.publish("a/c", b"\x00\x01binary")
        pubs = b.wait_publishes(2)
        self.assertEqual([(p.topic, p.info["payload"], p.info["retain"], p.info["qos"], p.info["dup"]) for p in pubs],
                         [("a/b", b"one", True, 0, False), ("a/c", b"\x00\x01binary", False, 0, False)])

    def test_text_is_sent_as_utf8(self):
        b, c = self.connected()
        c.publish("t", "21.5 °C, Ñandú")
        self.assertEqual(b.wait_publishes(1)[0].text, "21.5 °C, Ñandú")

    def test_publishing_while_not_connected_is_an_error(self):
        c = dhb.MiniMQTT("127.0.0.1", 1, "c")
        with self.assertRaisesRegex(dhb.MQTTError, "not connected"):
            c.publish("t", "x")

    def test_remaining_length_encoding_at_the_boundaries(self):
        topic = "t/x"
        for total in (127, 128, 16383, 16384):  # the packet's remaining length, i.e. 2 + topic + payload
            with self.subTest(remaining_length=total):
                b, c = self.connected()
                payload = bytes(range(256)) * (total // 256 + 1)
                payload = payload[: total - 2 - len(topic)]
                c.publish(topic, payload)
                got = b.wait_publishes(1)[0]
                self.assertEqual(len(got.body), total)
                self.assertEqual(got.info["payload"], payload)

    def test_subscribe_packet_and_packet_ids(self):
        b, c = self.connected()
        c.subscribe("homeassistant/status")
        c.subscribe("other/#")
        self.assertTrue(b.wait_for(lambda: len(b.of_type(fb.SUBSCRIBE)) == 2))
        first, second = b.of_type(fb.SUBSCRIBE)
        self.assertEqual(first.flags, 0x2)  # reserved bits the spec requires for SUBSCRIBE
        self.assertEqual(first.info["filters"], [("homeassistant/status", 0)])
        self.assertEqual(second.info["filters"], [("other/#", 0)])
        self.assertEqual((first.info["packet_id"], second.info["packet_id"]), (1, 2))

    def test_packet_id_wraps_to_one_and_never_uses_zero(self):
        b, c = self.connected()
        c._pid = 65535
        c.subscribe("x")
        self.assertTrue(b.wait_for(lambda: b.of_type(fb.SUBSCRIBE)))
        self.assertEqual(b.of_type(fb.SUBSCRIBE)[0].info["packet_id"], 1)

    def test_disconnect_says_goodbye_and_closes(self):
        b, c = self.connected()
        c.disconnect()
        self.assertTrue(b.wait_for(lambda: b.of_type(fb.DISCONNECT)))
        self.assertEqual(b.of_type(fb.DISCONNECT)[0].body, b"")
        self.assertIsNone(c.sock)


class Inbound(WireCase):
    def feed(self, c: dhb.MiniMQTT, data: bytes, timeout: float = 5.0) -> None:
        """Deliver `data` and run the client's loop once, the way the bridge's main loop would."""
        self.broker_ref.send(data)
        c.loop(timeout)

    def setUp(self):
        self.broker_ref, self.c = self.connected()
        self.got = self.inbox(self.c)

    def test_a_qos0_publish_reaches_on_message(self):
        self.feed(self.c, fb.encode_publish("homeassistant/status", "online"))
        self.assertEqual(self.got, [("homeassistant/status", "online")])

    def test_a_qos1_publish_skips_its_packet_id(self):
        self.feed(self.c, fb.encode_publish("t", "payload", qos=1, packet_id=0x1234))
        self.assertEqual(self.got, [("t", "payload")])

    def test_two_packets_in_one_segment_are_both_delivered_in_order(self):
        self.feed(self.c, fb.encode_publish("a", "1") + fb.encode_publish("b", "2"))
        self.assertEqual(self.got, [("a", "1"), ("b", "2")])

    def test_a_packet_split_across_reads_is_delivered_once_complete(self):
        payload = "x" * 200  # remaining length 204 needs a two-byte length field
        wire = fb.encode_publish("split/topic", payload)
        cuts = [1, 2, 3, 5, 12, len(wire) - 1]  # after the header, mid-length, mid-topic, mid-payload
        for cut in cuts:
            with self.subTest(cut=cut):
                self.got.clear()
                self.broker_ref.send(wire[:cut])
                self.c.loop(5.0)
                self.assertEqual(self.got, [], "delivered before the packet was complete")
                self.broker_ref.send(wire[cut:])
                self.c.loop(5.0)
                self.assertEqual(self.got, [("split/topic", payload)])

    def test_large_packets_at_the_length_boundaries(self):
        for total in (127, 128, 16383, 16384, 70_000):  # 70,000 also spans several 4096-byte reads
            with self.subTest(remaining_length=total):
                self.got.clear()
                payload = "x" * (total - 2 - len("t"))  # remaining length = 2 + topic + payload
                wire = fb.encode_publish("t", payload)
                self.assertEqual(len(wire) - len(fb.encode_length(total)) - 1, total)
                self.broker_ref.send(wire)
                deadline = time.monotonic() + 5
                while not self.got and time.monotonic() < deadline:
                    self.c.loop(0.5)
                self.assertEqual(self.got, [("t", payload)])

    def test_invalid_utf8_is_replaced_not_fatal(self):
        self.feed(self.c, fb.encode_publish("t", b"ok\xff\xfeend"))
        self.assertEqual(self.got, [("t", "ok��end")])

    def test_acks_and_unknown_packets_are_ignored(self):
        self.feed(self.c, bytes([fb.SUBACK << 4, 3, 0, 1, 0]) + bytes([0x40, 2, 0, 1]) + fb.encode_publish("t", "x"))
        self.assertEqual(self.got, [("t", "x")])

    def test_a_quiet_loop_just_returns(self):
        self.c.loop(0.05)
        self.assertEqual(self.got, [])
        self.assertFalse(self.c.ping_outstanding)


class KeepAlive(WireCase):
    def test_an_idle_connection_sends_a_pingreq_after_half_the_keepalive(self):
        b, c = self.connected()
        c.last_tx = time.monotonic() - (c.keepalive / 2 + 1)
        c.loop(0.05)
        self.assertTrue(b.wait_for(lambda: b.of_type(fb.PINGREQ)))
        self.assertEqual(b.of_type(fb.PINGREQ)[0].body, b"")
        self.assertTrue(c.ping_outstanding)

    def test_no_second_ping_while_one_is_outstanding(self):
        b, c = self.connected(answer_pings=False)
        c.last_tx = time.monotonic() - 100
        c.loop(0.05)
        c.last_tx = time.monotonic() - 100
        c.loop(0.05)
        self.assertTrue(b.wait_for(lambda: b.of_type(fb.PINGREQ)))
        self.assertFalse(b.wait_for(lambda: len(b.of_type(fb.PINGREQ)) > 1, timeout=0.3))

    def test_a_recently_active_connection_is_not_pinged(self):
        b, c = self.connected()
        c.publish("t", "x")
        c.loop(0.05)
        self.assertFalse(b.wait_for(lambda: b.of_type(fb.PINGREQ), timeout=0.3))

    def test_a_pingresp_clears_the_outstanding_ping(self):
        b, c = self.connected()
        c.last_tx = time.monotonic() - 100
        c.loop(0.05)
        self.assertTrue(c.ping_outstanding)
        c.loop(5.0)  # the broker answers; this reads it
        self.assertFalse(c.ping_outstanding)

    def test_a_broker_that_never_answers_the_ping_is_declared_dead(self):
        b, c = self.connected(answer_pings=False)
        c.last_tx = time.monotonic() - 100
        c.loop(0.05)
        self.assertTrue(c.ping_outstanding)
        c.last_rx = time.monotonic() - (c.keepalive + 1)
        with self.assertRaisesRegex(dhb.MQTTError, "broker stopped responding"):
            c.loop(0.05)

    def test_a_dropped_connection_is_reported_by_the_next_loop(self):
        b, c = self.connected()
        b.drop()
        with self.assertRaisesRegex(dhb.MQTTError, "connection closed by broker"):
            c.loop(5.0)


if __name__ == "__main__":
    unittest.main()
