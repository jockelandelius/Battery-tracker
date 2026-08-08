#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/battery-tracker
SERVICE_NAME=battery-tracker
SERVICE_USER=batterytracker
UPDATE_CONFIG=/etc/battery-tracker-update.conf
DEFAULT_REPOSITORY_URL=https://github.com/jockelandelius/Battery-tracker.git

if [[ $(id -u) -ne 0 ]]; then
  echo "Kör update som root i Battery Tracker-containern." >&2
  exit 1
fi

if [[ -f "$UPDATE_CONFIG" ]]; then
  source "$UPDATE_CONFIG"
fi

REPOSITORY_URL="${REPOSITORY_URL:-$DEFAULT_REPOSITORY_URL}"
REPOSITORY_BRANCH="${REPOSITORY_BRANCH:-main}"
command -v git >/dev/null 2>&1 || { echo "Git saknas. Kör om installationsskriptet." >&2; exit 1; }
[[ -d "$APP_DIR" ]] || { echo "Hittar inte $APP_DIR." >&2; exit 1; }

GETTY_OVERRIDE=/etc/systemd/system/container-getty@1.service.d/override.conf
install -d -m 0755 "$(dirname "$GETTY_OVERRIDE")"
cat > "$GETTY_OVERRIDE" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud tty%I 115200,38400,9600 $TERM
EOF
systemctl daemon-reload
systemctl restart container-getty@1.service

SOURCE_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$SOURCE_DIR"
}
trap cleanup EXIT

echo "Hämtar senaste kod från $REPOSITORY_URL ($REPOSITORY_BRANCH)..."
git clone --quiet --depth 1 --branch "$REPOSITORY_BRANCH" "$REPOSITORY_URL" "$SOURCE_DIR"
NEW_VERSION="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
CURRENT_VERSION="$(cat "$APP_DIR/.release" 2>/dev/null || true)"

if [[ "$NEW_VERSION" == "$CURRENT_VERSION" ]]; then
  echo "Batteribanken är redan uppdaterad ($(git -C "$SOURCE_DIR" rev-parse --short HEAD)); konsolens automatiska inloggning är konfigurerad."
  exit 0
fi

"$APP_DIR/.venv/bin/pip" install --quiet -r "$SOURCE_DIR/requirements.txt"
systemctl stop "$SERVICE_NAME"
cp "$SOURCE_DIR/app.py" "$SOURCE_DIR/schema.sql" "$SOURCE_DIR/requirements.txt" "$APP_DIR/"
cp -rT "$SOURCE_DIR/templates" "$APP_DIR/templates"
cp -rT "$SOURCE_DIR/static" "$APP_DIR/static"
install -m 755 "$SOURCE_DIR/deploy/lxc-update.sh" /usr/local/bin/update
printf '%s\n' "$NEW_VERSION" > "$APP_DIR/.release"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
systemctl start "$SERVICE_NAME"
systemctl is-active --quiet "$SERVICE_NAME"

echo "Batteribanken är uppdaterad till $(git -C "$SOURCE_DIR" rev-parse --short HEAD)."
