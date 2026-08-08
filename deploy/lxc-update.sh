#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/battery-tracker
SERVICE_NAME=battery-tracker
SERVICE_USER=batterytracker
UPDATE_CONFIG=/etc/battery-tracker-update.conf
DEFAULT_REPOSITORY_URL=https://github.com/jockelandelius/Battery-tracker.git

info() {
  printf '\033[1;34m==>\033[0m %s\n' "$*"
}

error() {
  printf '\033[1;31mFel:\033[0m %s\n' "$*" >&2
}

on_error() {
  error "Uppdateringen avbröts på rad $1. Kontrollera med: systemctl status $SERVICE_NAME"
}

trap 'on_error $LINENO' ERR

if [[ $(id -u) -ne 0 ]]; then
  error "Kör update som root i Battery Tracker-containern."
  exit 1
fi

if [[ -f "$UPDATE_CONFIG" ]]; then
  source "$UPDATE_CONFIG"
fi

REPOSITORY_URL="${REPOSITORY_URL:-$DEFAULT_REPOSITORY_URL}"
REPOSITORY_BRANCH="${REPOSITORY_BRANCH:-main}"
command -v git >/dev/null 2>&1 || { error "Git saknas. Kör om installationsskriptet."; exit 1; }
[[ -d "$APP_DIR" ]] || { error "Hittar inte $APP_DIR."; exit 1; }

info "Kontrollerar konsolinställningar..."
GETTY_OVERRIDE=/etc/systemd/system/container-getty@1.service.d/override.conf
install -d -m 0755 "$(dirname "$GETTY_OVERRIDE")"
cat > "$GETTY_OVERRIDE" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud tty%I 115200,38400,9600 $TERM
EOF
systemctl daemon-reload
info "Konsolinställningen används vid nästa anslutning till Proxmox-konsolen."

SOURCE_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$SOURCE_DIR"
}
trap cleanup EXIT

info "Hämtar senaste kod från $REPOSITORY_URL ($REPOSITORY_BRANCH)..."
git clone --quiet --depth 1 --branch "$REPOSITORY_BRANCH" "$REPOSITORY_URL" "$SOURCE_DIR"
NEW_VERSION="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
CURRENT_VERSION="$(cat "$APP_DIR/.release" 2>/dev/null || true)"

if [[ "$NEW_VERSION" == "$CURRENT_VERSION" ]]; then
  printf 'Batteribanken är redan uppdaterad (%s).\n' "$(git -C "$SOURCE_DIR" rev-parse --short HEAD)"
  exit 0
fi

info "Installerar beroenden..."
"$APP_DIR/.venv/bin/pip" install --quiet -r "$SOURCE_DIR/requirements.txt"
info "Uppdaterar appfiler..."
systemctl stop "$SERVICE_NAME"
cp "$SOURCE_DIR/app.py" "$SOURCE_DIR/schema.sql" "$SOURCE_DIR/requirements.txt" "$APP_DIR/"
cp -rT "$SOURCE_DIR/templates" "$APP_DIR/templates"
cp -rT "$SOURCE_DIR/static" "$APP_DIR/static"
install -m 755 "$SOURCE_DIR/deploy/lxc-update.sh" /usr/local/bin/update
printf '%s\n' "$NEW_VERSION" > "$APP_DIR/.release"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
info "Startar Batteribanken..."
systemctl start "$SERVICE_NAME"
systemctl is-active --quiet "$SERVICE_NAME"

printf '\033[1;32mKlart:\033[0m Batteribanken är uppdaterad till %s.\n' "$(git -C "$SOURCE_DIR" rev-parse --short HEAD)"
