#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "Kör skriptet som root i en Debian/Ubuntu LXC."
  exit 1
fi

APP_DIR=/opt/battery-tracker
DATA_DIR=/var/lib/battery-tracker
SERVICE_USER=batterytracker
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_URL="${BATTERY_TRACKER_REPO:-https://github.com/jockelandelius/Battery-tracker.git}"
REPOSITORY_BRANCH="${BATTERY_TRACKER_BRANCH:-main}"

apt-get update
apt-get install -y git python3 python3-venv

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR" "$DATA_DIR"
cp "$SOURCE_DIR/app.py" "$SOURCE_DIR/schema.sql" "$SOURCE_DIR/requirements.txt" "$APP_DIR/"
cp -rT "$SOURCE_DIR/templates" "$APP_DIR/templates"
cp -rT "$SOURCE_DIR/static" "$APP_DIR/static"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
install -m 755 "$SCRIPT_DIR/lxc-update.sh" /usr/local/bin/update

umask 077
printf 'REPOSITORY_URL=%q\nREPOSITORY_BRANCH=%q\n' "$REPOSITORY_URL" "$REPOSITORY_BRANCH" > /etc/battery-tracker-update.conf
if git -C "$SOURCE_DIR" rev-parse HEAD >/dev/null 2>&1; then
  git -C "$SOURCE_DIR" rev-parse HEAD > "$APP_DIR/.release"
else
  : > "$APP_DIR/.release"
fi

if [[ ! -f /etc/battery-tracker.env ]]; then
  umask 077
  printf 'SECRET_KEY=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" > /etc/battery-tracker.env
fi

cat > /etc/systemd/system/battery-tracker.service <<EOF
[Unit]
Description=Battery Tracker
After=network.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=DATABASE_PATH=$DATA_DIR/battery_tracker.db
EnvironmentFile=/etc/battery-tracker.env
ExecStart=$APP_DIR/.venv/bin/gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

GETTY_OVERRIDE=/etc/systemd/system/container-getty@1.service.d/override.conf
install -d -m 0755 "$(dirname "$GETTY_OVERRIDE")"
cat > "$GETTY_OVERRIDE" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud tty%I 115200,38400,9600 $TERM
EOF

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR"
systemctl daemon-reload
systemctl restart container-getty@1.service
systemctl enable --now battery-tracker
echo "Klart. Batteribanken lyssnar på port 8000."
