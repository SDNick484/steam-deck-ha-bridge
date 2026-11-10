"""The retained state topic is a contract: another project subscribes to it.

These tests fail when a field is renamed, removed, added without being documented, or changes type.
docs/state-schema.md describes the same contract in prose; the checks below keep the two in step.
"""
from __future__ import annotations

import json
import re
import unittest

from tests import scenarios
from tests.support import FIXTURES, ROOT, dhb

# field -> (allowed JSON types, values when restricted). Order is the order the bridge publishes.
SCHEMA = {
    "battery": (int, None), "charging": (str, {"ON", "OFF"}), "plugged": (str, {"ON", "OFF"}),
    "power_w": (float, None), "time_left_min": (int, None), "health": (float, None),
    "docked": (str, {"ON", "OFF"}), "apu_temp": (float, None), "fan_rpm": (int, None),
    "mode": (str, {"Game Mode", "Desktop Mode", "Unknown"}), "playing": (str, {"ON", "OFF"}),
    "appid": (int, None), "game": (str, None), "game_started": (str, None), "store_url": (str, None),
}
NULLABLE = {"battery", "power_w", "time_left_min", "health", "apu_temp", "fan_rpm", "appid", "game_started",
            "store_url"}
ALL_SCENARIOS = list(scenarios.SCENARIOS)


class StateFields(unittest.TestCase):
    def check(self, state: dict) -> None:
        self.assertEqual(list(state), list(SCHEMA), "fields were added, removed, renamed or reordered")
        for key, (kind, allowed) in SCHEMA.items():
            value = state[key]
            with self.subTest(field=key, value=value):
                if value is None:
                    self.assertIn(key, NULLABLE, f"{key} must never be null")
                    continue
                if kind is float:  # JSON has one number type; a whole number may come back as an int
                    self.assertIsInstance(value, (int, float))
                    self.assertNotIsInstance(value, bool)
                else:
                    self.assertIs(type(value), kind)
                if allowed:
                    self.assertIn(value, allowed)

    def test_every_scenario_matches_the_schema(self):
        for name in ALL_SCENARIOS:
            with self.subTest(scenario=name):
                self.check(scenarios.snapshot_of(scenarios.SCENARIOS[name]))

    def test_the_state_survives_a_json_round_trip_unchanged(self):
        for name in ALL_SCENARIOS:
            state = scenarios.snapshot_of(scenarios.SCENARIOS[name])
            self.assertEqual(json.loads(json.dumps(state)), state)

    def test_when_idle_every_game_field_is_empty(self):
        for name in ("idle", "bare"):
            s = scenarios.snapshot_of(scenarios.SCENARIOS[name])
            self.assertEqual((s["playing"], s["game"], s["appid"], s["game_started"], s["store_url"]),
                             ("OFF", "Idle", None, None, None))

    def test_a_steam_game_has_an_id_name_start_time_and_store_page(self):
        s = scenarios.snapshot_of(scenarios.playing)
        self.assertEqual((s["playing"], s["appid"], s["game"]), ("ON", 367520, "Hollow Knight"))
        self.assertEqual(s["store_url"], "https://store.steampowered.com/app/367520/")
        self.assertRegex(s["game_started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")

    def test_a_non_steam_game_has_an_id_at_or_above_2_pow_31_and_no_store_page(self):
        s = scenarios.snapshot_of(scenarios.non_steam)
        self.assertEqual((s["playing"], s["appid"], s["game"], s["store_url"]),
                         ("ON", scenarios.NON_STEAM_ID, "RetroArch", None))
        self.assertGreaterEqual(s["appid"], 2**31)

    def test_nothing_readable_gives_nulls_and_off_not_missing_fields(self):
        s = scenarios.snapshot_of(scenarios.bare)
        self.assertEqual({k for k, v in s.items() if v is None}, NULLABLE)
        self.assertEqual(s["mode"], "Unknown")


class Fixtures(unittest.TestCase):
    def test_checked_in_fixtures_are_what_the_code_produces(self):
        for name, content in scenarios.generate().items():
            with self.subTest(fixture=name):
                on_disk = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
                self.assertEqual(on_disk, content,
                                 f"{name} is stale; review the change, then run python3 tests/regen_fixtures.py")

    def test_fixtures_are_written_in_the_published_field_order(self):
        for name in ("state_playing.json", "state_idle.json"):
            self.assertEqual(list((json.loads((FIXTURES / name).read_text()))), list(SCHEMA))


class Topics(unittest.TestCase):
    def test_topic_layout(self):
        b = dhb.Bridge(__import__("tests.support", fromlist=["make_cfg"]).make_cfg())
        self.assertEqual((b.state_topic, b.avail_topic), ("deck-ha-bridge/steamdeck/state",
                                                          "deck-ha-bridge/steamdeck/availability"))

    def test_the_state_is_the_object_the_subscriber_receives(self):
        # The bridge publishes json.dumps(snapshot) with no wrapper and the retain flag set (see test_bridge).
        self.assertIsInstance(scenarios.snapshot_of(scenarios.playing), dict)


class DiscoveryReadsOnlyRealFields(unittest.TestCase):
    """Home Assistant's templates are part of the contract too: a renamed field would silently go 'None'."""

    def test_every_template_reference_is_a_published_field(self):
        used = set()
        for _, _, extra in dhb.entity_defs():
            for key in ("value_template", "json_attributes_template"):
                used.update(re.findall(r"value_json\.(\w+)", extra.get(key, "")))
        self.assertTrue(used)
        self.assertLessEqual(used, set(SCHEMA), "a discovery template reads a field that is not published")

    def test_every_published_field_is_used_by_some_entity(self):
        used = set()
        for _, _, extra in dhb.entity_defs():
            for key in ("value_template", "json_attributes_template"):
                used.update(re.findall(r"value_json\.(\w+)", extra.get(key, "")))
        self.assertEqual(set(SCHEMA) - used, set(), "a published field no entity shows (document it if intended)")


class DocMatchesCode(unittest.TestCase):
    def test_the_document_lists_every_field_and_nothing_else(self):
        text = (ROOT / "docs" / "state-schema.md").read_text(encoding="utf-8")
        documented = re.findall(r"^\| `(\w+)` \|", text, re.M)
        self.assertEqual(documented, list(SCHEMA))

    def test_the_document_names_the_topics(self):
        text = (ROOT / "docs" / "state-schema.md").read_text(encoding="utf-8")
        for needle in ("deck-ha-bridge/<node>/state", "deck-ha-bridge/<node>/availability", "`online`", "`offline`"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
