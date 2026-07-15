#!/usr/bin/env bash
# standalone-installer-linux/build-standalone-installer-linux.sh
#
# Build a self-contained OFFLINE installer package for the Humboldt-Probe that
# installs on any already-running Debian target (the release the bundle was built
# for; default Debian 13 "trixie"), amd64 or arm64 -- no
# re-image, no internet, no apt repo. Linux counterpart to
# standalone-installer/build-standalone-installer.ps1.
#
# Assembles: this probe checkout + the offline bundle produced by
# scripts/prepare-offline-linux.sh + the TLS material passed via --tls-dir, into:
#
#   HumboldtProbe-Setup-linux/
#     install.sh                  <- run as root (self-sudo): deploy + hand off
#     config.env                  <- optional MQTT broker override
#     README.txt
#     src/  requirements.lock.txt  userconfig.example.txt
#     certs/  ca_certificate.pem client_certificate.pem client_key.pem
#     installers-linux/  python/ wheels/ debs/ bundle.manifest.linux.json
#     scripts/install-linux.sh    <- the probe's own service installer (verbatim)
#
# install.sh deploys the payload to /opt/humboldt-probe + /etc/humboldt-probe,
# hardens the key, then calls scripts/install-linux.sh (reused verbatim -- single
# source of truth for the offline runtime + service setup).
#
# The certs are NOT in this repo (deployment secrets); pass them via --tls-dir.
# The resulting package contains the fleet mTLS key -> treat as a secret.
#
# Usage: bash standalone-installer-linux/build-standalone-installer-linux.sh --tls-dir <dir> [--out <dir>] [--tar]
set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

TLS_DIR=""
OUT_DIR=""
MAKE_TAR=0
while [ $# -gt 0 ]; do
    case "$1" in
        --tls-dir) TLS_DIR="$2"; shift 2 ;;
        --out)     OUT_DIR="$2"; shift 2 ;;
        --tar)     MAKE_TAR=1; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown flag: $1" ;;
    esac
done
[ -n "$TLS_DIR" ] || die "--tls-dir <dir with ca/client cert + key> is required."

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
inst="$repo_root/installers-linux"
svc_script="$repo_root/scripts/install-linux.sh"
lock="$repo_root/requirements.lock.txt"
manifest="$inst/bundle.manifest.linux.json"
[ -n "$OUT_DIR" ] || OUT_DIR="$here/dist"

echo
echo "=== build-standalone-installer-linux (probe) ==="
echo

# --- Validate inputs ------------------------------------------------------
[ -r "$repo_root/src/app.py" ]            || die "missing $repo_root/src/app.py -- run from a full probe checkout."
[ -r "$lock" ]                            || die "missing requirements.lock.txt."
[ -r "$repo_root/userconfig.example.txt" ] || die "missing userconfig.example.txt."
[ -r "$svc_script" ]                      || die "missing $svc_script -- checkout incomplete."
[ -r "$manifest" ]                        || die "missing $manifest -- run scripts/prepare-offline-linux.sh."
for c in ca_certificate.pem client_certificate.pem client_key.pem; do
    [ -r "$TLS_DIR/$c" ] || die "missing $TLS_DIR/$c -- pass --tls-dir <dir with ca/client cert + key>."
done

py_tarball="$(ls -1 "$inst"/python/cpython-*-install_only.tar.gz 2>/dev/null | sort -V | tail -1 || true)"
[ -n "$py_tarball" ] || die "no standalone Python tarball in $inst/python -- run prepare-offline-linux.sh."
ls "$inst"/wheels/*.whl >/dev/null 2>&1 || die "no wheels in $inst/wheels -- run prepare-offline-linux.sh."
ls "$inst"/debs/*.deb   >/dev/null 2>&1 || die "no .debs in $inst/debs -- run prepare-offline-linux.sh."

# --- Manifest gates (same idea as the Windows builder) --------------------
# Stale wheels would pass a count check yet fail pip --no-index on the target.
lock_hash="$(sha256sum "$lock" | awk '{print $1}')"
man_lock="$(grep -oE '"lockHash": *"[0-9a-f]{64}"' "$manifest" | head -1 | grep -oE '[0-9a-f]{64}' || true)"
[ -n "$man_lock" ] || die "manifest has no wheels.lockHash -- re-run prepare-offline-linux.sh."
[ "$man_lock" = "$lock_hash" ] || die "bundled wheels are stale vs requirements.lock.txt -- re-run prepare-offline-linux.sh."

# Python tarball on disk must match the manifest SHA256 (catches a corrupt/swapped binary).
man_py_sha="$(grep -oE '"sha256": *"[0-9a-f]{64}"' "$manifest" | head -1 | grep -oE '[0-9a-f]{64}' || true)"
if [ -n "$man_py_sha" ]; then
    disk_sha="$(sha256sum "$py_tarball" | awk '{print $1}')"
    [ "$man_py_sha" = "$disk_sha" ] || die "Python tarball SHA256 mismatch vs manifest -- re-run prepare-offline-linux.sh."
fi

# The bundle is self-describing: whatever Debian release the .debs were built for
# is recorded in the manifest, and install-linux.sh verifies the TARGET matches it
# at install time. Here we only require the field to be present (non-empty).
man_deb_codename="$(grep -oE '"debs":[^}]*"codename": *"[a-z]+"' "$manifest" | grep -oE '[a-z]+"' | tr -d '"' | tail -1 || true)"
[ -n "$man_deb_codename" ] || die "manifest has no debs.codename -- re-run prepare-offline-linux.sh."

echo "Inputs OK:"
echo "  target : Debian $man_deb_codename"
echo "  Python : $(basename "$py_tarball")"
echo "  wheels : $(ls -1 "$inst"/wheels/*.whl | wc -l | tr -d ' ') packages"
echo "  debs   : $(ls -1 "$inst"/debs/*.deb | wc -l | tr -d ' ') packages"
echo "  TLS    : $TLS_DIR"
echo

# --- Assemble -------------------------------------------------------------
pkg="$OUT_DIR/HumboldtProbe-Setup-linux"
rm -rf "$pkg"
mkdir -p "$pkg/certs" "$pkg/installers-linux" "$pkg/scripts"

# Source tree (exclude caches + the extracted runtime + any local venv).
if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
          "$repo_root/src/" "$pkg/src/"
else
    cp -a "$repo_root/src" "$pkg/src"
    find "$pkg/src" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$pkg/src" -name '*.pyc' -delete 2>/dev/null || true
fi
cp "$lock" "$pkg/requirements.lock.txt"
cp "$repo_root/userconfig.example.txt" "$pkg/userconfig.example.txt"

cp "$TLS_DIR/ca_certificate.pem"     "$pkg/certs/ca_certificate.pem"
cp "$TLS_DIR/client_certificate.pem" "$pkg/certs/client_certificate.pem"
# Private key written 0600 atomically (never briefly 0644 as cp would leave it).
install -m 600 "$TLS_DIR/client_key.pem" "$pkg/certs/client_key.pem"

# Bundle: python tarball + wheels + debs + manifest.
mkdir -p "$pkg/installers-linux/python"
cp "$py_tarball" "$pkg/installers-linux/python/"
cp -a "$inst/wheels" "$pkg/installers-linux/wheels"
cp -a "$inst/debs"   "$pkg/installers-linux/debs"
cp "$manifest" "$pkg/installers-linux/bundle.manifest.linux.json"

# The probe's service installer -- verbatim (single source of truth).
cp "$svc_script" "$pkg/scripts/install-linux.sh"

# Launcher + config + readme.
cp "$here/install.sh"           "$pkg/install.sh"
cp "$here/config.env.example"   "$pkg/config.env"
cp "$here/PACKAGE-README.txt"   "$pkg/README.txt"
chmod +x "$pkg/install.sh" "$pkg/scripts/install-linux.sh"

# --- Post-check -----------------------------------------------------------
must=(
    "$pkg/install.sh" "$pkg/config.env" "$pkg/README.txt"
    "$pkg/src/app.py" "$pkg/requirements.lock.txt" "$pkg/userconfig.example.txt"
    "$pkg/certs/client_key.pem" "$pkg/certs/ca_certificate.pem" "$pkg/certs/client_certificate.pem"
    "$pkg/installers-linux/wheels" "$pkg/installers-linux/debs"
    "$pkg/installers-linux/bundle.manifest.linux.json"
    "$pkg/scripts/install-linux.sh"
)
for m in "${must[@]}"; do [ -e "$m" ] || die "post-check failed, missing: $m"; done
ls "$pkg"/installers-linux/python/cpython-*-install_only.tar.gz >/dev/null 2>&1 \
    || die "post-check failed, no Python tarball in package."

size="$(du -sh "$pkg" | awk '{print $1}')"
echo "Package built: $pkg  ($size)"

if [ "$MAKE_TAR" = 1 ]; then
    tarball_out="$OUT_DIR/HumboldtProbe-Setup-linux.tar.gz"
    rm -f "$tarball_out"
    ( cd "$OUT_DIR" && tar -czf "HumboldtProbe-Setup-linux.tar.gz" "HumboldtProbe-Setup-linux" )
    echo "Tar: $tarball_out  ($(du -sh "$tarball_out" | awk '{print $1}'))"
fi

echo
echo "Copy the folder (or tar.gz) to the target, extract, then: sudo ./install.sh"
exit 0
