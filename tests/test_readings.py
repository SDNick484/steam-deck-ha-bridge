"""The readers: battery, dock, hwmon, the Steam process scan and game names, each against a fake tree."""
from __future__ import annotations

import os
import unittest

from tests.support import BOOT_TIME, DeckTestCase, dhb

NON_STEAM_ID = 0xC1A2B3C4  # shortcut ids have the top bit set; reaper is started with AppId=<this>


class Battery(DeckTestCase):
    def test_discharging_reports_power_runtime_and_health(self):
        self.deck.battery(status="Discharging", capacity=62, power_now=9_400_000, current_now=1_300_000,
                          voltage_now=7_200_000, charge_now=2_000_000, charge_full=4_800_000,
                          charge_full_design=5_000_000)
        self.assertEqual(dhb.battery_state(), {
            "battery": 62, "charging": "OFF", "plugged": "OFF", "power_w": 9.4,
            "time_left_min": 92,  # 2.0 Ah / 1.3 A = 1.538 h
            "health": 96.0})

    def test_charging_on_mains(self):
        self.deck.battery(status="Charging", capacity=88, power_now=22_000_000, energy_full=38_000_000,
                          energy_full_design=40_000_000)
        self.deck.mains(online=True)
        s = dhb.battery_state()
        self.assertEqual((s["battery"], s["charging"], s["plugged"]), (88, "ON", "ON"))
        self.assertEqual((s["power_w"], s["time_left_min"], s["health"]), (22.0, None, 95.0))

    def test_full_is_plugged_in_but_not_charging(self):
        self.deck.battery(status="Full", capacity=100)
        self.deck.mains(online=True)
        s = dhb.battery_state()
        self.assertEqual((s["battery"], s["charging"], s["plugged"], s["time_left_min"]), (100, "OFF", "ON", None))

    def test_other_statuses_are_not_charging(self):
        for status in ("Not charging", "Unknown", "Discharging"):
            with self.subTest(status=status):
                self.deck.battery(status=status, capacity=50)
                self.assertEqual(dhb.battery_state()["charging"], "OFF")

    def test_an_offline_mains_supply_is_not_plugged_in(self):
        self.deck.battery(status="Discharging", capacity=50)
        self.deck.mains(online=False)
        self.assertEqual(dhb.battery_state()["plugged"], "OFF")

    def test_runtime_from_energy_and_power_when_there_is_no_charge_counter(self):
        self.deck.battery(status="Discharging", capacity=50, power_now=10_000_000, energy_now=20_000_000)
        self.assertEqual(dhb.battery_state()["time_left_min"], 120)  # 20 Wh at 10 W

    def test_power_from_current_times_voltage_when_power_now_is_missing(self):
        # current_now is negative on some batteries while discharging; only its size matters.
        self.deck.battery(status="Discharging", capacity=50, current_now=-1_500_000, voltage_now=7_000_000,
                          charge_now=3_000_000)
        s = dhb.battery_state()
        self.assertEqual((s["power_w"], s["time_left_min"]), (10.5, 120))

    def test_an_absurd_runtime_is_dropped(self):
        self.deck.battery(status="Discharging", capacity=50, current_now=10_000, voltage_now=7_000_000,
                          charge_now=5_000_000)  # 500 hours
        self.assertIsNone(dhb.battery_state()["time_left_min"])

    def test_zero_current_gives_no_runtime(self):
        self.deck.battery(status="Discharging", capacity=50, power_now=0, current_now=0, charge_now=1_000_000)
        self.assertIsNone(dhb.battery_state()["time_left_min"])

    def test_no_battery_at_all(self):
        self.deck.mains(online=True)
        self.assertEqual(dhb.battery_state(), {
            "battery": None, "charging": "OFF", "plugged": "ON", "power_w": None,
            "time_left_min": None, "health": None})

    def test_unreadable_capacity_is_unknown_not_zero(self):
        self.deck.battery(status="Discharging", capacity="n/a")
        self.assertIsNone(dhb.battery_state()["battery"])

    def test_health_needs_both_capacities(self):
        self.deck.battery(status="Full", capacity=100, charge_full=4_800_000)
        self.assertIsNone(dhb.battery_state()["health"])

    def test_the_first_battery_wins(self):
        self.deck.battery("BAT0", status="Discharging", capacity=70)
        self.deck.battery("BAT1", status="Discharging", capacity=20)
        self.assertEqual(dhb.battery_state()["battery"], 70)


class Display(DeckTestCase):
    def test_an_external_display_means_docked(self):
        self.deck.display("eDP-1", True)
        self.deck.display("DP-1", True)
        self.assertEqual(dhb.docked(), "ON")

    def test_hdmi_counts_too(self):
        self.deck.display("HDMI-A-1", True)
        self.assertEqual(dhb.docked(), "ON")

    def test_the_built_in_screen_alone_is_not_docked(self):
        self.deck.display("eDP-1", True)
        self.assertEqual(dhb.docked(), "OFF")

    def test_a_disconnected_external_port_is_not_docked(self):
        self.deck.display("eDP-1", True)
        self.deck.display("DP-1", False)
        self.assertEqual(dhb.docked(), "OFF")

    def test_no_display_information_at_all(self):
        self.assertEqual(dhb.docked(), "OFF")


class Hwmon(DeckTestCase):
    def test_temperature_and_fan_from_separate_chips(self):
        self.deck.hwmon(3, "amdgpu", temp1_input=61_000)
        self.deck.hwmon(5, "steamdeck_hwmon", fan1_input=3200)
        self.assertEqual(dhb.hwmon_values(), {"apu_temp": 61.0, "fan_rpm": 3200})

    def test_k10temp_is_accepted(self):
        self.deck.hwmon(0, "k10temp", temp1_input=48_500)
        self.assertEqual(dhb.hwmon_values()["apu_temp"], 48.5)

    def test_temperature_of_other_chips_is_ignored(self):
        self.deck.hwmon(0, "nvme", temp1_input=40_000)
        self.deck.hwmon(1, "acpitz", temp1_input=30_000)
        self.assertIsNone(dhb.hwmon_values()["apu_temp"])

    def test_nothing_present(self):
        self.assertEqual(dhb.hwmon_values(), {"apu_temp": None, "fan_rpm": None})

    def test_an_unreadable_value_is_unknown(self):
        self.deck.hwmon(0, "amdgpu", temp1_input="garbage", fan1_input="")
        self.assertEqual(dhb.hwmon_values(), {"apu_temp": None, "fan_rpm": None})

    def test_chips_are_read_in_name_order_and_the_first_match_wins(self):
        self.deck.hwmon(1, "amdgpu", temp1_input=50_000)
        self.deck.hwmon(2, "k10temp", temp1_input=70_000)
        self.assertEqual(dhb.hwmon_values()["apu_temp"], 50.0)


class Processes(DeckTestCase):
    """scan_processes(): the running game from `reaper SteamLaunch AppId=N`, and Game vs Desktop Mode."""

    def test_a_game_found_through_reaper(self):
        self.deck.boot_time()
        self.deck.reaper(4321, 367520, start_ticks=123_456)  # 1234.56 s after boot
        found = dhb.scan_processes()
        self.assertEqual(found["appid"], 367520)
        self.assertAlmostEqual(found["started"], BOOT_TIME + 1234.56, places=2)

    def test_no_game(self):
        self.deck.boot_time()
        self.deck.process(100, "bash", [b"bash"], 5)
        self.assertEqual(dhb.scan_processes(), {"appid": None, "started": None, "mode": "Unknown"})

    def test_reaper_without_steamlaunch_is_not_a_game(self):
        self.deck.process(200, "reaper", [b"reaper", b"AppId=555", b"--", b"x"], 50)
        self.assertIsNone(dhb.scan_processes()["appid"])

    def test_a_bad_or_zero_appid_is_ignored(self):
        self.deck.reaper(201, "abc")
        self.deck.reaper(202, 0)
        self.assertIsNone(dhb.scan_processes()["appid"])

    def test_the_lowest_pid_wins_when_several_reapers_run(self):
        self.deck.boot_time()
        self.deck.reaper(900, 111, start_ticks=100)
        self.deck.reaper(300, 222, start_ticks=100)
        self.deck.reaper(700, 333, start_ticks=100)
        self.assertEqual(dhb.scan_processes()["appid"], 222)

    def test_a_missing_boot_time_leaves_the_start_unknown_but_the_game_known(self):
        self.deck.reaper(300, 222, start_ticks=100)  # no /proc/stat
        found = dhb.scan_processes()
        self.assertEqual((found["appid"], found["started"]), (222, None))

    def test_the_start_time_parses_a_comm_that_contains_parentheses(self):
        self.deck.boot_time()
        self.deck.write(self.deck.proc / "42" / "stat",
                        "42 (re (ap) er) S " + " ".join(["0"] * 18 + ["500"] + ["0"] * 30))
        self.assertEqual(dhb._proc_start("42"), BOOT_TIME + 5.0)

    def test_proc_entries_that_are_not_processes_or_have_vanished_are_skipped(self):
        self.deck.boot_time()
        (self.deck.proc / "sys").mkdir()
        (self.deck.proc / "777").mkdir()  # a process that exited mid-scan: no files
        self.deck.reaper(300, 222)
        self.assertEqual(dhb.scan_processes()["appid"], 222)

    def test_game_mode_desktop_mode_and_unknown(self):
        cases = [("Game Mode", [(10, "gamescope-wl")]),
                 ("Game Mode", [(10, "gamescope")]),
                 ("Desktop Mode", [(10, "plasmashell")]),
                 ("Unknown", [(10, "bash")])]
        for mode, procs in cases:
            with self.subTest(mode=mode, procs=procs):
                for pid, comm in procs:
                    self.deck.process(pid, comm, [comm.encode()])
                self.assertEqual(dhb.scan_processes()["mode"], mode)
                for pid, _ in procs:
                    for f in (self.deck.proc / str(pid)).iterdir():
                        f.unlink()
                    (self.deck.proc / str(pid)).rmdir()

    def test_gamescope_beats_plasmashell_whichever_is_listed_first(self):
        for gamescope_pid, plasma_pid in ((10, 20), (20, 10)):
            with self.subTest(gamescope_pid=gamescope_pid):
                self.deck.process(gamescope_pid, "gamescope-wl", [b"g"])
                self.deck.process(plasma_pid, "plasmashell", [b"p"])
                self.assertEqual(dhb.scan_processes()["mode"], "Game Mode")
                for pid in (gamescope_pid, plasma_pid):
                    for f in (self.deck.proc / str(pid)).iterdir():
                        f.unlink()
                    (self.deck.proc / str(pid)).rmdir()


class GameNames(DeckTestCase):
    def test_a_name_from_the_main_library(self):
        self.deck.manifest(367520, "Hollow Knight")
        self.assertEqual(self.names.name(367520), "Hollow Knight")

    def test_a_game_on_an_sd_card_listed_in_libraryfolders(self):
        card = self.deck.sd_card()
        self.deck.library_folders(card)
        self.deck.manifest(1245620, "ELDEN RING", library=card)
        self.assertEqual(self.names.name(1245620), "ELDEN RING")

    def test_library_dirs_lists_each_library_once(self):
        card = self.deck.sd_card()
        self.deck.library_folders(card)  # the main library is listed in the file too
        dirs = self.names.library_dirs()
        self.assertEqual(dirs, [str(self.deck.steam / "steamapps"), str(card / "steamapps")])

    def test_without_libraryfolders_only_the_main_library_is_searched(self):
        self.assertEqual(self.names.library_dirs(), [str(self.deck.steam / "steamapps")])

    def test_a_found_name_is_cached(self):
        self.deck.manifest(367520, "Hollow Knight")
        self.assertEqual(self.names.name(367520), "Hollow Knight")
        os.remove(self.deck.steam / "steamapps" / "appmanifest_367520.acf")
        self.assertEqual(self.names.name(367520), "Hollow Knight")

    def test_an_unknown_app_gets_a_placeholder_and_is_looked_up_again_later(self):
        self.assertEqual(self.names.name(424242), "App 424242")
        self.deck.manifest(424242, "Installed Just Now")
        self.assertEqual(self.names.name(424242), "Installed Just Now")

    def test_a_non_steam_game_is_named_from_shortcuts(self):
        self.deck.shortcuts([{"appid": NON_STEAM_ID, "name": "RetroArch"}])
        self.assertEqual(self.names.name(NON_STEAM_ID), "RetroArch")

    def test_an_unknown_non_steam_id_is_named_generically(self):
        self.assertEqual(self.names.name(NON_STEAM_ID), "Non-Steam game")

    def test_shortcuts_with_several_entries_unicode_names_and_two_users(self):
        self.deck.shortcuts([{"appid": 0xC0000001, "name": "Dolphin"},
                             {"appid": 0xC0000002, "name": "Pokémon Émeraude"}], user="1001")
        self.deck.shortcuts([{"appid": 0xC0000003, "name": "Yuzu"}], user="2002")
        self.assertEqual(self.names.load_shortcuts(),
                         {0xC0000001: "Dolphin", 0xC0000002: "Pokémon Émeraude", 0xC0000003: "Yuzu"})

    def test_a_shortcuts_file_that_cannot_be_read_is_skipped(self):
        path = self.deck.steam / "userdata" / "1001" / "config" / "shortcuts.vdf"
        path.mkdir(parents=True)  # a directory where the file should be
        self.assertEqual(self.names.load_shortcuts(), {})

    @unittest.expectedFailure
    def test_an_entry_without_an_appid_does_not_steal_the_next_entrys_id(self):
        """KNOWN BUG, not fixed here (this PR adds tests only). load_shortcuts() pairs an appid with the
        next AppName it sees, not with the AppName of the same entry. An entry that carries no `appid` key
        (I have not checked whether real files ever have one) leaves its name waiting, and it is then
        paired with the *next* entry's appid; the next entry's own name is left for the one after.
        Fixing it removes this decorator; an unexpected success fails the run so that cannot be forgotten.
        """
        self.deck.shortcuts([{"appid": None, "name": "No id here"},
                             {"appid": 0xC0000009, "name": "Real Game"}])
        self.assertEqual(self.names.load_shortcuts(), {0xC0000009: "Real Game"})


class Snapshot(DeckTestCase):
    def test_a_non_steam_game_has_no_store_url(self):
        self.deck.boot_time()
        self.deck.reaper(300, NON_STEAM_ID, start_ticks=100)
        self.deck.shortcuts([{"appid": NON_STEAM_ID, "name": "RetroArch"}])
        s = dhb.snapshot(self.names)
        self.assertEqual((s["playing"], s["appid"], s["game"], s["store_url"]),
                         ("ON", NON_STEAM_ID, "RetroArch", None))

    def test_a_steam_game_has_a_store_url(self):
        self.deck.reaper(300, 367520, start_ticks=100)
        s = dhb.snapshot(self.names)
        self.assertEqual(s["store_url"], "https://store.steampowered.com/app/367520/")
        self.assertEqual(s["game"], "App 367520")  # no manifest on this fake Deck

    def test_a_game_whose_start_time_is_unknown_is_still_playing(self):
        self.deck.reaper(300, 367520, start_ticks=None)
        s = dhb.snapshot(self.names)
        self.assertEqual((s["playing"], s["game_started"]), ("ON", None))

    def test_idle(self):
        s = dhb.snapshot(self.names)
        self.assertEqual((s["playing"], s["appid"], s["game"], s["game_started"], s["store_url"]),
                         ("OFF", None, "Idle", None, None))

    def test_the_start_time_is_utc_iso_8601_with_seconds(self):
        self.deck.boot_time()
        self.deck.reaper(300, 367520, start_ticks=123_456)
        self.assertEqual(dhb.snapshot(self.names)["game_started"], "2023-11-14T22:33:54+00:00")


if __name__ == "__main__":
    unittest.main()
