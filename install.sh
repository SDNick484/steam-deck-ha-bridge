#!/usr/bin/env bash
# install.sh - set up deck_ha_bridge.py as a systemd user service on a Steam Deck.
#
# Run from Desktop Mode (Konsole) or over SSH, in the folder holding deck_ha_bridge.py:
#   bash install.sh             install or reconfigure
#   bash install.sh --uninstall stop the service and remove everything
#
# Nothing touches the read-only system partition: files go under ~/.local and ~/.config,
# which survive SteamOS updates. No sudo needed.
set -euo pipefail

APP=deck-ha-bridge
SHARE="$HOME/.local/share/$APP"
CONF_DIR="$HOME/.config/$APP"
CONF="$CONF_DIR/config.ini"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/$APP.service"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now "$APP.service" 2>/dev/null || true
  rm -f "$UNIT"
  rm -rf "$SHARE"
  systemctl --user daemon-reload
  echo "Removed the service and program. Your config is still at $CONF (delete it if you like)."
  echo "In Home Assistant, delete the 'Steam Deck' device under Settings > Devices to remove its entities."
  exit 0
fi

[[ -f "$HERE/deck_ha_bridge.py" ]] || { echo "deck_ha_bridge.py must be next to install.sh"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }

mkdir -p "$SHARE" "$CONF_DIR" "$UNIT_DIR"
install -m 0755 "$HERE/deck_ha_bridge.py" "$SHARE/deck_ha_bridge.py"

ask() { # ask VAR "Prompt" default
  local __v; read -r -p "$2${3:+ [$3]}: " __v; printf -v "$1" '%s' "${__v:-$3}"
}

if [[ -f "$CONF" ]]; then
  read -r -p "Keep the existing config at $CONF? [Y/n] " keep
  [[ "${keep,,}" == n* ]] && rm -f "$CONF"
fi

if [[ ! -f "$CONF" ]]; then
  echo "MQTT broker details (the same broker Home Assistant's MQTT integration uses):"
  ask HOST "Broker host or IP (a Tailscale name works too)" ""
  [[ -n "$HOST" ]] || { echo "A broker host is required."; exit 1; }
  ask TLS "Use TLS? (y/N)" "n"
  if [[ "${TLS,,}" == y* ]]; then TLS=true; DEFPORT=8883; else TLS=false; DEFPORT=1883; fi
  ask PORT "Port" "$DEFPORT"
  ask MQTT_USER "Username (blank for none)" ""
  PASS=""
  if [[ -n "$MQTT_USER" ]]; then read -r -s -p "Password: " PASS; echo; fi
  ask NAME "Device name in Home Assistant" "Steam Deck"
  ask NODE "MQTT node id (letters, digits, _ -)" "steamdeck"

  umask 077
  cat > "$CONF" <<EOF
[mqtt]
host = $HOST
port = $PORT
username = $MQTT_USER
password = $PASS
tls = $TLS
# Set to true only for a self-signed broker certificate you trust.
tls_insecure = false
discovery_prefix = homeassistant

[device]
name = $NAME
node_id = $NODE
# How often to check for a game launch or exit, in seconds.
poll_seconds = 5
# Republish the full state at least this often, in seconds.
heartbeat_seconds = 60
EOF
  chmod 600 "$CONF"
  echo "Wrote $CONF"
fi

cat > "$UNIT" <<EOF
[Unit]
Description=Steam Deck to Home Assistant bridge (MQTT)

[Service]
ExecStart=/usr/bin/env python3 %h/.local/share/$APP/deck_ha_bridge.py
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

echo
echo "Current readings:"
python3 "$SHARE/deck_ha_bridge.py" --once || true
echo

systemctl --user daemon-reload
systemctl --user enable --now "$APP.service"
systemctl --user restart "$APP.service"
sleep 3
systemctl --user --no-pager --lines=8 status "$APP.service" || true
echo
echo "Done. In Home Assistant, look for the '$(sed -n 's/^name = //p' "$CONF")' device under Settings > Devices & services > MQTT."
echo "Logs: journalctl --user -u $APP -f"
