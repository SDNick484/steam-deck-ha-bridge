# steam-deck-ha-bridge

Publishes a Steam Deck's battery, dock, temperature, fan and running game to Home Assistant over MQTT.

- One file: `deck_ha_bridge.py`, Python standard library only, with its own MQTT 3.1.1 client. There is no
  pip on SteamOS, so no dependency may be added. `install.sh` (bash) sets it up as a systemd **user** service.
- The retained state topic's JSON is a contract: another project subscribes to it. `docs/state-schema.md`
  describes it and `tests/test_state_schema.py` fails when a field is renamed, removed or retyped. Change a
  field only on purpose, and update the doc, the tests and the fixtures (`python3 tests/regen_fixtures.py`)
  together.
- The device name and `node_id` fix Home Assistant's entity IDs (`sensor.steam_deck_battery` and so on).
  Never change the defaults or how the IDs are built.
- Tests: `python3 -m unittest discover -s tests -t . -v` (stdlib unittest, no network, no skips; needs
  `shellcheck` on PATH). Test seams are environment variables with today's paths as defaults
  (`DECK_SYSFS_ROOT`, `DECK_PROC_ROOT`, `DECK_STEAM_ROOT`, `DECK_HA_CONFIG`); add one only where a test needs it.
- `tests/test_privacy_scan.py` and `.github/scripts/privacy_scan.py` are copies from steam-backlog-triage;
  fix them there first.

STANDING RULES
- Read the current code on main before changing anything. If it contradicts a decision above, stop and
  tell me rather than improvising.
- steam-backlog-triage, steam-wishlist-deals, steam-deck-ha-bridge: Python standard library only; each page
  is one HTML file with no build step; Alpine with OpenRC and BusyBox crond is the primary target.
  mcp-server-steam-backlog: follow its CLAUDE.md; shared files change only in mcp-server-shared.
- Everything committed AND everything in a PR title, body or comment is public. Never write IPs, hostnames,
  my domain, container IDs, my time zone, my SteamID, phone or notify-service names, my real name or
  personal email, my Cloudflare team name or AUD, or any key. Use placeholders (7656119XXXXXXXXXX,
  notify.mobile_app_your_phone, steam.example.com, your-team.cloudflareaccess.com). Grep every diff.
  Author: SDNick484 <SDNick484@users.noreply.github.com>.
- Never ask me to paste keys or tokens into chat. If a step needs one, give me a vim command for the file
  on the server.
- Installers stay safe to re-run: keep config and data, remember options, replace only their own crontab
  block. Update the README in the same PR when behavior or options change.

SHIP PROCEDURE
1. Work on the feature branch named above. Small, focused commits, each with its tests.
2. Before pushing, run everything CI runs, locally, with nothing skipped: the unit tests on Python 3.11 to
   3.14, `sh .github/scripts/lint.sh`, and `python3 .github/scripts/privacy_scan.py`.
3. git push -u origin <branch>, then gh pr create. PR body: what changed, the exact commands you ran with
   pass counts, and anything I must do by hand. No deploy commands and no server details in the PR.
4. gh pr checks <number> --watch --interval 30. If a check fails: gh run view <run-id> --log-failed, fix
   the cause, push, watch again. Never get to green by skipping, loosening or deleting a test, lowering a
   threshold, or changing a pin this prompt didn't ask you to change. After three failed rounds, stop and
   tell me what is failing and why.
5. When every check is green, notify me: use your push-notification tool if you have one; otherwise, if
   STEAM_NOTIFY_CMD is set, run it with one argument, "<repo>#<number> CI green: <title>". Do not merge.
6. Finish with one message, in chat only: the PR URL, the check summary, and a DEPLOY VERDICT. This repo
   is not deployed through the dashboard. If the PR changed deck_ha_bridge.py or install.sh, give the update
   commands for the Deck (Desktop Mode, in Konsole: git pull, then bash install.sh and keep the config) and a
   check with `python3 ~/.local/share/deck-ha-bridge/deck_ha_bridge.py --once`. If neither changed, say that
   nothing deploys.
