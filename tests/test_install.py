"""install.sh, run for real in a throwaway HOME with systemctl, loginctl and sleep replaced by stubs."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import scenarios
from tests.support import ROOT, FakeDeck

APP = "deck-ha-bridge"
STUB = '#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\n'


class InstallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.log = base / "stub.log"
        self.log.touch()
        bindir = base / "bin"
        bindir.mkdir()
        for name in ("systemctl", "loginctl", "sleep"):
            stub = bindir / name
            stub.write_text(STUB.format(name=name))
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        # The installer runs the program once to print readings; point it at a fake Deck, not this machine.
        deck = FakeDeck(base / "deck")
        scenarios.playing(deck)
        self.env = {**os.environ, **deck.env, "HOME": str(self.home), "STUB_LOG": str(self.log),
                    "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        self.share = self.home / ".local/share" / APP
        self.conf = self.home / ".config" / APP / "config.ini"
        self.unit = self.home / ".config/systemd/user" / f"{APP}.service"

    def run_install(self, *args: str, answers: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(ROOT / "install.sh"), *args], input=answers, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    # prompts, in order: host, TLS, port, user, [password], device name, node id
    FIRST = "broker.example.com\nn\n\ndeck\ns3cret\nLiving Room Deck\nlrdeck\n"


class FreshInstall(InstallTestCase):
    def test_installs_the_program_config_and_unit_and_starts_the_service(self):
        p = self.run_install(answers=self.FIRST)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((self.share / "deck_ha_bridge.py").read_bytes(), (ROOT / "deck_ha_bridge.py").read_bytes())
        self.assertTrue(os.access(self.share / "deck_ha_bridge.py", os.X_OK))
        text = self.conf.read_text()
        for line in ("host = broker.example.com", "port = 1883", "username = deck", "password = s3cret",
                     "tls = false", "name = Living Room Deck", "node_id = lrdeck", "poll_seconds = 5",
                     "heartbeat_seconds = 60", "discovery_prefix = homeassistant"):
            self.assertIn(line, text.splitlines())
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o600)
        unit = self.unit.read_text()
        self.assertIn(f"ExecStart=/usr/bin/env python3 %h/.local/share/{APP}/deck_ha_bridge.py", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertEqual(self.calls(), ["systemctl --user daemon-reload",
                                        f"systemctl --user enable --now {APP}.service",
                                        f"systemctl --user restart {APP}.service", "sleep 3",
                                        f"systemctl --user --no-pager --lines=8 status {APP}.service"])

    def test_it_prints_the_current_readings_and_the_device_name(self):
        p = self.run_install(answers=self.FIRST)
        self.assertIn('"game": "Hollow Knight"', p.stdout)
        self.assertIn("'Living Room Deck' device", p.stdout)

    def test_the_password_is_never_echoed(self):
        p = self.run_install(answers=self.FIRST)
        self.assertNotIn("s3cret", p.stdout + p.stderr)

    def test_tls_changes_the_default_port_and_no_user_means_no_password_prompt(self):
        p = self.run_install(answers="broker.example.com\ny\n\n\n\n\n")  # host, tls, port, user, name, node
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = self.conf.read_text().splitlines()
        for line in ("port = 8883", "tls = true", "username = ", "password = ", "name = Steam Deck",
                     "node_id = steamdeck"):
            self.assertIn(line, lines)

    def test_a_missing_broker_host_stops_before_anything_is_started(self):
        p = self.run_install(answers="\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("A broker host is required.", p.stdout)
        self.assertFalse(self.conf.exists())
        self.assertFalse(self.unit.exists())
        self.assertEqual(self.calls(), [])

    def test_it_refuses_to_run_without_the_program_next_to_it(self):
        lone = self.home.parent / "lone"
        lone.mkdir()
        shutil.copy(ROOT / "install.sh", lone / "install.sh")
        p = subprocess.run(["bash", str(lone / "install.sh")], env=self.env, capture_output=True, text=True,
                           input="", timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertIn("deck_ha_bridge.py must be next to install.sh", p.stdout)


class SecondRun(InstallTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.run_install(answers=self.FIRST).returncode, 0)
        self.first_config = self.conf.read_bytes()
        self.log.write_text("")

    def test_keeping_the_config_leaves_it_byte_for_byte_and_asks_nothing_else(self):
        p = self.run_install(answers="y\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.conf.read_bytes(), self.first_config)
        self.assertNotIn("MQTT broker details", p.stdout)
        self.assertIn(f"{APP}.service", " ".join(self.calls()))

    def test_just_pressing_enter_keeps_the_config(self):
        self.assertEqual(self.run_install(answers="\n").returncode, 0)
        self.assertEqual(self.conf.read_bytes(), self.first_config)

    def test_the_program_and_unit_are_refreshed(self):
        (self.share / "deck_ha_bridge.py").write_text("# stale\n")
        self.unit.write_text("stale\n")
        self.assertEqual(self.run_install(answers="y\n").returncode, 0)
        self.assertEqual((self.share / "deck_ha_bridge.py").read_bytes(), (ROOT / "deck_ha_bridge.py").read_bytes())
        self.assertIn("Restart=always", self.unit.read_text())

    def test_saying_no_replaces_the_config_with_the_new_answers(self):
        p = self.run_install(answers="n\nother.example.com\nn\n1884\n\nSecond Deck\nsecond\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = self.conf.read_text().splitlines()
        for line in ("host = other.example.com", "port = 1884", "name = Second Deck", "node_id = second",
                     "username = "):
            self.assertIn(line, lines)
        self.assertNotIn("password = s3cret", lines)
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o600)

    def test_running_it_many_times_changes_nothing_further(self):
        for _ in range(3):
            self.assertEqual(self.run_install(answers="y\n").returncode, 0)
        self.assertEqual(self.conf.read_bytes(), self.first_config)
        self.assertEqual(len(list(self.unit.parent.glob("*"))), 1)


class Uninstall(InstallTestCase):
    def test_removes_what_it_installed_and_keeps_the_config(self):
        self.run_install(answers=self.FIRST)
        self.log.write_text("")
        p = self.run_install("--uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.share.exists())
        self.assertTrue(self.conf.exists(), "the config is the user's and stays")
        self.assertIn(str(self.conf), p.stdout)
        self.assertEqual(self.calls(), [f"systemctl --user disable --now {APP}.service",
                                        "systemctl --user daemon-reload"])

    def test_leaves_everything_else_in_the_home_alone(self):
        other = self.home / ".config/systemd/user/other.service"
        other.parent.mkdir(parents=True)
        other.write_text("keep me\n")
        self.run_install(answers=self.FIRST)
        self.run_install("--uninstall")
        self.assertEqual(other.read_text(), "keep me\n")

    def test_is_safe_when_nothing_is_installed(self):
        self.assertEqual(self.run_install("--uninstall").returncode, 0)

    def test_an_install_after_an_uninstall_reuses_the_kept_config(self):
        self.run_install(answers=self.FIRST)
        kept = self.conf.read_bytes()
        self.run_install("--uninstall")
        self.assertEqual(self.run_install(answers="y\n").returncode, 0)
        self.assertEqual(self.conf.read_bytes(), kept)
        self.assertTrue(self.unit.exists())


class ShellCheck(unittest.TestCase):
    def test_install_sh_is_clean(self):
        exe = shutil.which("shellcheck")
        self.assertIsNotNone(exe, "shellcheck is required (CI runners have it; locally: pip install shellcheck-py)")
        p = subprocess.run([exe, "--exclude=SC1090,SC1091", str(ROOT / "install.sh")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
