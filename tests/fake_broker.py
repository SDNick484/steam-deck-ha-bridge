"""An in-process MQTT 3.1.1 broker stand-in for the tests, listening on a loopback port.

Packets are decoded here straight from the specification (OASIS MQTT 3.1.1, sections 2 and 3), not with
the bridge's own helpers, so a bug in the bridge's encoder cannot hide behind a matching bug in the test.
It records every packet it receives, answers CONNECT, PINGREQ and SUBSCRIBE the way a broker does, and
lets a test push raw bytes to the client, split them however it likes, or drop the connection.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass, field

CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 8, 9, 12, 13, 14


def encode_length(n: int) -> bytes:
    """Remaining Length, spec section 2.2.3."""
    out = bytearray()
    while True:
        digit, n = n % 128, n // 128
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode_publish(topic: str, payload, retain: bool = False, qos: int = 0, packet_id: int = 1) -> bytes:
    t = topic.encode("utf-8")
    p = payload.encode("utf-8") if isinstance(payload, str) else payload
    body = struct.pack("!H", len(t)) + t
    if qos:
        body += struct.pack("!H", packet_id)
    body += p
    return bytes([0x30 | (qos << 1) | (1 if retain else 0)]) + encode_length(len(body)) + body


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def read_packet(sock: socket.socket) -> tuple[int, bytes]:
    first = _read_exact(sock, 1)[0]
    mult, length = 1, 0
    while True:
        digit = _read_exact(sock, 1)[0]
        length += (digit & 0x7F) * mult
        mult *= 128
        if not digit & 0x80:
            break
    return first, (_read_exact(sock, length) if length else b"")


def _take_str(body: bytes, off: int) -> tuple[bytes, int]:
    (n,) = struct.unpack("!H", body[off:off + 2])
    return body[off + 2:off + 2 + n], off + 2 + n


def decode_connect(body: bytes) -> dict:
    proto, off = _take_str(body, 0)
    level, flags = body[off], body[off + 1]
    (keepalive,) = struct.unpack("!H", body[off + 2:off + 4])
    off += 4
    client_id, off = _take_str(body, off)
    info = {"protocol": proto, "level": level, "flags": flags, "keepalive": keepalive,
            "client_id": client_id.decode(), "will_topic": None, "will_payload": None,
            "will_retain": bool(flags & 0x20), "will_qos": (flags >> 3) & 3,
            "clean_session": bool(flags & 0x02), "username": None, "password": None}
    if flags & 0x04:
        t, off = _take_str(body, off)
        p, off = _take_str(body, off)
        info["will_topic"], info["will_payload"] = t.decode(), p.decode()
    if flags & 0x80:
        u, off = _take_str(body, off)
        info["username"] = u.decode()
    if flags & 0x40:
        pw, off = _take_str(body, off)
        info["password"] = pw.decode()
    return info


def decode_publish(first: int, body: bytes) -> dict:
    topic, off = _take_str(body, 0)
    qos = (first >> 1) & 3
    packet_id = None
    if qos:
        (packet_id,) = struct.unpack("!H", body[off:off + 2])
        off += 2
    return {"topic": topic.decode(), "payload": body[off:], "retain": bool(first & 1), "qos": qos,
            "dup": bool(first & 8), "packet_id": packet_id}


def decode_subscribe(body: bytes) -> dict:
    (packet_id,) = struct.unpack("!H", body[:2])
    off, filters = 2, []
    while off < len(body):
        t, off = _take_str(body, off)
        filters.append((t.decode(), body[off]))
        off += 1
    return {"packet_id": packet_id, "filters": filters}


@dataclass
class Packet:
    conn: int
    type: int
    flags: int
    body: bytes
    t: float
    info: dict = field(default_factory=dict)

    @property
    def topic(self) -> str | None:
        return self.info.get("topic")

    @property
    def text(self) -> str:
        return self.info["payload"].decode("utf-8")


class FakeBroker:
    """connack_rcs: return codes for the first connections (then 0 = accepted forever).
    connack_bytes: send these raw bytes instead of a CONNACK (to test a broker that answers wrongly).
    silent: read CONNECT but never answer it.
    """

    def __init__(self, connack_rcs=(), answer_pings: bool = True, silent: bool = False,
                 connack_bytes: bytes | None = None):
        self.connack_rcs = list(connack_rcs)
        self.answer_pings = answer_pings
        self.silent = silent
        self.connack_bytes = connack_bytes
        self.connect_hook = None  # called with the connection number, in the broker's thread
        self.cond = threading.Condition()
        self.packets: list[Packet] = []
        self.connections = 0
        self.conn: socket.socket | None = None
        self._conns: list[socket.socket] = []
        self._closed = False
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # --- serving
    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with self.cond:
                self.connections += 1
                number = self.connections
                self.conn = conn
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn, number), daemon=True).start()

    def _serve(self, conn: socket.socket, number: int) -> None:
        try:
            while True:
                first, body = read_packet(conn)
                ptype = first >> 4
                info: dict = {}
                if ptype == CONNECT:
                    info = decode_connect(body)
                elif ptype == PUBLISH:
                    info = decode_publish(first, body)
                elif ptype == SUBSCRIBE:
                    info = decode_subscribe(body)
                with self.cond:
                    self.packets.append(Packet(number, ptype, first & 0x0F, body, time.monotonic(), info))
                    self.cond.notify_all()
                if ptype == CONNECT:
                    if self.connect_hook:
                        self.connect_hook(number)
                    if not self._answer_connect(conn, number):
                        return
                elif ptype == PINGREQ and self.answer_pings:
                    conn.sendall(bytes([PINGRESP << 4, 0]))
                elif ptype == SUBSCRIBE:
                    conn.sendall(bytes([SUBACK << 4, 3]) + body[:2] + b"\x00")
                elif ptype == DISCONNECT:
                    return
        except (EOFError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _answer_connect(self, conn: socket.socket, number: int) -> bool:
        if self.silent:
            return True
        if self.connack_bytes is not None:
            conn.sendall(self.connack_bytes)
            return True
        rc = self.connack_rcs[number - 1] if number <= len(self.connack_rcs) else 0
        conn.sendall(bytes([CONNACK << 4, 2, 0, rc]))
        return rc == 0  # a refusing broker hangs up

    # --- what a test can do
    def send(self, data: bytes) -> None:
        assert self.conn is not None
        self.conn.sendall(data)

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0) -> None:
        self.send(encode_publish(topic, payload, retain=retain, qos=qos))

    def drop(self) -> None:
        """Hang up on the current client."""
        conn = self.conn
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self) -> None:
        self._closed = True
        try:
            self.listener.close()
        except OSError:
            pass
        for c in list(self._conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    # --- what a test can ask
    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        with self.cond:
            while not predicate():
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
            return True

    def of_type(self, ptype: int, conn: int | None = None) -> list[Packet]:
        with self.cond:
            return [p for p in self.packets if p.type == ptype and (conn is None or p.conn == conn)]

    def publishes(self, topic: str | None = None, conn: int | None = None) -> list[Packet]:
        return [p for p in self.of_type(PUBLISH, conn) if topic is None or p.topic == topic]

    def wait_publishes(self, n: int, topic: str | None = None, conn: int | None = None,
                       timeout: float = 5.0) -> list[Packet]:
        ok = self.wait_for(lambda: len([p for p in self.packets if p.type == PUBLISH
                                        and (topic is None or p.topic == topic)
                                        and (conn is None or p.conn == conn)]) >= n, timeout)
        got = self.publishes(topic, conn)
        if not ok:
            raise AssertionError(f"expected {n} publishes on {topic!r}, saw {len(got)}")
        return got
