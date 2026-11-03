#!/usr/bin/env python3
"""
deck_ha_bridge.py - publish Steam Deck state to Home Assistant over MQTT.

Runs on the Deck as a systemd user service. Standard library only (SteamOS's root
filesystem is read-only and has no pip), so it carries its own small MQTT 3.1.1 client.

Home Assistant entities (created automatically through MQTT discovery, grouped under
one "Steam Deck" device):
  sensor.steam_deck_battery            %      battery level
  sensor.steam_deck_battery_power      W      charge/discharge rate
  sensor.steam_deck_time_remaining     min    estimated runtime left while discharging
  sensor.steam_deck_battery_health     %      full-charge capacity vs design capacity
  binary_sensor.steam_deck_charging
  binary_sensor.steam_deck_plugged_in
  binary_sensor.steam_deck_docked             an external display is connected
  binary_sensor.steam_deck_playing            a game is running
  sensor.steam_deck_game                       name of the running game, "Idle" when none
  sensor.steam_deck_game_started       time   when the running game was launched
  sensor.steam_deck_mode                       Game Mode / Desktop Mode
  sensor.steam_deck_apu_temperature    °C
  sensor.steam_deck_fan_speed          rpm
Every entity goes "unavailable" when the Deck sleeps or leaves the network (MQTT last will).

Config: ~/.config/deck-ha-bridge/config.ini (written by install.sh). Run with --once to
print one reading and exit, which is handy for checking the sensors.
"""

from __future__ import annotations

import argparse
import configparser
import glob
import json
import os
import re
import select
import signal
import socket
import ssl
import struct
import sys
import time
from datetime import datetime, timezone

VERSION = "1.0.0"

# Roots are overridable so the bridge can be tested off-device.
SYS = os.environ.get("DECK_SYSFS_ROOT", "/sys")
PROC = os.environ.get("DECK_PROC_ROOT", "/proc")
STEAM = os.path.expanduser(os.environ.get("DECK_STEAM_ROOT", "~/.local/share/Steam"))
CONFIG_PATH = os.path.expanduser(
    os.environ.get("DECK_HA_CONFIG", "~/.config/deck-ha-bridge/config.ini")
)


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


# =========================================================================== MQTT

class MQTTError(Exception):
    pass


def _enc_str(s: str | bytes) -> bytes:
    b = s.encode("utf-8") if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


def _enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


class MiniMQTT:
    """Just enough MQTT 3.1.1: connect with a last will, QoS 0 publish, subscribe, keepalive."""

    def __init__(self, host, port, client_id, username=None, password=None,
                 use_tls=False, tls_insecure=False, keepalive=30):
        self.host, self.port = host, port
        self.client_id = client_id
        self.username, self.password = username, password
        self.use_tls, self.tls_insecure = use_tls, tls_insecure
        self.keepalive = keepalive
        self.sock: socket.socket | None = None
        self.buf = b""
        self.last_tx = 0.0
        self.last_rx = 0.0
        self.ping_outstanding = False
        self.on_message = None
        self._pid = 0

    # --- connection
    def connect(self, will_topic: str, will_payload: str) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=10)
        if self.use_tls:
            ctx = ssl.create_default_context()
            if self.tls_insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(10)
        self.sock, self.buf = raw, b""

        flags = 0x02 | 0x04 | 0x20  # clean session, will flag, will retain (will QoS 0)
        payload = _enc_str(self.client_id) + _enc_str(will_topic) + _enc_str(will_payload)
        if self.username:
            flags |= 0x80
            payload += _enc_str(self.username)
            if self.password:
                flags |= 0x40
                payload += _enc_str(self.password)
        var = _enc_str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive)
        self._send(0x10, var + payload)

        header, body = self._read_packet(blocking=True)
        if header & 0xF0 != 0x20 or len(body) < 2:
            raise MQTTError("broker did not answer CONNECT")
        rc = body[1]
        if rc:
            reasons = {1: "unsupported protocol", 2: "client id rejected", 3: "broker unavailable",
                       4: "bad username or password", 5: "not authorized"}
            raise MQTTError(f"broker refused connection: {reasons.get(rc, rc)}")
        self.sock.settimeout(None)
        self.last_rx = time.monotonic()
        self.ping_outstanding = False

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def disconnect(self) -> None:
        try:
            self._send(0xE0, b"")
        except OSError:
            pass
        self.close()

    # --- packets
    def _send(self, header: int, body: bytes) -> None:
        if not self.sock:
            raise MQTTError("not connected")
        self.sock.sendall(bytes([header]) + _enc_len(len(body)) + body)
        self.last_tx = time.monotonic()

    def publish(self, topic: str, payload: str | bytes, retain: bool = False) -> None:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        self._send(0x30 | (0x01 if retain else 0), _enc_str(topic) + data)

    def subscribe(self, topic: str) -> None:
        self._pid = self._pid % 65535 + 1
        self._send(0x82, struct.pack("!H", self._pid) + _enc_str(topic) + b"\x00")

    def _try_parse(self):
        if len(self.buf) < 2:
            return None
        mult, length, i = 1, 0, 1
        while True:
            if i >= len(self.buf):
                return None
            byte = self.buf[i]
            length += (byte & 0x7F) * mult
            mult *= 128
            i += 1
            if not byte & 0x80:
                break
            if i > 4:
                raise MQTTError("malformed packet length")
        if len(self.buf) < i + length:
            return None
        header, body = self.buf[0], self.buf[i:i + length]
        self.buf = self.buf[i + length:]
        return header, body

    def _read_packet(self, blocking: bool):
        while True:
            pkt = self._try_parse()
            if pkt:
                self.last_rx = time.monotonic()
                return pkt
            if not blocking:
                return None, None
            chunk = self.sock.recv(4096)
            if not chunk:
                raise MQTTError("connection closed by broker")
            self.buf += chunk

    def loop(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for traffic, handle it, keep the connection alive."""
        pending = isinstance(self.sock, ssl.SSLSocket) and self.sock.pending() > 0
        r = pending or select.select([self.sock], [], [], timeout)[0]
        if r:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise MQTTError("connection closed by broker")
            self.buf += chunk
            while True:
                header, raw = self._read_packet(blocking=False)
                if header is None:
                    break
                ptype = header & 0xF0
                if ptype == 0xD0:
                    self.ping_outstanding = False
                elif ptype == 0x30:
                    tlen = struct.unpack("!H", raw[:2])[0]
                    topic = raw[2:2 + tlen].decode("utf-8", "replace")
                    off = 2 + tlen + (2 if header & 0x06 else 0)
                    if self.on_message:
                        self.on_message(topic, raw[off:].decode("utf-8", "replace"))
        now = time.monotonic()
        if self.ping_outstanding and now - self.last_rx > self.keepalive:
            raise MQTTError("broker stopped responding")
        if not self.ping_outstanding and now - self.last_tx > self.keepalive / 2:
            self._send(0xC0, b"")
            self.ping_outstanding = True


# =========================================================================== Readers

def read(path: str, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def read_num(path: str):
    v = read(path)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def battery_state() -> dict:
    out = {"battery": None, "charging": "OFF", "plugged": "OFF", "power_w": None,
           "time_left_min": None, "health": None}
    bats = sorted(glob.glob(f"{SYS}/class/power_supply/BAT*"))
    for ac in glob.glob(f"{SYS}/class/power_supply/*"):
        if read(f"{ac}/type") == "Mains" and read(f"{ac}/online") == "1":
            out["plugged"] = "ON"
    if not bats:
        return out
    b = bats[0]
    cap = read_num(f"{b}/capacity")
    status = read(f"{b}/status", "Unknown")
    out["battery"] = int(cap) if cap is not None else None
    out["charging"] = "ON" if status == "Charging" else "OFF"

    # Power: power_now (µW) if present, else current_now (µA) × voltage_now (µV).
    p = read_num(f"{b}/power_now")
    cur = read_num(f"{b}/current_now")
    volt = read_num(f"{b}/voltage_now")
    if p is None and cur is not None and volt is not None:
        p = abs(cur) * volt / 1e6
    if p is not None:
        out["power_w"] = round(p / 1e6, 1)

    # Remaining runtime: charge (µAh / µA) or energy (µWh / µW).
    if status == "Discharging":
        cn, en = read_num(f"{b}/charge_now"), read_num(f"{b}/energy_now")
        hours = None
        if cn is not None and cur:
            hours = cn / abs(cur)
        elif en is not None and p:
            hours = en / p
        if hours is not None and 0 < hours < 48:
            out["time_left_min"] = int(hours * 60)

    full = read_num(f"{b}/charge_full") or read_num(f"{b}/energy_full")
    design = read_num(f"{b}/charge_full_design") or read_num(f"{b}/energy_full_design")
    if full and design:
        out["health"] = round(100 * full / design, 1)
    return out


def docked() -> str:
    for status in glob.glob(f"{SYS}/class/drm/card*-*/status"):
        conn = os.path.basename(os.path.dirname(status))
        if "eDP" in conn:  # the built-in screen
            continue
        if read(status) == "connected":
            return "ON"
    return "OFF"


def hwmon_values() -> dict:
    out = {"apu_temp": None, "fan_rpm": None}
    for hw in sorted(glob.glob(f"{SYS}/class/hwmon/hwmon*")):
        name = read(f"{hw}/name", "")
        if out["apu_temp"] is None and name in ("amdgpu", "k10temp"):
            t = read_num(f"{hw}/temp1_input")
            if t is not None:
                out["apu_temp"] = round(t / 1000, 1)
        if out["fan_rpm"] is None:
            f = read_num(f"{hw}/fan1_input")
            if f is not None:
                out["fan_rpm"] = int(f)
    return out


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _boot_time() -> float | None:
    for line in (read(f"{PROC}/stat", "") or "").splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return None


def _proc_start(pid: str) -> float | None:
    stat = read(f"{PROC}/{pid}/stat")
    bt = _boot_time()
    if not stat or bt is None:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    try:
        return bt + int(fields[19]) / _CLK_TCK  # field 22 overall: starttime
    except (IndexError, ValueError):
        return None


def scan_processes() -> dict:
    """Find the running Steam game (via Steam's `reaper SteamLaunch AppId=N` wrapper) and the session."""
    game_appid, game_pid, mode = None, None, None
    for d in os.listdir(PROC):
        if not d.isdigit():
            continue
        comm = read(f"{PROC}/{d}/comm", "")
        if comm.startswith("gamescope"):
            mode = "Game Mode"
        elif comm == "plasmashell" and mode is None:
            mode = "Desktop Mode"
        if comm != "reaper":
            continue
        try:
            with open(f"{PROC}/{d}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if b"SteamLaunch" not in args:
            continue
        for a in args:
            if a.startswith(b"AppId="):
                try:
                    appid = int(a[6:])
                except ValueError:
                    continue
                if appid and (game_pid is None or int(d) < int(game_pid)):
                    game_appid, game_pid = appid, d
    started = _proc_start(game_pid) if game_pid else None
    return {"appid": game_appid, "started": started, "mode": mode or "Unknown"}


class GameNames:
    """Resolve app ids to names from Steam's app manifests and non-Steam shortcuts."""

    def __init__(self):
        self.cache: dict[int, str] = {}
        self.shortcuts: dict[int, str] | None = None

    def library_dirs(self) -> list[str]:
        dirs = [os.path.join(STEAM, "steamapps")]
        vdf = read(os.path.join(STEAM, "steamapps", "libraryfolders.vdf"), "") or ""
        for m in re.finditer(r'"path"\s+"([^"]+)"', vdf):
            dirs.append(os.path.join(m.group(1).replace("\\\\", "\\"), "steamapps"))
        return list(dict.fromkeys(dirs))

    def load_shortcuts(self) -> dict[int, str]:
        """Parse userdata/*/config/shortcuts.vdf (binary VDF) for appid -> AppName."""
        found: dict[int, str] = {}
        for path in glob.glob(os.path.join(STEAM, "userdata", "*", "config", "shortcuts.vdf")):
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            # Each shortcut is a map; split on the "\x00<index>\x00" map openers is fragile,
            # so walk key by key and pair each appid with the AppName in the same entry.
            appid, name = None, None
            for m in re.finditer(rb"\x02appid\x00(.{4})|\x01appname\x00([^\x00]*)\x00", data,
                                 re.IGNORECASE | re.DOTALL):
                if m.group(1) is not None:
                    appid = struct.unpack("<I", m.group(1))[0]
                else:
                    name = m.group(2).decode("utf-8", "replace")
                if appid is not None and name is not None:
                    found[appid] = name
                    appid, name = None, None
        return found

    def name(self, appid: int) -> str:
        if appid in self.cache:
            return self.cache[appid]
        for d in self.library_dirs():
            acf = read(os.path.join(d, f"appmanifest_{appid}.acf"))
            if acf:
                m = re.search(r'"name"\s+"([^"]+)"', acf)
                if m:
                    self.cache[appid] = m.group(1)
                    return m.group(1)
        if self.shortcuts is None or appid not in self.shortcuts:
            self.shortcuts = self.load_shortcuts()
        if appid in self.shortcuts:
            self.cache[appid] = self.shortcuts[appid]
            return self.shortcuts[appid]
        return f"App {appid}" if appid < 2**31 else "Non-Steam game"


def snapshot(names: GameNames) -> dict:
    s = battery_state()
    s["docked"] = docked()
    s.update(hwmon_values())
    p = scan_processes()
    s["mode"] = p["mode"]
    if p["appid"]:
        s["playing"] = "ON"
        s["appid"] = p["appid"]
        s["game"] = names.name(p["appid"])
        s["game_started"] = (datetime.fromtimestamp(p["started"], timezone.utc).isoformat(timespec="seconds")
                             if p["started"] else None)
        s["store_url"] = f"https://store.steampowered.com/app/{p['appid']}/" if p["appid"] < 2**31 else None
    else:
        s.update(playing="OFF", appid=None, game="Idle", game_started=None, store_url=None)
    return s


# =========================================================================== Home Assistant

def entity_defs() -> list[tuple[str, str, dict]]:
    """(component, object_id, extra discovery fields)."""
    vj = lambda k: "{{ value_json.%s }}" % k
    num = lambda k: "{{ value_json.%s if value_json.%s is not none else 'None' }}" % (k, k)
    return [
        ("sensor", "battery", {"name": "Battery", "device_class": "battery", "unit_of_measurement": "%",
                               "state_class": "measurement", "value_template": num("battery")}),
        ("sensor", "battery_power", {"name": "Battery power", "device_class": "power", "unit_of_measurement": "W",
                                     "state_class": "measurement", "value_template": num("power_w")}),
        ("sensor", "time_remaining", {"name": "Time remaining", "device_class": "duration",
                                      "unit_of_measurement": "min", "icon": "mdi:timer-sand",
                                      "value_template": num("time_left_min")}),
        ("sensor", "battery_health", {"name": "Battery health", "unit_of_measurement": "%",
                                      "icon": "mdi:battery-heart-variant", "entity_category": "diagnostic",
                                      "value_template": num("health")}),
        ("binary_sensor", "charging", {"name": "Charging", "device_class": "battery_charging",
                                       "value_template": vj("charging")}),
        ("binary_sensor", "plugged_in", {"name": "Plugged in", "device_class": "plug",
                                         "value_template": vj("plugged")}),
        ("binary_sensor", "docked", {"name": "Docked", "icon": "mdi:monitor", "value_template": vj("docked")}),
        ("binary_sensor", "playing", {"name": "Playing", "device_class": "running",
                                      "value_template": vj("playing")}),
        ("sensor", "game", {"name": "Game", "icon": "mdi:gamepad-variant", "value_template": vj("game"),
                            "json_attributes_template":
                                "{{ {'appid': value_json.appid, 'store_url': value_json.store_url} | tojson }}"}),
        ("sensor", "game_started", {"name": "Game started", "device_class": "timestamp",
                                    "value_template": num("game_started")}),
        ("sensor", "mode", {"name": "Mode", "icon": "mdi:steam", "value_template": vj("mode")}),
        ("sensor", "apu_temperature", {"name": "APU temperature", "device_class": "temperature",
                                       "unit_of_measurement": "°C", "state_class": "measurement",
                                       "entity_category": "diagnostic", "value_template": num("apu_temp")}),
        ("sensor", "fan_speed", {"name": "Fan speed", "unit_of_measurement": "rpm", "icon": "mdi:fan",
                                 "state_class": "measurement", "entity_category": "diagnostic",
                                 "value_template": num("fan_rpm")}),
    ]


class Bridge:
    def __init__(self, cfg: configparser.ConfigParser):
        m = cfg["mqtt"]
        d = cfg["device"] if cfg.has_section("device") else {}
        self.node = re.sub(r"[^a-zA-Z0-9_-]", "_", d.get("node_id", "steamdeck"))
        self.device_name = d.get("name", "Steam Deck")
        self.prefix = m.get("discovery_prefix", "homeassistant")
        self.base = f"deck-ha-bridge/{self.node}"
        self.state_topic = f"{self.base}/state"
        self.avail_topic = f"{self.base}/availability"
        self.poll = float(d.get("poll_seconds", "5"))
        self.heartbeat = float(d.get("heartbeat_seconds", "60"))
        self.client = MiniMQTT(
            m.get("host"), int(m.get("port", "1883")), f"deck-ha-bridge-{self.node}",
            m.get("username") or None, m.get("password") or None,
            use_tls=m.getboolean("tls", fallback=False),
            tls_insecure=m.getboolean("tls_insecure", fallback=False),
        )
        self.client.on_message = self.on_message
        self.names = GameNames()
        self.last_state: dict | None = None
        self.last_pub = 0.0
        self.need_discovery = True
        self.stopping = False

    def device(self) -> dict:
        return {"identifiers": [f"deck-ha-bridge-{self.node}"], "name": self.device_name,
                "manufacturer": "Valve", "model": "Steam Deck", "sw_version": f"deck-ha-bridge {VERSION}"}

    def publish_discovery(self) -> None:
        for comp, oid, extra in entity_defs():
            cfg = {
                "unique_id": f"{self.node}_{oid}",
                "state_topic": self.state_topic,
                "availability_topic": self.avail_topic,
                "device": self.device(),
                **extra,
            }
            if "json_attributes_template" in cfg:
                cfg["json_attributes_topic"] = self.state_topic
            if comp == "binary_sensor":
                cfg.update(payload_on="ON", payload_off="OFF")
            self.client.publish(f"{self.prefix}/{comp}/{self.node}/{oid}/config", json.dumps(cfg), retain=True)
        self.need_discovery = False
        log(f"published discovery for {len(entity_defs())} entities")

    def on_message(self, topic: str, payload: str) -> None:
        if topic == f"{self.prefix}/status" and payload == "online":
            log("Home Assistant restarted; republishing discovery and state")
            self.need_discovery = True
            self.last_state = None

    @staticmethod
    def significant(a: dict | None, b: dict) -> bool:
        if a is None:
            return True
        for k, v in b.items():
            old = a.get(k)
            if k in ("power_w",) and old is not None and v is not None:
                if abs(old - v) >= 1.0:
                    return True
            elif k in ("apu_temp",) and old is not None and v is not None:
                if abs(old - v) >= 3:
                    return True
            elif k in ("fan_rpm",) and old is not None and v is not None:
                if abs(old - v) >= 500:
                    return True
            elif k == "time_left_min" and old is not None and v is not None:
                if abs(old - v) >= 5:
                    return True
            elif old != v:
                return True
        return False

    def tick(self) -> None:
        s = snapshot(self.names)
        now = time.monotonic()
        if self.significant(self.last_state, s) or now - self.last_pub >= self.heartbeat:
            if (self.last_state or {}).get("game") != s["game"]:
                log(f"game: {s['game']}")
            self.client.publish(self.state_topic, json.dumps(s), retain=True)
            self.last_state, self.last_pub = s, now

    def run(self) -> None:
        backoff = 2
        while not self.stopping:
            try:
                self.client.connect(self.avail_topic, "offline")
                log(f"connected to {self.client.host}:{self.client.port}")
                backoff = 2
                self.client.subscribe(f"{self.prefix}/status")
                self.client.publish(self.avail_topic, "online", retain=True)
                self.need_discovery, self.last_state = True, None
                next_poll = 0.0
                while not self.stopping:
                    if self.need_discovery:
                        self.publish_discovery()
                        self.client.publish(self.avail_topic, "online", retain=True)
                    now = time.monotonic()
                    if now >= next_poll:
                        self.tick()
                        next_poll = now + self.poll
                    self.client.loop(max(0.1, min(1.0, next_poll - time.monotonic())))
            except (OSError, MQTTError) as e:
                if self.stopping:
                    break
                log(f"MQTT: {e}; retrying in {backoff}s")
                self.client.close()
                slept = 0.0
                while slept < backoff and not self.stopping:
                    time.sleep(0.5)
                    slept += 0.5
                backoff = min(backoff * 2, 60)
        # Graceful exit: mark unavailable ourselves (the last will only fires on unclean drops).
        try:
            self.client.publish(self.avail_topic, "offline", retain=True)
            self.client.disconnect()
        except (OSError, MQTTError):
            self.client.close()
        log("stopped")

    def stop(self, *_):
        self.stopping = True


def main() -> None:
    ap = argparse.ArgumentParser(description="Publish Steam Deck state to Home Assistant over MQTT.")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--once", action="store_true", help="Print one reading as JSON and exit")
    args = ap.parse_args()

    if args.once:
        print(json.dumps(snapshot(GameNames()), indent=2))
        return

    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(args.config) or not cfg.has_option("mqtt", "host"):
        raise SystemExit(f"Missing config: {args.config} needs an [mqtt] section with host=")
    bridge = Bridge(cfg)
    signal.signal(signal.SIGTERM, bridge.stop)
    signal.signal(signal.SIGINT, bridge.stop)
    bridge.run()


if __name__ == "__main__":
    main()
