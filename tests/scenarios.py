"""Fake Decks that the state-schema tests and regen_fixtures.py build, and the fixtures made from them.

The checked-in files under tests/fixtures/ are whatever generate() returns. A test compares them with a
fresh run, so an edit to the published state or the discovery payloads shows up as a failing test and,
once reviewed on purpose, a regenerated fixture (python3 tests/regen_fixtures.py).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from tests.support import FakeDeck, RecordingClient, dhb, make_cfg

NON_STEAM_ID = 0xC1A2B3C4


def playing(deck: FakeDeck) -> None:
    """Game Mode, Hollow Knight running on battery, undocked."""
    deck.boot_time()
    deck.process(50, "gamescope-wl", [b"gamescope-wl"], 10)
    deck.reaper(4321, 367520, start_ticks=123_456)  # 1234.56 s after boot: 2023-11-14T22:33:54Z
    deck.manifest(367520, "Hollow Knight")
    deck.battery(status="Discharging", capacity=62, power_now=9_400_000, current_now=1_300_000,
                 voltage_now=7_200_000, charge_now=2_000_000, charge_full=4_800_000,
                 charge_full_design=5_000_000)
    deck.mains(online=False)
    deck.display("eDP-1", True)
    deck.display("DP-1", False)
    deck.hwmon(3, "amdgpu", temp1_input=61_000)
    deck.hwmon(5, "steamdeck_hwmon", fan1_input=3200)


def idle(deck: FakeDeck) -> None:
    """Desktop Mode, charging and docked, no game, no temperature sensors."""
    deck.boot_time()
    deck.process(60, "plasmashell", [b"plasmashell"], 15)
    deck.battery(status="Charging", capacity=88, power_now=22_000_000, energy_full=38_000_000,
                 energy_full_design=40_000_000)
    deck.mains(online=True)
    deck.display("eDP-1", True)
    deck.display("HDMI-A-1", True)


def bare(deck: FakeDeck) -> None:
    """Nothing readable at all: no battery, no sensors, no game, no session."""


def non_steam(deck: FakeDeck) -> None:
    """A non-Steam shortcut (an emulator) running."""
    deck.boot_time()
    deck.process(50, "gamescope-wl", [b"gamescope-wl"], 10)
    deck.reaper(4321, NON_STEAM_ID, start_ticks=200_000)
    deck.shortcuts([{"appid": NON_STEAM_ID, "name": "RetroArch"}])
    deck.battery(status="Discharging", capacity=40, power_now=12_000_000, energy_now=24_000_000)


SCENARIOS = {"playing": playing, "idle": idle, "bare": bare, "non_steam": non_steam}


def snapshot_of(build) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        deck = FakeDeck(Path(tmp))
        build(deck)
        with deck.patched():
            return dhb.snapshot(dhb.GameNames())


def discovery_payloads() -> dict:
    """Every discovery message for a default config, as {topic: payload}."""
    bridge = dhb.Bridge(make_cfg())
    bridge.client = RecordingClient()
    with mock.patch.object(dhb, "log"):
        bridge.publish_discovery()
    return {topic: json.loads(payload) for topic, payload, _ in bridge.client.published}


def generate() -> dict:
    """File name under tests/fixtures/ -> content."""
    return {
        "state_playing.json": snapshot_of(playing),
        "state_idle.json": snapshot_of(idle),
        "discovery_golden.json": discovery_payloads(),
    }
