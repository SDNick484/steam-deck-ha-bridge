"""Helpers shared by the tests: import the bridge, and build fake /sys, /proc and Steam trees.

The bridge reads its roots from DECK_SYSFS_ROOT, DECK_PROC_ROOT and DECK_STEAM_ROOT when it is imported,
and then keeps them in module globals (SYS, PROC, STEAM). In-process tests point those globals at a
throwaway tree with FakeDeck.patched(); the tests that run the script as a program set the environment
variables instead.
"""
from __future__ import annotations

import configparser
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

import deck_ha_bridge as dhb  # noqa: E402  (after the path tweak)

BOOT_TIME = 1_700_000_000  # 2023-11-14T22:13:20Z


def vdf_shortcuts(entries: list[dict]) -> bytes:
    """A binary shortcuts.vdf: map "shortcuts" holding one numbered map per entry.

    Each entry may carry "appid" (int, little-endian uint32), "name", and is padded with the keys real
    files have (Exe, StartDir, a nested "tags" map) so the parser has noise to step over. An entry
    without "appid" is written without the key.
    """
    out = b"\x00shortcuts\x00"
    for i, e in enumerate(entries):
        out += b"\x00" + str(i).encode() + b"\x00"
        if e.get("appid") is not None:
            out += b"\x02appid\x00" + struct.pack("<I", e["appid"])
        out += b"\x01AppName\x00" + e["name"].encode("utf-8") + b"\x00"
        out += b'\x01Exe\x00"/usr/bin/emulator"\x00'
        out += b'\x01StartDir\x00"/usr/bin/"\x00'
        out += b"\x00tags\x00" + b"\x01" + b"0\x00" + b"favorite\x00" + b"\x08"
        out += b"\x08"
    return out + b"\x08\x08"


def acf(appid: int, name: str) -> str:
    return ('"AppState"\n{\n\t"appid"\t\t"%d"\n\t"Universe"\t\t"1"\n\t"name"\t\t"%s"\n'
            '\t"StateFlags"\t\t"4"\n}\n' % (appid, name))


class FakeDeck:
    """A throwaway /sys, /proc and Steam tree under `base`."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.sys = self.base / "sys"
        self.proc = self.base / "proc"
        self.steam = self.base / "steam"
        for p in (self.sys / "class" / "power_supply", self.sys / "class" / "drm",
                  self.sys / "class" / "hwmon", self.proc, self.steam / "steamapps"):
            p.mkdir(parents=True, exist_ok=True)

    def patched(self):
        """Point the bridge module at this tree. Use as a patcher (start/stop) or a context manager."""
        return mock.patch.multiple(dhb, SYS=str(self.sys), PROC=str(self.proc), STEAM=str(self.steam),
                                   _CLK_TCK=100)

    @property
    def env(self) -> dict[str, str]:
        """The same roots as environment variables, for running the script as a program."""
        return {"DECK_SYSFS_ROOT": str(self.sys), "DECK_PROC_ROOT": str(self.proc),
                "DECK_STEAM_ROOT": str(self.steam)}

    @staticmethod
    def write(path: Path, text) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(str(text) + "\n")

    # --- /sys
    def battery(self, name: str = "BAT1", **attrs) -> Path:
        d = self.sys / "class" / "power_supply" / name
        self.write(d / "type", "Battery")
        for key, value in attrs.items():
            if value is not None:
                self.write(d / key, value)
        return d

    def mains(self, name: str = "ADP1", online: bool = True) -> None:
        d = self.sys / "class" / "power_supply" / name
        self.write(d / "type", "Mains")
        self.write(d / "online", "1" if online else "0")

    def display(self, connector: str, connected: bool, card: str = "card0") -> None:
        self.write(self.sys / "class" / "drm" / f"{card}-{connector}" / "status",
                   "connected" if connected else "disconnected")

    def hwmon(self, index: int, name: str, **files) -> None:
        d = self.sys / "class" / "hwmon" / f"hwmon{index}"
        self.write(d / "name", name)
        for key, value in files.items():
            self.write(d / key, value)

    # --- /proc
    def boot_time(self, btime: int = BOOT_TIME) -> None:
        self.write(self.proc / "stat", f"cpu  1 2 3 4\nbtime {btime}\nprocesses 99")

    def process(self, pid: int, comm: str, cmdline: list[bytes] | None = None,
                start_ticks: int | None = None) -> None:
        d = self.proc / str(pid)
        self.write(d / "comm", comm)
        if cmdline is not None:
            (d / "cmdline").write_bytes(b"\0".join(cmdline) + b"\0")
        if start_ticks is not None:
            # /proc/<pid>/stat: "pid (comm) state ..." and starttime is field 22, the 20th after ")".
            tokens = ["S"] + ["0"] * 18 + [str(start_ticks)] + ["0"] * 30
            self.write(d / "stat", f"{pid} ({comm}) " + " ".join(tokens))

    def reaper(self, pid: int, appid, start_ticks: int | None = 1000) -> None:
        self.process(pid, "reaper", [b"reaper", b"SteamLaunch", f"AppId={appid}".encode(), b"--",
                                     b"/path/to/game"], start_ticks)

    # --- Steam
    def manifest(self, appid: int, name: str, library: Path | None = None) -> None:
        root = library if library is not None else self.steam
        self.write(Path(root) / "steamapps" / f"appmanifest_{appid}.acf", acf(appid, name))

    def sd_card(self, name: str = "sdcard") -> Path:
        root = self.base / name
        (root / "steamapps").mkdir(parents=True, exist_ok=True)
        return root

    def library_folders(self, *extra: Path) -> None:
        lines = ['"libraryfolders"', "{"]
        for i, root in enumerate([self.steam, *extra]):
            lines += [f'\t"{i}"', "\t{", f'\t\t"path"\t\t"{root}"', '\t\t"label"\t\t""', "\t}"]
        lines.append("}")
        self.write(self.steam / "steamapps" / "libraryfolders.vdf", "\n".join(lines))

    def shortcuts(self, entries: list[dict], user: str = "1001") -> None:
        self.write(self.steam / "userdata" / user / "config" / "shortcuts.vdf", vdf_shortcuts(entries))


class DeckTestCase(unittest.TestCase):
    """A fake Deck for the whole test, with the bridge module pointed at it (so nothing reads the host)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.deck = FakeDeck(Path(tmp.name))
        patcher = self.deck.patched()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.names = dhb.GameNames()


def make_cfg(mqtt: dict | None = None, device: dict | None = None) -> configparser.ConfigParser:
    """A parsed config.ini. Passing device=None leaves out the [device] section, as an old config would."""
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"mqtt": {"host": "broker.example.com", "port": "1883", **(mqtt or {})}})
    if device is not None:
        cfg.read_dict({"device": device})
    return cfg


class RecordingClient:
    """Stands in for MiniMQTT when a test only wants to see what the bridge would publish."""

    host, port = "broker.example.com", 1883

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []

    def publish(self, topic: str, payload, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))


class FakeTime:
    """A clock the test moves by hand. Patched over the bridge's `time` name only (not the real module)."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
