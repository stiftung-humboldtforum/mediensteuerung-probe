#!/usr/bin/env bash
# standalone-installer-linux/install.sh — offline installer launcher.
#
# Runs inside the self-contained package built by
# build-standalone-installer-linux.sh. Self-elevates via sudo, deploys the probe
# to /opt/humboldt-probe + /etc/humboldt-probe, hardens the mTLS key, then hands
# off to scripts/install-linux.sh (the probe's own offline runtime + service
# installer). No internet, no apt repo required.
#
# Deploy is done here (not in install-linux.sh) so install-linux.sh keeps its
# "install from a checkout in place" semantics.
#
# Usage:  sudo ./install.sh      (or ./install.sh -- it re-execs itself with sudo)
set -euo pipefail

# --- self-elevate ---------------------------------------------------------
if [ "$(id -u)" != 0 ]; then
    echo "Requesting root via sudo ..."
    exec sudo bash "$0" "$@"
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="/opt/humboldt-probe"
config_dir="/etc/humboldt-probe"

echo "=== Humboldt-Probe standalone installer (Linux) ==="

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- optional config.env: MQTT_HOST / MQTT_PORT / CLIENT_ID ----------------
mqtt_host="srv-control-avm"
mqtt_port="8883"
client_id=""
cfg="$here/config.env"
if [ -r "$cfg" ]; then
    # '|| [ -n "$line" ]' so a final line without a trailing newline is still
    # processed (read returns non-zero at EOF but fills $line).
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"   # ltrim
        [ -z "$line" ] && continue
        case "$line" in \#*) continue ;; esac
        case "$line" in
            MQTT_HOST=*) mqtt_host="${line#MQTT_HOST=}" ;;
            MQTT_PORT=*) mqtt_port="${line#MQTT_PORT=}" ;;
            CLIENT_ID=*) client_id="${line#CLIENT_ID=}" ;;
        esac
    done < "$cfg"
fi
# trim surrounding whitespace on values
mqtt_host="$(echo "$mqtt_host" | xargs)"
mqtt_port="$(echo "$mqtt_port" | xargs)"
client_id="$(echo "$client_id" | xargs)"
case "$mqtt_port" in
    ''|*[!0-9]*) die "invalid MQTT port '$mqtt_port' in config.env (must be numeric)." ;;
esac
[ "$mqtt_port" -ge 1 ] && [ "$mqtt_port" -le 65535 ] || die "MQTT port '$mqtt_port' out of range 1-65535."
echo "MQTT broker: $mqtt_host:$mqtt_port"

# --- stop an existing service so its python releases files before we overwrite ---
svc="humboldt-probe"
if systemctl list-unit-files "${svc}.service" >/dev/null 2>&1 \
   && systemctl is-active --quiet "$svc" 2>/dev/null; then
    echo "Stopping existing $svc ..."
    systemctl stop "$svc" || true
fi

# --- deploy payload to the permanent install dir --------------------------
echo "Deploying probe to $target ..."
mkdir -p "$target"
deploy_dir() {  # src/ -> dst/ , mirror contents (no nesting on re-install)
    local s="$1" d="$2"
    mkdir -p "$d"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "$s/" "$d/"
    else
        rm -rf "$d"; mkdir -p "$d"; cp -a "$s/." "$d/"
    fi
}
deploy_dir "$here/src"     "$target/src"
deploy_dir "$here/scripts" "$target/scripts"
cp "$here/requirements.lock.txt"  "$target/requirements.lock.txt"
cp "$here/userconfig.example.txt" "$target/userconfig.example.txt"

# --- certs: key created 0600 atomically (no world-readable window) ---------
# config dir non-traversable to others; key written 0600 in one step via
# 'install -m' so it is never briefly 0644 the way cp-then-chmod leaves it.
install -d -m 0750 -o root -g root "$config_dir"
key="$config_dir/client_key.pem"
install -m 600 "$here/certs/client_key.pem" "$key"
echo "client_key.pem deployed (0600)."
cp "$here/certs/ca_certificate.pem"     "$config_dir/ca_certificate.pem"
cp "$here/certs/client_certificate.pem" "$config_dir/client_certificate.pem"
if [ ! -e "$config_dir/userconfig.txt" ]; then
    cp "$here/userconfig.example.txt" "$config_dir/userconfig.txt"
fi

# --- hand off to the probe's service installer (offline) -------------------
svc_installer="$here/scripts/install-linux.sh"
[ -r "$svc_installer" ] || die "$svc_installer not found -- package incomplete."

args=(
    --install-path "$target"
    --config-dir   "$config_dir"
    --bundle-dir   "$here/installers-linux"
    --mqtt-host    "$mqtt_host"
    --mqtt-port    "$mqtt_port"
)
[ -n "$client_id" ] && args+=(--client-id "$client_id")

bash "$svc_installer" "${args[@]}"
