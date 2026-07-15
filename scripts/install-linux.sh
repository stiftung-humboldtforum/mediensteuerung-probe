#!/usr/bin/env bash
# scripts/install-linux.sh — install the Humboldt-Probe as a systemd service,
# fully offline. Linux counterpart to scripts/install-windows.ps1.
#
# Idempotent install / re-install. Bootstraps the runtime from the local offline
# bundle produced by scripts/prepare-offline-linux.sh (NO internet, NO apt repo):
#   - system packages  from installers-linux/debs/   (dpkg -i, full closure)
#   - Python           from installers-linux/python/  (standalone tarball -> INSTALL_PATH/python)
#   - deps             from installers-linux/wheels/  (pip --no-index vs requirements.lock.txt)
#   - service          rendered systemd unit (Type=notify, WatchdogSec) -> enable --now
#
# Deps go straight into the bundled interpreter (INSTALL_PATH/python) -- it IS the
# isolated environment, so there is no venv and no dependency on the distro Python.
#
# Re-running is safe: it re-installs deps, re-renders the unit, and restarts the
# service. The source tree must already be present at INSTALL_PATH (the standalone
# package's install.sh deploys it there before calling this; a git checkout lives
# there directly).
#
# Run as root:  sudo bash scripts/install-linux.sh [flags]
set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

[ "$(id -u)" = 0 ] || die "must run as root (sudo bash $0 ...)."

# --- Defaults (override via flags or the matching UPPER_CASE env var) ---------
INSTALL_PATH="${INSTALL_PATH:-/opt/humboldt-probe}"
CONFIG_DIR="${CONFIG_DIR:-/etc/humboldt-probe}"
CONFIG_FILE="${CONFIG_FILE:-}"
MQTT_HOST="${MQTT_HOST:-srv-control-avm}"
MQTT_PORT="${MQTT_PORT:-0}"          # 0 -> 8883 (TLS) / 1883 (--no-tls)
CA_CERT="${CA_CERT:-}"
CERT_FILE="${CERT_FILE:-}"
KEY_FILE="${KEY_FILE:-}"
NO_TLS="${NO_TLS:-0}"
LOGLEVEL="${LOGLEVEL:-INFO}"
CLIENT_ID="${CLIENT_ID:-}"
SERVICE_NAME="${SERVICE_NAME:-humboldt-probe}"
BUNDLE_DIR="${BUNDLE_DIR:-}"
PROBE_USER="${PROBE_USER:-probe}"
KIOSK_UID="${KIOSK_UID:-1000}"        # UID whose /run/user/<uid> + :0 the probe reads (display/audio)
KIOSK_USER="${KIOSK_USER:-}"          # optional: sets XAUTHORITY=/home/<user>/.Xauthority
SKIP_DEPS=0
SKIP_SYSPKGS=0
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --install-path) INSTALL_PATH="$2"; shift 2 ;;
        --config-dir)   CONFIG_DIR="$2"; shift 2 ;;
        --config-file)  CONFIG_FILE="$2"; shift 2 ;;
        --mqtt-host)    MQTT_HOST="$2"; shift 2 ;;
        --mqtt-port)    MQTT_PORT="$2"; shift 2 ;;
        --ca)           CA_CERT="$2"; shift 2 ;;
        --cert)         CERT_FILE="$2"; shift 2 ;;
        --key)          KEY_FILE="$2"; shift 2 ;;
        --no-tls)       NO_TLS=1; shift ;;
        --loglevel)     LOGLEVEL="$2"; shift 2 ;;
        --client-id)    CLIENT_ID="$2"; shift 2 ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        --bundle-dir)   BUNDLE_DIR="$2"; shift 2 ;;
        --probe-user)   PROBE_USER="$2"; shift 2 ;;
        --kiosk-uid)    KIOSK_UID="$2"; shift 2 ;;
        --kiosk-user)   KIOSK_USER="$2"; shift 2 ;;
        --skip-deps)    SKIP_DEPS=1; shift ;;
        --skip-syspkgs) SKIP_SYSPKGS=1; shift ;;
        --force)        FORCE=1; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown flag: $1" ;;
    esac
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
[ -n "$BUNDLE_DIR" ] || BUNDLE_DIR="$repo_root/installers-linux"
lock="$INSTALL_PATH/requirements.lock.txt"
[ -r "$lock" ] || lock="$repo_root/requirements.lock.txt"
manifest="$BUNDLE_DIR/bundle.manifest.linux.json"
PYBIN="$INSTALL_PATH/python/bin/python3"

[ -r "$INSTALL_PATH/src/app.py" ] || die "no src/app.py under $INSTALL_PATH -- deploy the source tree there first."

# --- Pre-flight: release match --------------------------------------------
# The .deb closure + wheels are built for one release/arch. Refuse a mismatch
# (dpkg would install foreign-ABI packages) unless explicitly overridden.
if [ -r "$manifest" ] && [ -r /etc/os-release ]; then
    want_codename="$(sed -nE 's/.*"codename": *"([a-z]+)".*/\1/p' "$manifest" | head -1)"
    # shellcheck disable=SC1091
    have_codename="$(. /etc/os-release; echo "${VERSION_CODENAME:-}")"
    if [ -n "$want_codename" ] && [ -n "$have_codename" ] && [ "$want_codename" != "$have_codename" ]; then
        if [ "${IGNORE_CODENAME:-0}" = 1 ]; then
            echo "WARN: bundle targets '$want_codename' but host is '$have_codename' (IGNORE_CODENAME=1)."
        else
            die "bundle targets Debian '$want_codename' but this host is '$have_codename'. The .debs/wheels will not match. Rebuild the bundle on a '$have_codename' box, or set IGNORE_CODENAME=1 to force."
        fi
    fi
fi

# Arch match: cross-architecture is out of scope; a wrong-arch bundle would let
# dpkg unpack foreign-ABI .debs. Cheap manifest-vs-dpkg guard, same override style.
if [ -r "$manifest" ] && command -v dpkg >/dev/null 2>&1; then
    want_arch="$(sed -nE 's/.*"arch": *"([a-z0-9_]+)".*/\1/p' "$manifest" | head -1)"
    have_arch="$(dpkg --print-architecture 2>/dev/null || echo '')"
    if [ -n "$want_arch" ] && [ -n "$have_arch" ] && [ "$want_arch" != "$have_arch" ]; then
        if [ "${IGNORE_ARCH:-0}" = 1 ]; then
            echo "WARN: bundle arch '$want_arch' != host arch '$have_arch' (IGNORE_ARCH=1)."
        else
            die "bundle targets arch '$want_arch' but this host is '$have_arch'. Build on a matching-arch host, or set IGNORE_ARCH=1 to force."
        fi
    fi
fi

echo "=== Humboldt-Probe Linux install ==="
echo "  install path : $INSTALL_PATH"
echo "  config dir   : $CONFIG_DIR"
echo "  service      : $SERVICE_NAME"
echo "  broker       : $MQTT_HOST"

# --- Step 1: system packages (offline .deb closure) -----------------------
step "Step 1: system packages (offline)"
if [ "$SKIP_SYSPKGS" = 1 ]; then
    echo "  -> --skip-syspkgs: assuming pipewire/xrandr already present."
elif [ -d "$BUNDLE_DIR/debs" ] && ls "$BUNDLE_DIR/debs"/*.deb >/dev/null 2>&1; then
    # Install ONLY packages not already present (at any version). The bundle
    # carries the full recursive closure, most of which a real target already
    # has; force-installing all of it would needlessly churn (and possibly
    # up/downgrade) the target's base (libc6/pam/perl/...). Installing just the
    # genuinely-missing set (pipewire/wireplumber/mosquitto-clients + their
    # missing deps) is what actually needs to happen, and it sidesteps the
    # pre-dependency ordering errors that a whole-closure dpkg hits.
    echo "  -> selecting packages not already installed ..."
    to_install=()
    for deb in "$BUNDLE_DIR"/debs/*.deb; do
        pkg="$(dpkg-deb -f "$deb" Package 2>/dev/null)"
        [ -n "$pkg" ] || continue
        if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
            continue
        fi
        to_install+=("$deb")
    done
    if [ ${#to_install[@]} -eq 0 ]; then
        echo "  -> all bundled packages already installed."
    else
        echo "  -> installing ${#to_install[@]} missing package(s) offline ..."
        # Two dpkg passes: the second resolves pre-dependency ordering that can
        # trip the first (dpkg processes the given set in one shot; a pre-dep
        # satisfied later in the same set fails on pass 1). --configure -a then
        # settles anything half-configured. --refuse-downgrade is a safety net.
        # No repo access. The real success gate is the wpctl/xrandr check below.
        dpkg -i --refuse-downgrade "${to_install[@]}" >/dev/null 2>&1 || true
        dpkg -i --refuse-downgrade "${to_install[@]}" >/dev/null 2>&1 || true
        dpkg --configure -a >/dev/null 2>&1 || true
        echo "  -> done."
    fi
else
    echo "  -> no bundled debs at $BUNDLE_DIR/debs; assuming system packages already present."
fi
# Verify the two the probe actually needs at runtime (mosquitto-clients is only
# for the smoke-test, not the probe). Fail loud rather than ship a probe that
# silently returns empty display/audio.
missing=()
command -v wpctl  >/dev/null 2>&1 || missing+=("wpctl (pipewire/wireplumber)")
command -v xrandr >/dev/null 2>&1 || missing+=("xrandr (x11-xserver-utils)")
if [ ${#missing[@]} -gt 0 ]; then
    die "required runtime tools missing after package step: ${missing[*]}. Bundle incomplete, or re-run prepare-offline-linux.sh."
fi
echo "  -> wpctl + xrandr present."

# --- Step 2: standalone Python --------------------------------------------
step "Step 2: standalone Python"
if [ -x "$PYBIN" ] && [ "$FORCE" != 1 ]; then
    echo "  -> already present at $PYBIN (use --force to re-extract)."
else
    tarball="$(ls -1 "$BUNDLE_DIR"/python/cpython-*-install_only.tar.gz 2>/dev/null | sort -V | tail -1 || true)"
    [ -n "$tarball" ] || die "no standalone Python tarball in $BUNDLE_DIR/python -- run prepare-offline-linux.sh."
    # Optional integrity check against the manifest sha256.
    want_sha="$(grep -oE '"sha256": *"[0-9a-f]{64}"' "$manifest" 2>/dev/null | head -1 | grep -oE '[0-9a-f]{64}' || true)"
    if [ -n "$want_sha" ]; then
        got_sha="$(sha256sum "$tarball" | awk '{print $1}')"
        [ "$want_sha" = "$got_sha" ] || die "Python tarball SHA256 mismatch vs manifest (corrupt/swapped bundle)."
    fi
    echo "  -> extracting $(basename "$tarball") ..."
    rm -rf "$INSTALL_PATH/python"
    tar -xzf "$tarball" -C "$INSTALL_PATH"     # yields $INSTALL_PATH/python/
    [ -x "$PYBIN" ] || die "extraction did not produce $PYBIN."
    echo "  -> Python: $("$PYBIN" --version 2>&1)"
fi

# --- Step 3: Python dependencies (offline) --------------------------------
step "Step 3: Python dependencies (offline)"
if [ "$SKIP_DEPS" = 1 ]; then
    echo "  -> --skip-deps: assuming dependencies already installed."
elif [ -d "$BUNDLE_DIR/wheels" ] && ls "$BUNDLE_DIR/wheels"/*.whl >/dev/null 2>&1; then
    [ -r "$lock" ] || die "requirements.lock.txt not found (looked in $INSTALL_PATH and $repo_root)."
    # --no-index so a missing wheel is a hard error, never a silent PyPI fallback
    # (the target cannot reach it). win32-marked lines in the lock are skipped by
    # pip's environment-marker evaluation on Linux.
    "$PYBIN" -m pip install --no-index --find-links "$BUNDLE_DIR/wheels" -r "$lock"
    echo "  -> dependencies installed offline."
else
    die "no wheels at $BUNDLE_DIR/wheels -- run prepare-offline-linux.sh (or pass --skip-deps)."
fi

# --- Step 4: service account + directories --------------------------------
step "Step 4: service account + directories"
if ! id "$PROBE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "$PROBE_USER"
    echo "  -> created system user '$PROBE_USER'."
else
    echo "  -> user '$PROBE_USER' exists."
fi
mkdir -p "$CONFIG_DIR"
chown -R "$PROBE_USER:$PROBE_USER" "$INSTALL_PATH"
chown "$PROBE_USER:$PROBE_USER" "$CONFIG_DIR"

# --- Pre-flight: config + TLS ---------------------------------------------
[ -n "$CONFIG_FILE" ] || CONFIG_FILE="$CONFIG_DIR/userconfig.txt"
if [ ! -r "$CONFIG_FILE" ]; then
    if [ -r "$INSTALL_PATH/userconfig.example.txt" ]; then
        cp "$INSTALL_PATH/userconfig.example.txt" "$CONFIG_FILE"
        chown "$PROBE_USER:$PROBE_USER" "$CONFIG_FILE"
        echo "  -> seeded $CONFIG_FILE from userconfig.example.txt (review PROBE_METHODS/CAPABILITIES)."
    else
        die "config file '$CONFIG_FILE' not found and no userconfig.example.txt to seed from."
    fi
fi

if [ "$MQTT_PORT" = 0 ]; then
    if [ "$NO_TLS" = 1 ]; then MQTT_PORT=1883; else MQTT_PORT=8883; fi
fi

if [ "$NO_TLS" != 1 ]; then
    [ -n "$CA_CERT" ]   || CA_CERT="$CONFIG_DIR/ca_certificate.pem"
    [ -n "$CERT_FILE" ] || CERT_FILE="$CONFIG_DIR/client_certificate.pem"
    [ -n "$KEY_FILE" ]  || KEY_FILE="$CONFIG_DIR/client_key.pem"
    for f in "$CA_CERT" "$CERT_FILE" "$KEY_FILE"; do
        [ -r "$f" ] || die "TLS material missing: '$f'. Pass --ca/--cert/--key, or --no-tls."
    done
    # Harden the fleet mTLS private key: owner-only read. A leak is fleet-wide.
    chown "$PROBE_USER:$PROBE_USER" "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    echo "  -> client_key.pem hardened (0600, owner $PROBE_USER)."
fi

# --- Step 5: sudoers for shutdown/reboot ----------------------------------
# _linux.py invokes 'sudo /sbin/shutdown now' / 'sudo /sbin/reboot now'. Grant
# exactly those (plus the /usr/sbin aliases in case /sbin is not usr-merged),
# NOPASSWD, and nothing else. Validate before install -- a malformed drop-in
# breaks sudo system-wide.
step "Step 5: sudoers (shutdown/reboot NOPASSWD)"
# sudo skips any /etc/sudoers.d file whose name contains a '.' (or other
# non [A-Za-z0-9_-] chars), so derive the drop-in basename by replacing them --
# otherwise --service-name humboldt-probe.kosmo would write a file sudo ignores,
# silently losing the shutdown/reboot grant.
sudoers_base="$(printf '%s' "$SERVICE_NAME" | tr -c 'A-Za-z0-9_-' '_')"
sudoers_file="/etc/sudoers.d/${sudoers_base}"
sudoers_tmp="$(mktemp)"
cat > "$sudoers_tmp" <<EOF
# Managed by install-linux.sh -- allow the probe user to power the box off/reboot.
$PROBE_USER ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/reboot, /usr/sbin/shutdown, /usr/sbin/reboot
EOF
if visudo -cf "$sudoers_tmp" >/dev/null 2>&1; then
    install -m 0440 -o root -g root "$sudoers_tmp" "$sudoers_file"
    echo "  -> installed $sudoers_file."
else
    rm -f "$sudoers_tmp"
    die "generated sudoers fragment failed visudo -c -- refusing to install (would break sudo)."
fi
rm -f "$sudoers_tmp"

# --- Step 6: render + install the systemd unit ----------------------------
step "Step 6: systemd unit"
unit_path="/etc/systemd/system/${SERVICE_NAME}.service"

# Build the ExecStart argument line. --no-tls omits the cert args; --client-id is
# only emitted when set (an empty value would pin an empty identity instead of
# falling back to socket.getfqdn()).
exec_args="src/app.py --config_file=$CONFIG_FILE --mqtt_hostname=$MQTT_HOST --mqtt_port=$MQTT_PORT --loglevel=$LOGLEVEL"
if [ "$NO_TLS" = 1 ]; then
    exec_args="$exec_args --no_tls"
else
    exec_args="$exec_args --ca_certificate=$CA_CERT --certfile=$CERT_FILE --keyfile=$KEY_FILE"
fi
[ -n "$CLIENT_ID" ] && exec_args="$exec_args --client_id=$CLIENT_ID"

xauth_line=""
[ -n "$KIOSK_USER" ] && xauth_line="Environment=XAUTHORITY=/home/$KIOSK_USER/.Xauthority"

cat > "$unit_path" <<EOF
# Rendered by scripts/install-linux.sh -- do not edit by hand (re-run the installer).
[Unit]
Description=Humboldt-Probe — MQTT health/control agent
Documentation=https://github.com/stiftung-humboldtforum/mediensteuerung-probe
After=network-online.target pipewire.service
Wants=network-online.target
StartLimitBurst=10
StartLimitIntervalSec=300

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=30s

User=$PROBE_USER
Group=$PROBE_USER
WorkingDirectory=$INSTALL_PATH

# Probe needs the active display + PipeWire session of the logged-in kiosk user
# for the display/is_muted sensors. Adjust KIOSK_UID/KIOSK_USER if not 1000.
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/$KIOSK_UID
Environment=PYTHONDONTWRITEBYTECODE=1
$xauth_line

ExecStart=$PYBIN $exec_args

Restart=on-failure
RestartSec=5

# Hardening. ProtectHome / PrivateTmp are deliberately OFF: the display + audio
# sensors need the kiosk user's session -- ProtectHome would blank /run/user
# (the PipeWire socket wpctl talks to) and PrivateTmp would hide the X11
# /tmp/.X11-unix socket. ProtectSystem=strict is safe: the probe writes nothing
# (logs go to journald; PYTHONDONTWRITEBYTECODE avoids .pyc writes under /opt).
# No ReadWritePaths -- a non-existent one would fail the namespace (226/NAMESPACE).
NoNewPrivileges=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
EOF
echo "  -> wrote $unit_path"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
# '|| true' so a unit that fails to start does not abort the script under set -e
# before the Verify block below runs its diagnostic journal dump.
systemctl restart "$SERVICE_NAME" || true

# --- Verify ---------------------------------------------------------------
step "Verify"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "  -> $SERVICE_NAME is ACTIVE."
else
    echo "  -> WARNING: $SERVICE_NAME is not active. Recent log:"
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
    die "service failed to come up -- see log above."
fi

echo
echo "Done. Manage with:"
echo "  systemctl status $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
echo "  bash $INSTALL_PATH/scripts/smoke-test.sh $MQTT_HOST"
if [ "$PROBE_USER" != "$KIOSK_USER" ]; then
    echo
    echo "NOTE: the service runs as '$PROBE_USER'. The display + audio sensors read"
    echo "      the kiosk session at /run/user/$KIOSK_UID (mode 0700, owned by uid $KIOSK_UID),"
    echo "      which '$PROBE_USER' cannot enter -- so is_muted/display may stay empty."
    echo "      If they do, re-run with --probe-user <kiosk-login-user> (MQTT/temps/fans/"
    echo "      uptime work either way). Verify: bash $INSTALL_PATH/scripts/hardware-test-linux.sh"
fi
exit 0
