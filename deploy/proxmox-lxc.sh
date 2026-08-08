#!/usr/bin/env bash
# Battery Tracker for Proxmox VE
set -Eeuo pipefail

APP_NAME="Batteribanken"
REPOSITORY_URL="${BATTERY_TRACKER_REPO:-https://github.com/jockelandelius/Battery-tracker.git}"
ADVANCED_MODE=0

for argument in "$@"; do
  case "$argument" in
    --advanced) ADVANCED_MODE=1 ;;
    --help|-h)
      echo "Användning: bash proxmox-lxc.sh [--advanced]"
      exit 0
      ;;
    *)
      echo "Okänd flagga: $argument" >&2
      exit 1
      ;;
  esac
done

red() { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
blue() { printf '\033[1;34m%s\033[0m\n' "$*"; }
die() { red "Fel: $*"; exit 1; }

on_error() {
  red "Installationen avbröts på rad $1. Containern lämnas kvar för felsökning."
}
trap 'on_error $LINENO' ERR

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Kommandot '$1' saknas. Kör skriptet på en Proxmox VE-host."
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

rootfs_storages() {
  pvesm status -content rootdir 2>/dev/null | awk 'NR > 1 && $3 == "active" { print $1 }'
}

default_template_storage() {
  pvesm status -content vztmpl 2>/dev/null | awk 'NR > 1 && $3 == "active" { print $1; exit }'
}

choose_rootfs_storage() {
  storage_count=$(rootfs_storages | wc -l)
  (( storage_count > 0 )) || die "Hittar ingen aktiv lagring för LXC-diskar."
  blue "Välj lagring för LXC-disken:"
  storage_number=1
  while IFS= read -r storage_name; do
    printf '  %s) %s\n' "$storage_number" "$storage_name"
    ((storage_number += 1))
  done < <(rootfs_storages)
  while true; do
    read -r -p "Lagring (ange nummer): " selected_storage || die "Ett lagringsval krävs."
    if [[ "$selected_storage" =~ ^[1-9][0-9]*$ ]] && (( selected_storage <= storage_count )); then
      STORAGE=$(rootfs_storages | sed -n "$selected_storage"p)
      return
    fi
    red "Ange ett av numren i listan."
  done
}

storage_exists() {
  pvesm status 2>/dev/null | awk 'NR > 1 { print $1 }' | grep -Fxq "$1"
}

if [[ $(id -u) -ne 0 ]]; then
  die "Kör skriptet som root på Proxmox VE-hosten."
fi
for required_command in pveversion pct pveam pvesm pvesh; do
  require_command "$required_command"
done
[[ $(dpkg --print-architecture) == "amd64" ]] || die "Skriptet kräver en amd64-baserad Proxmox-host."

CTID="$(pvesh get /cluster/nextid)"
HOSTNAME="battery-tracker"
CORES=1
MEMORY=512
DISK_SIZE=4
BRIDGE="vmbr0"
STORAGE=""
TEMPLATE_STORAGE="$(default_template_storage)"

[[ -n "$TEMPLATE_STORAGE" ]] || die "Hittar ingen aktiv lagring för LXC-mallar."

if (( ! ADVANCED_MODE )); then
  [[ -t 0 ]] || die "Interaktiv terminal krävs för att välja LXC-lagring."
  blue "Välj installationsläge:"
  printf '  1) Standard (1 CPU, 512 MiB RAM, 4 GiB disk, DHCP)\n'
  printf '  2) Avancerat (ändra resurser, nätverk och mallagring)\n'
  read -r -p "Läge [1/2, standard=1]: " installation_mode
  [[ -n "$installation_mode" ]] || installation_mode=1
  case "$installation_mode" in
    1) ;;
    2) ADVANCED_MODE=1 ;;
    *) die "Välj 1 för standard eller 2 för avancerat läge." ;;
  esac
fi

if (( ADVANCED_MODE )); then
  blue "Avancerad konfiguration (tryck Enter för standardvärde)"
  input=""; read -r -p "Container-ID [$CTID]: " input || true; [[ -n "$input" ]] && CTID="$input"
  input=""; read -r -p "Värdnamn [$HOSTNAME]: " input || true; [[ -n "$input" ]] && HOSTNAME="$input"
  input=""; read -r -p "CPU-kärnor [$CORES]: " input || true; [[ -n "$input" ]] && CORES="$input"
  input=""; read -r -p "Minne i MiB [$MEMORY]: " input || true; [[ -n "$input" ]] && MEMORY="$input"
  input=""; read -r -p "Disk i GiB [$DISK_SIZE]: " input || true; [[ -n "$input" ]] && DISK_SIZE="$input"
  input=""; read -r -p "Nätverksbrygga [$BRIDGE]: " input || true; [[ -n "$input" ]] && BRIDGE="$input"
  input=""; read -r -p "Lagring för mall [$TEMPLATE_STORAGE]: " input || true; [[ -n "$input" ]] && TEMPLATE_STORAGE="$input"
fi

choose_rootfs_storage

is_positive_integer "$CTID" || die "Container-ID måste vara ett positivt heltal."
is_positive_integer "$CORES" || die "Antal CPU-kärnor måste vara ett positivt heltal."
is_positive_integer "$MEMORY" || die "Minne måste vara ett positivt heltal."
is_positive_integer "$DISK_SIZE" || die "Diskstorlek måste vara ett positivt heltal."
[[ "$HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "Värdnamnet är ogiltigt."
storage_exists "$TEMPLATE_STORAGE" || die "Mallagringen '$TEMPLATE_STORAGE' finns inte."
ip link show "$BRIDGE" >/dev/null 2>&1 || die "Nätverksbryggan '$BRIDGE' finns inte."
if pct status "$CTID" >/dev/null 2>&1; then
  die "Container-ID $CTID används redan."
fi

blue "Skapar $APP_NAME med följande standardvärden:"
printf '  CTID: %s · Namn: %s · CPU: %s · Minne: %s MiB · Disk: %s GiB\n' "$CTID" "$HOSTNAME" "$CORES" "$MEMORY" "$DISK_SIZE"
printf '  Nätverk: DHCP via %s · Disklagring: %s · Mallagring: %s\n' "$BRIDGE" "$STORAGE" "$TEMPLATE_STORAGE"

if [[ -t 0 ]]; then
  read -r -p "Fortsätt? [Y/n] " answer
  [[ ${answer:-Y} =~ ^[Yy]$ ]] || exit 0
fi

blue "Uppdaterar listan över Debian-mallar..."
pveam update
TEMPLATE="$(pveam available --section system | awk '$2 ~ /^debian-12-standard_[0-9][0-9.\-]*_amd64\.tar\.zst$/ { print $2 }' | sort -V | tail -n 1)"
[[ -n "$TEMPLATE" ]] || die "Hittar ingen Debian 12 LXC-mall i Proxmox katalog."

if ! pveam list "$TEMPLATE_STORAGE" | awk '{ print $1 }' | grep -Fxq "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE"; then
  blue "Hämtar $TEMPLATE..."
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

blue "Skapar oprivilegierad LXC-container..."
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores "$CORES" \
  --memory "$MEMORY" \
  --swap 512 \
  --rootfs "$STORAGE:$DISK_SIZE" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
  --unprivileged 1 \
  --onboot 1 \
  --start 0

blue "Startar containern och installerar applikationen..."
pct start "$CTID"
REPOSITORY_URL_QUOTED="$(printf '%q' "$REPOSITORY_URL")"
pct exec "$CTID" -- bash -c "
  set -Eeuo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates git
  git clone --depth 1 $REPOSITORY_URL_QUOTED /tmp/battery-tracker-source
  cd /tmp/battery-tracker-source
  BATTERY_TRACKER_REPO=$REPOSITORY_URL_QUOTED bash deploy/lxc-install.sh
  rm -rf /tmp/battery-tracker-source
"

CONTAINER_IP=""
for _ in {1..15}; do
  CONTAINER_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{ print $1 }' || true)"
  [[ -n "$CONTAINER_IP" ]] && break
  sleep 2
done

green "$APP_NAME är installerad."
printf '  Container: %s (%s)\n' "$CTID" "$HOSTNAME"
if [[ -n "$CONTAINER_IP" ]]; then
  printf '  Öppna: http://%s:8000\n' "$CONTAINER_IP"
else
  printf '  IP-adress kunde inte läsas ännu. Kontrollera den i Proxmox och öppna port 8000.\n'
fi
