"""Bridge logic: what is worth publishing, the heartbeat, discovery, Home Assistant's birth message,
and (against the fake broker) availability, last will, reconnects and the command line."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time as real_time
import unittest
from pathlib import Path
from unittest import mock

from tests import fake_broker as fb
from tests import scenarios
from tests.support import FIXTURES, ROOT, DeckTestCase, FakeTime, RecordingClient, dhb, make_cfg

STATE_TOPIC = "deck-ha-bridge/steamdeck/state"
AVAIL_TOPIC = "deck-ha-bridge/steamdeck/availability"


class Significant(unittest.TestCase):
    BASE = {"battery": 80, "charging": "OFF", "plugged": "OFF", "power_w": 5.0, "time_left_min": 100,
            "health": 96.0, "docked": "OFF", "apu_temp": 40.0, "fan_rpm": 3000, "mode": "Game Mode",
            "playing": "OFF", "appid": None, "game": "Idle", "game_started": None, "store_url": None}

    def sig(self, **changes) -> bool:
        return dhb.Bridge.significant(dict(self.BASE), dict(self.BASE, **changes))

    def test_the_first_reading_is_always_published(self):
        self.assertTrue(dhb.Bridge.significant(None, dict(self.BASE)))

    def test_an_identical_reading_is_not(self):
        self.assertFalse(self.sig())

    def test_any_change_to_a_plain_field_is_significant(self):
        changes = {"battery": 79, "charging": "ON", "plugged": "ON", "health": 95.9, "docked": "ON",
                   "mode": "Desktop Mode", "playing": "ON", "appid": 367520, "game": "Hollow Knight",
                   "game_started": "2023-11-14T22:33:54+00:00", "store_url": "https://x/"}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.assertTrue(self.sig(**{key: value}))

    def test_numeric_fields_need_to_move_by_their_threshold(self):
        # (field, new value, significant?) against the BASE values above.
        cases = [("power_w", 5.9, False), ("power_w", 4.1, False), ("power_w", 6.0, True), ("power_w", 4.0, True),
                 ("apu_temp", 42.9, False), ("apu_temp", 37.1, False), ("apu_temp", 43.0, True),
                 ("apu_temp", 37.0, True),
                 ("fan_rpm", 3499, False), ("fan_rpm", 2501, False), ("fan_rpm", 3500, True), ("fan_rpm", 2500, True),
                 ("time_left_min", 104, False), ("time_left_min", 96, False), ("time_left_min", 105, True),
                 ("time_left_min", 95, True)]
        for key, value, expected in cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.sig(**{key: value}), expected)

    def test_a_numeric_field_appearing_or_disappearing_is_significant(self):
        for key in ("power_w", "apu_temp", "fan_rpm", "time_left_min"):
            with self.subTest(key=key, direction="lost"):
                self.assertTrue(self.sig(**{key: None}))
            with self.subTest(key=key, direction="found"):
                old = dict(self.BASE, **{key: None})
                self.assertTrue(dhb.Bridge.significant(old, dict(self.BASE)))

    def test_small_moves_in_every_numeric_field_at_once_are_still_not_significant(self):
        self.assertFalse(self.sig(power_w=5.5, apu_temp=41.0, fan_rpm=3100, time_left_min=102))

    def test_a_small_move_does_not_hide_a_real_change_next_to_it(self):
        self.assertTrue(self.sig(power_w=5.5, battery=79))


class Config(unittest.TestCase):
    def test_defaults_when_there_is_no_device_section(self):
        b = dhb.Bridge(make_cfg())
        self.assertEqual((b.node, b.device_name, b.prefix), ("steamdeck", "Steam Deck", "homeassistant"))
        self.assertEqual((b.base, b.state_topic, b.avail_topic),
                         ("deck-ha-bridge/steamdeck", STATE_TOPIC, AVAIL_TOPIC))
        self.assertEqual((b.poll, b.heartbeat), (5.0, 60.0))
        c = b.client
        self.assertEqual((c.host, c.port, c.client_id, c.username, c.password, c.use_tls, c.tls_insecure),
                         ("broker.example.com", 1883, "deck-ha-bridge-steamdeck", None, None, False, False))

    def test_every_setting_is_read(self):
        b = dhb.Bridge(make_cfg(
            mqtt={"port": "8883", "username": "deck", "password": "pw", "tls": "true", "tls_insecure": "yes",
                  "discovery_prefix": "ha"},
            device={"name": "Sam's Deck", "node_id": "sam", "poll_seconds": "2", "heartbeat_seconds": "30"}))
        c = b.client
        self.assertEqual((c.port, c.username, c.password, c.use_tls, c.tls_insecure),
                         (8883, "deck", "pw", True, True))
        self.assertEqual((b.prefix, b.device_name, b.node, b.poll, b.heartbeat), ("ha", "Sam's Deck", "sam", 2.0, 30.0))
        self.assertEqual(c.client_id, "deck-ha-bridge-sam")

    def test_the_node_id_is_made_safe_for_topics(self):
        for raw, safe in (("My Deck!", "My_Deck_"), ("a.b/c#d+e", "a_b_c_d_e"), ("ok-1_x", "ok-1_x")):
            with self.subTest(raw=raw):
                self.assertEqual(dhb.Bridge(make_cfg(device={"node_id": raw})).node, safe)

    def test_blank_credentials_mean_none(self):
        c = dhb.Bridge(make_cfg(mqtt={"username": "", "password": ""})).client
        self.assertEqual((c.username, c.password), (None, None))


class Tick(DeckTestCase):
    """tick() decides when to publish. A hand-moved clock stands in for time, so no test sleeps."""

    def setUp(self):
        super().setUp()
        self.clock = FakeTime()
        self.logs: list[str] = []
        for patcher in (mock.patch.object(dhb, "time", self.clock), mock.patch.object(dhb, "log", self.logs.append)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.battery = self.deck.battery(status="Discharging", capacity=80, power_now=5_000_000)
        self.bridge = dhb.Bridge(make_cfg(device={"poll_seconds": "5", "heartbeat_seconds": "60"}))
        self.bridge.client = RecordingClient()

    @property
    def published(self):
        return self.bridge.client.published

    def set_battery(self, **files):
        for name, value in files.items():
            (self.battery / name).write_text(f"{value}\n")

    def test_the_first_tick_publishes_the_state_retained(self):
        self.bridge.tick()
        self.assertEqual(len(self.published), 1)
        topic, payload, retain = self.published[0]
        self.assertEqual((topic, retain), (STATE_TOPIC, True))
        self.assertEqual(json.loads(payload), dhb.snapshot(self.names))
        self.assertEqual(json.loads(payload)["battery"], 80)

    def test_an_unchanged_state_is_not_republished_before_the_heartbeat(self):
        self.bridge.tick()
        self.clock.now += 59.9
        self.bridge.tick()
        self.assertEqual(len(self.published), 1)

    def test_the_heartbeat_republishes_an_unchanged_state(self):
        self.bridge.tick()
        self.clock.now += 60.0
        self.bridge.tick()
        self.assertEqual(len(self.published), 2)
        self.assertEqual(self.published[0][1], self.published[1][1])

    def test_the_heartbeat_counts_from_the_last_publish_not_the_first(self):
        self.bridge.tick()  # t=0
        self.clock.now += 30
        self.set_battery(capacity=79)
        self.bridge.tick()  # t=30, a change
        self.assertEqual(len(self.published), 2)
        self.clock.now += 59
        self.bridge.tick()  # t=89, 59 s after the last publish
        self.assertEqual(len(self.published), 2)
        self.clock.now += 1
        self.bridge.tick()  # t=90
        self.assertEqual(len(self.published), 3)

    def test_a_real_change_is_published_at_once(self):
        self.bridge.tick()
        self.set_battery(capacity=79)
        self.bridge.tick()
        self.assertEqual(json.loads(self.published[-1][1])["battery"], 79)

    def test_a_change_below_the_threshold_waits_for_the_heartbeat(self):
        self.bridge.tick()
        self.set_battery(power_now=5_500_000)  # +0.5 W
        self.bridge.tick()
        self.assertEqual(len(self.published), 1)
        self.set_battery(power_now=6_000_000)  # +1.0 W against the last published value
        self.bridge.tick()
        self.assertEqual(len(self.published), 2)

    def test_forgetting_the_last_state_forces_a_publish(self):
        self.bridge.tick()
        self.bridge.last_state = None  # what a reconnect or Home Assistant's birth message does
        self.bridge.tick()
        self.assertEqual(len(self.published), 2)

    def test_the_game_is_logged_when_it_differs_from_the_last_published_state(self):
        self.deck.boot_time()
        self.deck.manifest(367520, "Hollow Knight")
        self.bridge.tick()
        self.deck.reaper(300, 367520, start_ticks=100)
        self.bridge.tick()
        self.bridge.tick()  # same game, nothing significant: neither published nor logged
        self.bridge.last_state = None  # after a reconnect the game is logged again, as the code does today
        self.bridge.tick()
        self.assertEqual([m for m in self.logs if m.startswith("game:")],
                         ["game: Idle", "game: Hollow Knight", "game: Hollow Knight"])


class Discovery(unittest.TestCase):
    def setUp(self):
        self.logs: list[str] = []
        patcher = mock.patch.object(dhb, "log", self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def published(self, **kw):
        bridge = dhb.Bridge(make_cfg(**kw))
        bridge.client = RecordingClient()
        bridge.publish_discovery()
        return bridge, bridge.client.published

    def test_matches_the_golden_file(self):
        _, pubs = self.published()
        golden = json.loads((FIXTURES / "discovery_golden.json").read_text(encoding="utf-8"))
        self.assertEqual({t: json.loads(p) for t, p, _ in pubs}, golden,
                         "discovery changed; if on purpose, run python3 tests/regen_fixtures.py and review the diff")

    def test_thirteen_retained_messages_on_homeassistants_topic_layout(self):
        bridge, pubs = self.published()
        self.assertEqual(len(pubs), 13)
        self.assertTrue(all(retain for _, _, retain in pubs))
        for topic, payload, _ in pubs:
            cfg = json.loads(payload)
            comp, oid = re.fullmatch(r"homeassistant/(sensor|binary_sensor)/steamdeck/(\w+)/config", topic).groups()
            self.assertEqual(cfg["unique_id"], f"steamdeck_{oid}")
            self.assertEqual((cfg["state_topic"], cfg["availability_topic"]), (STATE_TOPIC, AVAIL_TOPIC))
            self.assertEqual(cfg["device"]["identifiers"], ["deck-ha-bridge-steamdeck"])
            self.assertEqual(("payload_on" in cfg, "payload_off" in cfg), (comp == "binary_sensor",) * 2)
        self.assertFalse(bridge.need_discovery)
        self.assertIn("published discovery for 13 entities", self.logs)

    def test_only_the_game_sensor_carries_attributes(self):
        _, pubs = self.published()
        with_attrs = [t for t, p, _ in pubs if "json_attributes_topic" in json.loads(p)]
        self.assertEqual(with_attrs, ["homeassistant/sensor/steamdeck/game/config"])

    def test_a_custom_prefix_node_and_name_move_everything_together(self):
        _, pubs = self.published(mqtt={"discovery_prefix": "ha"}, device={"node_id": "sam", "name": "Sam's Deck"})
        for topic, payload, _ in pubs:
            cfg = json.loads(payload)
            self.assertRegex(topic, r"^ha/(sensor|binary_sensor)/sam/\w+/config$")
            self.assertTrue(cfg["unique_id"].startswith("sam_"))
            self.assertEqual(cfg["state_topic"], "deck-ha-bridge/sam/state")
            self.assertEqual(cfg["availability_topic"], "deck-ha-bridge/sam/availability")
            self.assertEqual((cfg["device"]["name"], cfg["device"]["identifiers"]), ("Sam's Deck", ["deck-ha-bridge-sam"]))

    @staticmethod
    def entity_ids(device_name: str) -> set[str]:
        """Home Assistant builds entity ids from the device name and the entity's name."""
        slug = lambda s: re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")  # noqa: E731
        return {f"{comp}.{slug(device_name + ' ' + extra['name'])}" for comp, _, extra in dhb.entity_defs()}

    def test_default_entity_ids_are_the_ones_the_docs_and_examples_use(self):
        ids = self.entity_ids("Steam Deck")
        pattern = r"\b(?:binary_sensor|sensor)\.steam_deck_[a-z_]+"
        self.assertEqual(set(re.findall(pattern, dhb.__doc__)), ids, "module docstring")
        used = set(re.findall(pattern, (ROOT / "examples" / "ha_automations.yaml").read_text()))
        self.assertTrue(used, "the example automations should mention entities")
        self.assertLessEqual(used, ids, "the example automations use an entity the bridge does not create")


class HomeAssistantBirth(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(dhb, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def bridge(self, **mqtt):
        b = dhb.Bridge(make_cfg(mqtt=mqtt))
        b.need_discovery, b.last_state = False, {"battery": 50}
        return b

    def test_online_on_the_status_topic_republishes_everything(self):
        b = self.bridge()
        b.on_message("homeassistant/status", "online")
        self.assertEqual((b.need_discovery, b.last_state), (True, None))

    def test_anything_else_is_ignored(self):
        for topic, payload in (("homeassistant/status", "offline"), ("homeassistant/status", ""),
                               ("homeassistant/other", "online"), ("other/status", "online")):
            with self.subTest(topic=topic, payload=payload):
                b = self.bridge()
                b.on_message(topic, payload)
                self.assertEqual((b.need_discovery, b.last_state), (False, {"battery": 50}))

    def test_a_custom_discovery_prefix_is_honoured(self):
        b = self.bridge(discovery_prefix="ha")
        b.on_message("homeassistant/status", "online")
        self.assertFalse(b.need_discovery)
        b.on_message("ha/status", "online")
        self.assertTrue(b.need_discovery)


class FastTime:
    """Real clock for the broker conversation, but backoff sleeps cost a millisecond and are counted."""

    def __init__(self):
        self.virtual = 0.0

    monotonic = staticmethod(real_time.monotonic)

    def sleep(self, seconds: float) -> None:
        self.virtual += seconds
        real_time.sleep(0.001)


class Wire(DeckTestCase):
    """Bridge.run() in a thread, talking to the fake broker."""

    def setUp(self):
        super().setUp()
        scenarios.playing(self.deck)
        self.logs: list[str] = []
        self.fast = FastTime()
        for patcher in (mock.patch.object(dhb, "time", self.fast), mock.patch.object(dhb, "log", self.logs.append)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge = self.thread = None

    def broker(self, **kw) -> fb.FakeBroker:
        b = fb.FakeBroker(**kw)
        self.addCleanup(b.close)
        self.b = b
        return b

    def start(self, port: int | None = None, **device) -> None:
        cfg = make_cfg(mqtt={"host": "127.0.0.1", "port": str(port or self.b.port)},
                       device={"poll_seconds": "0.05", "heartbeat_seconds": "60", **device})
        self.bridge = dhb.Bridge(cfg)
        self.thread = threading.Thread(target=self.bridge.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self) -> None:
        if self.bridge:
            self.bridge.stop()
            self.thread.join(5)

    def configs(self, conn=None):
        return [p for p in self.b.publishes(conn=conn) if p.topic.endswith("/config")]

    def test_connect_sets_the_will_and_announces_availability_then_discovery_then_state(self):
        self.broker()
        self.start()
        pubs = self.b.wait_publishes(1, topic=STATE_TOPIC)
        connect = self.b.of_type(fb.CONNECT)[0].info
        self.assertEqual((connect["client_id"], connect["keepalive"], connect["clean_session"]),
                         ("deck-ha-bridge-steamdeck", 30, True))
        self.assertEqual((connect["will_topic"], connect["will_payload"], connect["will_retain"], connect["will_qos"]),
                         (AVAIL_TOPIC, "offline", True, 0))
        self.assertEqual(self.b.of_type(fb.SUBSCRIBE)[0].info["filters"], [("homeassistant/status", 0)])
        first = pubs and self.b.publishes()[0]
        self.assertEqual((first.topic, first.text, first.info["retain"]), (AVAIL_TOPIC, "online", True))
        topics = [p.topic for p in self.b.publishes()]
        last_config = max(i for i, t in enumerate(topics) if t.endswith("/config"))
        self.assertLess(last_config, topics.index(STATE_TOPIC), "state published before discovery finished")
        self.assertEqual(len(self.configs()), 13)
        self.assertTrue(all(p.info["retain"] for p in self.b.publishes()))

    def test_the_state_on_the_wire_is_the_fixture(self):
        self.broker()
        self.start()
        state = self.b.wait_publishes(1, topic=STATE_TOPIC)[0]
        self.assertEqual(json.loads(state.text), json.loads((FIXTURES / "state_playing.json").read_text()))

    def test_home_assistants_birth_message_republishes_discovery_and_state(self):
        self.broker()
        self.start()
        self.b.wait_publishes(1, topic=STATE_TOPIC)
        self.b.publish("homeassistant/status", "online")
        self.b.wait_publishes(2, topic=STATE_TOPIC)
        self.assertTrue(self.b.wait_for(lambda: len(self.configs()) == 26))
        # "online" follows the connect and again each discovery round (connect round + birth round)
        self.assertEqual(len(self.b.publishes(AVAIL_TOPIC)), 3)
        self.assertTrue(all(p.text == "online" for p in self.b.publishes(AVAIL_TOPIC)))

    def test_other_status_messages_change_nothing(self):
        self.broker()
        self.start()
        self.b.wait_publishes(1, topic=STATE_TOPIC)
        before = len(self.b.publishes())
        self.b.publish("homeassistant/status", "offline")
        self.assertFalse(self.b.wait_for(lambda: len(self.b.publishes()) > before, timeout=0.5))

    def test_the_heartbeat_republishes_an_unchanged_state(self):
        self.broker()
        self.start(heartbeat_seconds="0.3")
        first, second = self.b.wait_publishes(2, topic=STATE_TOPIC)[:2]
        self.assertGreaterEqual(second.t - first.t, 0.15)  # heartbeat is 0.3 s; allow scheduling jitter
        self.assertEqual(first.text, second.text)

    def test_a_change_on_the_deck_is_published_without_waiting_for_the_heartbeat(self):
        self.broker()
        self.start()
        self.b.wait_publishes(1, topic=STATE_TOPIC)
        (self.deck.sys / "class/power_supply/BAT1/capacity").write_text("61\n")
        second = self.b.wait_publishes(2, topic=STATE_TOPIC)[1]
        self.assertEqual(json.loads(second.text)["battery"], 61)

    def test_stopping_marks_the_deck_offline_and_says_goodbye(self):
        self.broker()
        self.start()
        self.b.wait_publishes(1, topic=STATE_TOPIC)
        self.stop()
        self.assertFalse(self.thread.is_alive())
        last = self.b.publishes(AVAIL_TOPIC)[-1]
        self.assertEqual((last.text, last.info["retain"]), ("offline", True))
        self.assertTrue(self.b.wait_for(lambda: self.b.of_type(fb.DISCONNECT)))
        self.assertIn("stopped", self.logs)

    def test_a_dropped_connection_is_re_established_from_scratch(self):
        self.broker()
        self.start()
        self.b.wait_publishes(1, topic=STATE_TOPIC, conn=1)
        self.b.drop()
        self.b.wait_publishes(1, topic=STATE_TOPIC, conn=2)
        self.assertEqual(self.b.connections, 2)
        again = self.b.of_type(fb.CONNECT, conn=2)[0].info
        self.assertEqual((again["will_topic"], again["will_payload"]), (AVAIL_TOPIC, "offline"))
        self.assertEqual(self.b.of_type(fb.SUBSCRIBE, conn=2)[0].info["filters"], [("homeassistant/status", 0)])
        self.assertEqual(len(self.configs(conn=2)), 13)
        self.assertEqual(self.b.publishes(AVAIL_TOPIC, conn=2)[0].text, "online")
        self.assertIn("MQTT: connection closed by broker; retrying in 2s", self.logs)

    def test_refused_connections_back_off_and_the_delay_resets_after_a_success(self):
        self.broker(connack_rcs=[5] * 7)  # the eighth connection is accepted
        attempts: list[float] = []
        self.b.connect_hook = lambda n: attempts.append(self.fast.virtual)
        self.start()
        self.b.wait_publishes(1, topic=STATE_TOPIC, conn=8)
        self.b.drop()
        self.assertTrue(self.b.wait_for(lambda: self.b.connections >= 9))
        self.assertTrue(self.b.wait_for(lambda: len(attempts) >= 9))
        gaps = [b - a for a, b in zip(attempts, attempts[1:9])]
        self.assertEqual(gaps, [2, 4, 8, 16, 32, 60, 60, 2])
        refusals = [m for m in self.logs if "refused" in m]
        self.assertEqual(refusals[:7], [f"MQTT: broker refused connection: not authorized; retrying in {n}s"
                                        for n in (2, 4, 8, 16, 32, 60, 60)])

    def test_stopping_while_waiting_to_reconnect_exits_promptly(self):
        self.broker(connack_rcs=[5] * 1000)
        self.start()
        self.assertTrue(self.b.wait_for(lambda: self.b.connections >= 1))
        self.stop()
        self.assertFalse(self.thread.is_alive())
        self.assertIn("stopped", self.logs)

    def test_a_broker_that_is_down_is_retried(self):
        import socket
        with socket.socket() as probe:  # a port that was just free: nothing listens on it
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.start(port=port)
        deadline = real_time.monotonic() + 5
        while not any(m.startswith("MQTT:") for m in self.logs) and real_time.monotonic() < deadline:
            real_time.sleep(0.01)
        self.assertTrue(any(m.startswith("MQTT:") and "retrying in 2s" in m for m in self.logs), self.logs)


class CommandLine(DeckTestCase):
    """The script run as a program, steered by the same environment variables a person would use."""

    def run_script(self, *args, extra_env=None):
        env = {**os.environ, **self.deck.env, "HOME": str(self.deck.base / "home"), **(extra_env or {})}
        return subprocess.run([sys.executable, str(ROOT / "deck_ha_bridge.py"), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_once_prints_the_state_as_json(self):
        scenarios.playing(self.deck)
        p = self.run_script("--once")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), json.loads((FIXTURES / "state_playing.json").read_text()))

    def test_a_missing_config_is_a_clear_error(self):
        missing = self.deck.base / "nope.ini"
        p = self.run_script("--config", str(missing))
        self.assertEqual(p.returncode, 1)
        self.assertIn(f"Missing config: {missing} needs an [mqtt] section with host=", p.stderr)

    def test_a_config_without_a_broker_host_is_a_clear_error(self):
        cfg = self.deck.base / "config.ini"
        cfg.write_text("[mqtt]\nport = 1883\n")
        p = self.run_script("--config", str(cfg))
        self.assertEqual(p.returncode, 1)
        self.assertIn("needs an [mqtt] section with host=", p.stderr)


if __name__ == "__main__":
    unittest.main()
