#!/usr/bin/env bash
# Installs the systemd units for the dashboard web service and its periodic-task timer.
# Run after ./setup.sh, from any directory:
#   sudo deploy/install.sh [--user NAME] [--name UNIT] [--no-start]
# Preview without touching the system:   deploy/install.sh --render-only DIR
# UNIT defaults to jabs-dashboard-v2; use --name jabs-dashboard at cutover.
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$APP_DIR"

SERVICE_USER=${SUDO_USER:-}
NAME=jabs-dashboard-v2
START=1
RENDER_ONLY=""

usage() {
    echo "Usage: sudo $0 [--user NAME] [--name UNIT] [--no-start]   |   $0 --render-only DIR" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --user) [[ $# -ge 2 ]] || usage; SERVICE_USER=$2; shift 2 ;;
        --name) [[ $# -ge 2 ]] || usage; NAME=$2; shift 2 ;;
        --no-start) START=0; shift ;;
        --render-only) [[ $# -ge 2 ]] || usage; RENDER_ONLY=$2; shift 2 ;;
        *) usage ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

[[ $NAME =~ ^[a-z][a-z0-9-]*$ ]] || die "unit name must be lowercase letters, digits and dashes"
[[ $APP_DIR =~ ^[A-Za-z0-9_./-]+$ ]] || die "app directory path has unsupported characters: $APP_DIR"
if [[ -z $RENDER_ONLY ]]; then
    [[ -n $SERVICE_USER ]] || die "cannot tell which user should run the service; pass --user NAME"
fi
SERVICE_USER=${SERVICE_USER:-nobody}
[[ $SERVICE_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "unusual user name: $SERVICE_USER"
[[ $SERVICE_USER != root ]] || die "do not run the service as root"

render() {  # render SRC DEST
    sed -e "s|@USER@|$SERVICE_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@NAME@|$NAME|g" "$1" > "$2"
}

UNITS=("$NAME.service:dashboard.service" "$NAME-tick.service:dashboard-tick.service" "$NAME-tick.timer:dashboard-tick.timer")

if [[ -n $RENDER_ONLY ]]; then
    mkdir -p "$RENDER_ONLY"
    for pair in "${UNITS[@]}"; do
        render "$APP_DIR/deploy/${pair#*:}" "$RENDER_ONLY/${pair%%:*}"
    done
    echo "Rendered units into $RENDER_ONLY"
    exit 0
fi

[[ $EUID -eq 0 ]] || die "run with sudo"
id "$SERVICE_USER" >/dev/null 2>&1 || die "user $SERVICE_USER does not exist"
[[ -x $APP_DIR/.venv/bin/python ]] || die "no .venv found; run ./setup.sh first"
[[ -f $APP_DIR/config/config.yaml ]] || die "config/config.yaml missing; run ./setup.sh first"

# Files created under sudo end up root-owned and unreadable by the service user.
mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER": "$APP_DIR/data"
for entry in .env config/config.yaml config/agents.yaml; do
    [[ -e $APP_DIR/$entry ]] && chown "$SERVICE_USER": "$APP_DIR/$entry"
done
runuser -u "$SERVICE_USER" -- "$APP_DIR/.venv/bin/python" -m app config \
    || die "config check failed as $SERVICE_USER (fix config or file ownership)"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
for pair in "${UNITS[@]}"; do
    render "$APP_DIR/deploy/${pair#*:}" "$tmp"
    install -m 0644 -o root -g root "$tmp" "/etc/systemd/system/${pair%%:*}"
    echo "Installed /etc/systemd/system/${pair%%:*}"
done
systemctl daemon-reload

if ((START)); then
    systemctl enable --now "$NAME.service" "$NAME-tick.timer"
else
    systemctl enable "$NAME.service" "$NAME-tick.timer"
fi

PORT=$(cd "$APP_DIR" && runuser -u "$SERVICE_USER" -- .venv/bin/python -c \
    'from app.config import load_config; print(load_config().server.port)')

cat <<EOF

Done.
  Status:  systemctl status $NAME $NAME-tick.timer
  Logs:    journalctl -u $NAME -f        (periodic tasks: journalctl -u $NAME-tick)
  Timer:   systemctl list-timers $NAME-tick.timer
  Web UI:  http://$(hostname):$PORT/
  Restart after editing config.yaml or .env:  sudo systemctl restart $NAME
EOF
