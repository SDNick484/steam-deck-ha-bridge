# State schema

The bridge publishes the Deck's state as one JSON object on a retained MQTT topic. Other programs can
subscribe to it. This page is the contract: `tests/test_state_schema.py` fails if the code and this page
drift apart, and if a field is renamed or removed.

## Topics

`<node>` is the `node_id` from the config (default `steamdeck`; characters other than letters, digits,
`-` and `_` become `_`).

| Topic | Payload | Retained | Written |
|---|---|---|---|
| `deck-ha-bridge/<node>/state` | JSON object, below | yes | when something worth reporting changes, and at least every `heartbeat_seconds` (default 60) |
| `deck-ha-bridge/<node>/availability` | `online` or `offline` | yes | `online` after every connect; `offline` by the broker (last will) when the bridge drops without saying goodbye, and by the bridge on a clean stop |
| `<discovery_prefix>/<sensor or binary_sensor>/<node>/<object_id>/config` | Home Assistant discovery JSON | yes | on connect, and when Home Assistant announces `online` on `<discovery_prefix>/status` |

All messages are QoS 0. A subscriber that starts late gets the last state straight away because it is
retained. Treat `offline` on the availability topic as "the state is stale".

## State object

Every field is always present. Unknown values are `null`, never omitted. Fields appear in this order.

| Field | Type | Null when | Meaning |
|---|---|---|---|
| `battery` | integer | no battery found or unreadable | Charge, percent. |
| `charging` | string | never | `"ON"` while the battery reports Charging, else `"OFF"`. |
| `plugged` | string | never | `"ON"` if a mains supply reports online, else `"OFF"`. |
| `power_w` | number | no power or current and voltage reading | Battery power draw or charge power, watts, one decimal. |
| `time_left_min` | integer | not discharging, or no usable reading, or estimate over 48 hours | Estimated minutes of runtime left. |
| `health` | number | full or design capacity unreadable | Full capacity as a percent of design capacity, one decimal. |
| `docked` | string | never | `"ON"` if an external display is connected, else `"OFF"`. |
| `apu_temp` | number | no `amdgpu` or `k10temp` temperature sensor | APU temperature, degrees Celsius. |
| `fan_rpm` | integer | no hwmon device with a `fan1_input` | Fan speed, rpm. |
| `mode` | string | never | `"Game Mode"`, `"Desktop Mode"` or `"Unknown"` (no session process recognised). |
| `playing` | string | never | `"ON"` while a game is running, else `"OFF"`. |
| `appid` | integer | no game running | Steam app id. Non-Steam shortcuts have ids of 2147483648 (2^31) or more. |
| `game` | string | never | Game name, or `"Idle"` when nothing is running. Falls back to `"App <appid>"` for an unknown Steam game and `"Non-Steam game"` for an unknown shortcut. |
| `game_started` | string | no game running, or start time unknown | When the game's process started: UTC, ISO 8601, whole seconds, with `+00:00` offset. |
| `store_url` | string | no game running, or a non-Steam shortcut | `https://store.steampowered.com/app/<appid>/`. |

`ON` and `OFF` are strings, not booleans, so the values can feed Home Assistant binary sensors directly.

### Examples

Produced by the code and checked by tests: `tests/fixtures/state_playing.json` (Game Mode, a game
running) and `tests/fixtures/state_idle.json` (Desktop Mode, nothing running, no temperature sensors).
After an intended change, `python3 tests/regen_fixtures.py` rewrites them; review the diff.

## Changing the contract

- Adding a field is compatible. Subscribers should ignore fields they do not know.
- Renaming a field, removing one, or changing its type or the values of an `ON`/`OFF` or `mode` field breaks
  subscribers. Do it only on purpose, update this page and the tests together, and say so in the release notes.
- The device name and `node_id` fix Home Assistant's entity ids (for example `sensor.steam_deck_battery`);
  changing them renames the entities there.
