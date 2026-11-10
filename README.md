# Steam Deck → Home Assistant bridge

Publishes your Steam Deck's state to [Home Assistant](https://www.home-assistant.io) over MQTT. The entities show up on their own through MQTT discovery, grouped under one **Steam Deck** device.

| Entity | |
|---|---|
| `sensor.steam_deck_battery` | Battery % |
| `sensor.steam_deck_battery_power` | Charge / discharge rate (W) |
| `sensor.steam_deck_time_remaining` | Estimated minutes left while discharging |
| `sensor.steam_deck_battery_health` | Full-charge capacity vs. design capacity |
| `binary_sensor.steam_deck_charging` / `_plugged_in` | Power state |
| `binary_sensor.steam_deck_docked` | An external display is connected |
| `binary_sensor.steam_deck_playing` | A game is running |
| `sensor.steam_deck_game` | Name of the running game (`Idle` when none); includes Steam and non-Steam shortcuts |
| `sensor.steam_deck_game_started` | When the current game was launched |
| `sensor.steam_deck_mode` | Game Mode / Desktop Mode |
| `sensor.steam_deck_apu_temperature`, `sensor.steam_deck_fan_speed` | Diagnostics |

When the Deck sleeps or leaves the network, every entity shows **unavailable** within about 45 seconds, through MQTT's last-will message.

## How it works

`deck_ha_bridge.py` is a single file that uses only Python's standard library. SteamOS's root filesystem is read-only and has no pip, so the script includes its own small MQTT 3.1.1 client. It runs as a systemd **user** service, so no sudo is needed and it survives SteamOS updates.

**Game detection:** Steam launches games through its `reaper` wrapper (`reaper SteamLaunch AppId=N`). The bridge finds that process and names the game from Steam's app manifests, including games on an SD card. Non-Steam shortcuts, such as emulators added by EmuDeck or Steam ROM Manager, are named from `shortcuts.vdf`.

**Battery, dock and temperatures** come from `/sys/class/power_supply`, `/sys/class/drm` and `/sys/class/hwmon`.

The bridge checks the Deck every 5 seconds. It publishes only when something meaningful changes, plus a full update every 60 seconds. When Home Assistant restarts, it republishes discovery and state.

## Requirements

- **An MQTT broker** that Home Assistant uses. On Home Assistant OS, the **Mosquitto broker** app plus the MQTT integration work out of the box.
- **A broker login for the Deck.** Mosquitto accepts Home Assistant users, so create a local-only user such as `deck` under Settings → People → Users.

## Install

On the Deck, switch to Desktop Mode, open Konsole, and run:

```sh
git clone https://github.com/SDNick484/steam-deck-ha-bridge.git
cd steam-deck-ha-bridge
bash install.sh
```

The installer:
1. Asks for the broker host, port, TLS, username and password, then the device name.
2. Writes `~/.config/deck-ha-bridge/config.ini` (mode 600).
3. Prints a test reading.
4. Enables the user service, which starts at boot in both Game Mode and Desktop Mode.

Useful commands:

```sh
journalctl --user -u deck-ha-bridge -f                          # logs
python3 ~/.local/share/deck-ha-bridge/deck_ha_bridge.py --once  # one reading as JSON
bash install.sh --uninstall                                     # remove it
```

If your broker is on a VPN such as Tailscale, give the installer the VPN hostname, and the Deck keeps reporting when it's away from home.

**Entity IDs:** the IDs above assume the default device name, "Steam Deck". Home Assistant derives the IDs from whatever name you choose.

**Temperatures:** Home Assistant shows them in your configured units, so °F is normal on a US-customary setup.

## Example automations

`examples/ha_automations.yaml` has four automations to adapt:
- **Dim the lights** when a game starts while the Deck is docked after sunset, and restore them afterward.
- **A low-battery warning**, with the game and the minutes left.
- **A two-hour session nudge.**
- **A fully-charged alert**, which ignores the Deck waking from sleep already full.

Replace `light.living_room` and `notify.mobile_app_your_phone` with your own entities.

## Configuration

`~/.config/deck-ha-bridge/config.ini`:

```ini
[mqtt]
host = homeassistant.local
port = 1883
username = deck
password = ...
tls = false
tls_insecure = false
discovery_prefix = homeassistant

[device]
name = Steam Deck
node_id = steamdeck
poll_seconds = 5
heartbeat_seconds = 60
```

## Troubleshooting

- **No entities in Home Assistant:** check the log for `connected to …`. A login error means the broker rejected the username or password.
- **A game isn't detected:** run `--once` while the game is running and check `appid` and `game`. Detection relies on Steam's `reaper` launcher, which SteamOS uses for Steam and non-Steam games alike.

This project isn't affiliated with Valve or Home Assistant.

## State topic

The Deck's state is published as one retained JSON object on `deck-ha-bridge/<node>/state`, with `online`/`offline` on `deck-ha-bridge/<node>/availability`. Other programs can subscribe to it. Every field, its type and when it is `null` are listed in [docs/state-schema.md](docs/state-schema.md).

## Tests

The tests use only the standard library's `unittest` and need no network and no Deck. They run the MQTT client against a fake broker on a loopback port, the readers against fake `/sys`, `/proc` and Steam trees (through `DECK_SYSFS_ROOT`, `DECK_PROC_ROOT` and `DECK_STEAM_ROOT`), the bridge logic, the state-schema contract, and `install.sh` with `systemctl` stubbed. `shellcheck` must be on your `PATH` (`pip install shellcheck-py` works).

```bash
python3 -m unittest discover -s tests -t . -v     # every test; a skipped test fails CI
sh .github/scripts/lint.sh                        # shellcheck and py_compile
python3 .github/scripts/privacy_scan.py           # no IPs, SteamIDs or personal emails in the repo
python3 tests/regen_fixtures.py                   # rewrite tests/fixtures/ after an intended change
```

The fixtures under `tests/fixtures/` are produced by the code, and a test compares them with a fresh run, so a changed state field or discovery payload fails until the change is reviewed and the fixtures are regenerated.

**Known bug, recorded as an expected failure:** `GameNames.load_shortcuts()` pairs an `appid` with the next `AppName` it reads, so a `shortcuts.vdf` entry with no `appid` key would take the next entry's id. The test (`test_an_entry_without_an_appid_does_not_steal_the_next_entrys_id`) is marked `expectedFailure`; fixing the bug makes it an unexpected success, which fails the run until the marker is removed.

GitHub Actions runs the tests on Python 3.11 to 3.14, the lint, and the privacy scan on every push and pull request, and weekly.

## License

MIT
