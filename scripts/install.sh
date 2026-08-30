#!/bin/sh

set -eu

PREFIX="${PREFIX:-/opt/raspi-ha-display-control}"
CONFIG_DIR="${CONFIG_DIR:-/etc/raspi-ha-display-control}"
SERVICE_DIR="${SERVICE_DIR:-/etc/systemd/user}"
SERVICE_NAME="raspi-display-mqtt.service"

if [ "$(id -u)" -ne 0 ]; then
    printf 'Run this installer as root.\n' >&2
    exit 77
fi

if ! id pi >/dev/null 2>&1; then
    printf 'The pi user was not found.\n' >&2
    exit 78
fi

install -d -o pi -g pi -m 0755 "$PREFIX/bin" "$PREFIX/mqtt"
install -m 0755 bin/display-control "$PREFIX/bin/display-control"
install -m 0644 mqtt/display-mqtt-bridge.py "$PREFIX/mqtt/display-mqtt-bridge.py"

install -d -m 0755 "$CONFIG_DIR"
if [ ! -e "$CONFIG_DIR/mqtt.env" ]; then
    install -o pi -g pi -m 0600 config/mqtt.env.example "$CONFIG_DIR/mqtt.env"
    printf 'Created %s; edit its broker credentials before starting the service.\n' "$CONFIG_DIR/mqtt.env"
else
    printf 'Preserved existing %s.\n' "$CONFIG_DIR/mqtt.env"
fi

install -d -m 0755 "$SERVICE_DIR"
install -m 0644 systemd/raspi-display-mqtt.service "$SERVICE_DIR/$SERVICE_NAME"

printf '\nInstalled the bridge. As user pi, run:\n'
printf '  systemctl --user daemon-reload\n'
printf '  systemctl --user enable --now %s\n' "$SERVICE_NAME"
