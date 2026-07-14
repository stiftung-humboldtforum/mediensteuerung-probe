#!/usr/bin/env bash
# scripts/prepare-offline-linux.sh — fetch the OFFLINE install bundle for Linux.
#
# Linux counterpart to scripts/prepare-offline.ps1. Run this ONCE on a machine
# with internet that MATCHES the target (Debian 12 "bookworm", x86_64);
# afterwards scripts/install-linux.sh installs everything with NO internet.
#
# Why "matches the target": two of the three artefacts are release/arch-bound —
#   - the .deb closure is bookworm/amd64 packages (a jammy/noble .deb will not
#     dpkg cleanly onto bookworm), and
#   - the pip wheels are cp311 / manylinux x86_64.
# So this script refuses to run on a non-bookworm / non-amd64 host unless
# ALLOW_HOST_MISMATCH=1 is set (you are then on your own re: dpkg on the target).
#
# It populates installers-linux/ (gitignored):
#
#   installers-linux/python/cpython-3.11.*-x86_64-...-install_only.tar.gz
#                                            standalone Python runtime (+ .sha256 verified)
#   installers-linux/wheels/*.whl            pip wheels, pinned to requirements.lock.txt
#   installers-linux/debs/*.deb              pipewire/wireplumber/xrandr/mosquitto-clients
#                                            + full recursive dependency closure
#   installers-linux/bundle.manifest.linux.json   resolved versions + sha256 + lockHash
#
# Flags:
#   --force     re-fetch every component even if already present
#   --offline   no network; only validate that the bundle is present
#
# Exit: 0 = bundle ready, 1 = at least one component failed.
set -uo pipefail

# --- Target profile (change these two lines for a different arch/release) -----
ARCH='x86_64'
PBS_TRIPLE='x86_64-unknown-linux-gnu'   # python-build-standalone target triple
PY_MINOR='3.11'                          # bundled Python minor (any modern one; we ship it)
TARGET_CODENAME='bookworm'               # Debian 12
SYS_PKGS='pipewire wireplumber x11-xserver-utils mosquitto-clients'

# --- Args -----------------------------------------------------------------
FORCE=0
OFFLINE=0
for a in "$@"; do
    case "$a" in
        --force)   FORCE=1 ;;
        --offline) OFFLINE=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown arg: $a" >&2; exit 2 ;;
    esac
done

# --- Paths ----------------------------------------------------------------
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
inst="$repo_root/installers-linux"
py_dir="$inst/python"
wheels="$inst/wheels"
debs="$inst/debs"
lock="$repo_root/requirements.lock.txt"
manifest="$inst/bundle.manifest.linux.json"

mkdir -p "$inst" "$py_dir" "$wheels" "$debs"

errors=()
py_file=''; py_ver='unknown'; py_sha=''
deb_count=0; wheel_count=0
lock_hash=''

note()  { printf '%s\n' "$*"; }
head1() { printf -- '-- %s\n' "$*"; }
fail()  { errors+=("$1"); printf 'ERROR: %s\n' "$1" >&2; }

echo
echo "=== prepare-offline-linux: bundle -> $inst ==="
[ "$OFFLINE" = 1 ] && echo "    (offline mode -- validating existing files only)"
echo

# --- Host sanity ----------------------------------------------------------
# Not fatal in offline mode (we are only validating), but online we must build
# on the target profile or the artefacts will not apply cleanly.
host_codename=''
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    host_codename="$(. /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME:-}")"
fi
host_arch="$(uname -m 2>/dev/null || echo unknown)"
if [ "$OFFLINE" != 1 ]; then
    if [ "$host_codename" != "$TARGET_CODENAME" ] || [ "$host_arch" != "$ARCH" ]; then
        msg="build host is ${host_codename:-?}/${host_arch} but bundle targets ${TARGET_CODENAME}/${ARCH}"
        if [ "${ALLOW_HOST_MISMATCH:-0}" = 1 ]; then
            note "   WARN: $msg (ALLOW_HOST_MISMATCH=1 -> continuing)"
        else
            fail "$msg. Run on a ${TARGET_CODENAME}/${ARCH} box (or container), or set ALLOW_HOST_MISMATCH=1."
            echo; echo "prepare-offline-linux aborted."; exit 1
        fi
    fi
fi

# =============================================================================
# 1. Standalone Python (python-build-standalone, install_only, sha256-verified)
# =============================================================================
head1 "Standalone Python ${PY_MINOR}.x ($PBS_TRIPLE)"
existing_py="$(ls -1 "$py_dir"/cpython-${PY_MINOR}.*-"$PBS_TRIPLE"-install_only.tar.gz 2>/dev/null | sort -V | tail -1 || true)"

if [ "$OFFLINE" = 1 ]; then
    if [ -n "$existing_py" ]; then
        py_file="$existing_py"; note "   offline: keeping $(basename "$py_file")"
    else
        fail "python: offline and nothing bundled"
    fi
elif [ -n "$existing_py" ] && [ "$FORCE" != 1 ]; then
    py_file="$existing_py"; note "   current: $(basename "$py_file") (use --force to refresh)"
else
    api='https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest'
    if ! json="$(curl -fsSL --retry 3 "$api" 2>/dev/null)"; then
        if [ -n "$existing_py" ]; then
            py_file="$existing_py"; note "   WARN: release lookup failed; keeping $(basename "$py_file")"
        else
            fail "python: cannot reach $api and nothing bundled"
        fi
    else
        # install_only asset for our minor + triple; pick highest patch by
        # version sort. NOTE: browser_download_url URL-encodes '+' as '%2B',
        # so match either form.
        url="$(printf '%s' "$json" \
            | grep -oE '"browser_download_url": *"[^"]+"' \
            | sed -E 's/.*"(https[^"]+)".*/\1/' \
            | grep -E "cpython-${PY_MINOR}\.[0-9]+(%2B|\+)[0-9]+-${PBS_TRIPLE}-install_only\.tar\.gz$" \
            | sort -V | tail -1 || true)"
        if [ -z "$url" ]; then
            fail "python: no cpython-${PY_MINOR}.*-${PBS_TRIPLE}-install_only asset in latest release"
        else
            # Decode %2B -> + so the local filename (and later glob/version parse,
            # which expect a literal '+') is clean.
            fname="$(basename "$url" | sed 's/%2B/+/g')"
            dest="$py_dir/$fname"
            note "   downloading $fname ..."
            if ! curl -fSL --retry 3 -o "$dest.part" "$url"; then
                rm -f "$dest.part"
                if [ -n "$existing_py" ]; then py_file="$existing_py"; note "   WARN: download failed; keeping $(basename "$py_file")"
                else fail "python: download failed and nothing bundled"; fi
            else
                # PBS ships no per-asset .sha256 sidecar; verify against the
                # release-wide SHA256SUMS ("<hex>  <decoded-filename>").
                want=''
                if curl -fsSL --retry 3 -o "$py_dir/.SHA256SUMS" "${url%/*}/SHA256SUMS" 2>/dev/null; then
                    want="$(awk -v f="$fname" '$2==f{print $1}' "$py_dir/.SHA256SUMS" | head -1)"
                    rm -f "$py_dir/.SHA256SUMS"
                fi
                got="$(sha256sum "$dest.part" | awk '{print $1}')"
                sz="$(stat -c%s "$dest.part" 2>/dev/null || echo 0)"
                if [ -n "$want" ] && [ "$want" != "$got" ]; then
                    rm -f "$dest.part"; fail "python: SHA256 mismatch for $fname (want $want got $got)"
                elif [ -z "$want" ] && [ "$sz" -lt 15000000 ]; then
                    rm -f "$dest.part"; fail "python: download implausibly small ($sz bytes) and no SHA256SUMS to verify"
                else
                    mv -f "$dest.part" "$dest"
                    # Drop older patch tarballs so the dir has exactly one.
                    ls -1 "$py_dir"/cpython-${PY_MINOR}.*-"$PBS_TRIPLE"-install_only.tar.gz 2>/dev/null \
                        | grep -vF "$dest" | while read -r old; do rm -f "$old"; note "   removed old $(basename "$old")"; done
                    py_file="$dest"
                    if [ -n "$want" ]; then note "   done (SHA256 verified)."; else note "   done (no SHA256SUMS; size-checked only)."; fi
                fi
            fi
        fi
    fi
fi
if [ -n "$py_file" ]; then
    py_sha="$(sha256sum "$py_file" | awk '{print $1}')"
    # Version out of the filename: cpython-3.11.9+20240814-...  -> 3.11.9+20240814
    py_ver="$(basename "$py_file" | sed -E "s/^cpython-([0-9.+]+)-${PBS_TRIPLE}-install_only\.tar\.gz$/\1/")"
fi
echo

# =============================================================================
# 2. pip wheels — pinned to requirements.lock.txt, built with the bundled Python
#    so the ABI (cp311 / manylinux x86_64) matches the target exactly.
# =============================================================================
head1 "pip wheels (pinned to requirements.lock.txt)"
if [ ! -r "$lock" ]; then
    fail "wheels: requirements.lock.txt not found at $lock"
else
    lock_hash="$(sha256sum "$lock" | awk '{print $1}')"
    have_wheels="$(ls -1 "$wheels"/*.whl 2>/dev/null | wc -l | tr -d ' ')"
    prev_lock=''
    [ -r "$manifest" ] && prev_lock="$(grep -oE '"lockHash": *"[0-9a-f]{64}"' "$manifest" | head -1 | grep -oE '[0-9a-f]{64}' || true)"

    if [ "$OFFLINE" = 1 ]; then
        if [ "$have_wheels" -ge 1 ]; then note "   offline: keeping $have_wheels wheels"
        else fail "wheels: offline and none present"; fi
    elif [ "$have_wheels" -ge 1 ] && [ "$FORCE" != 1 ] && [ "$prev_lock" = "$lock_hash" ]; then
        note "   current: $have_wheels wheels (lock unchanged)"
    elif [ -z "$py_file" ]; then
        fail "wheels: need the bundled Python to build wheels, but it is missing"
    else
        # Extract the standalone Python to a scratch dir and drive pip with it.
        pytmp="$(mktemp -d "$inst/.pytmp.XXXXXX")"
        if tar -xzf "$py_file" -C "$pytmp"; then
            pybin="$pytmp/python/bin/python3"
            if [ ! -x "$pybin" ]; then pybin="$(ls "$pytmp"/python/bin/python3* 2>/dev/null | head -1 || true)"; fi
            wheels_new="$wheels.new"
            rm -rf "$wheels_new"; mkdir -p "$wheels_new"
            note "   building wheelhouse with $(basename "$py_file") ..."
            # pip wheel converts EVERY requirement (incl. any sdist-only dep such
            # as sd-notify) into an installable .whl, so the offline target never
            # needs a compiler or build backend. Build isolation fetches build
            # deps from PyPI -- fine, we are online here.
            if "$pybin" -m pip wheel --no-cache-dir -r "$lock" -w "$wheels_new" >/dev/null; then
                built="$(ls -1 "$wheels_new"/*.whl 2>/dev/null | wc -l | tr -d ' ')"
                if [ "$built" -ge 1 ]; then
                    rm -rf "$wheels"; mv "$wheels_new" "$wheels"
                    note "   done ($built wheels)."
                else
                    rm -rf "$wheels_new"; fail "wheels: pip wheel produced nothing"
                fi
            else
                rm -rf "$wheels_new"; fail "wheels: pip wheel failed"
            fi
        else
            fail "wheels: could not extract $py_file"
        fi
        rm -rf "$pytmp"
    fi
    wheel_count="$(ls -1 "$wheels"/*.whl 2>/dev/null | wc -l | tr -d ' ')"
fi
echo

# =============================================================================
# 3. .deb closure — pipewire/wireplumber/xrandr/mosquitto-clients + all deps.
#    "voll autark": we grab the full recursive dependency closure so dpkg on the
#    target succeeds with no repo. That makes the bundle large (base libs get
#    pulled in too); dpkg simply skips ones already installed at the same version.
# =============================================================================
head1 ".deb closure ($TARGET_CODENAME/$ARCH): $SYS_PKGS"
if [ "$OFFLINE" = 1 ]; then
    deb_count="$(ls -1 "$debs"/*.deb 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$deb_count" -ge 1 ]; then note "   offline: keeping $deb_count .debs"
    else fail "debs: offline and none present"; fi
elif ! command -v apt-get >/dev/null 2>&1 || ! command -v apt-cache >/dev/null 2>&1; then
    fail "debs: apt-get/apt-cache not found -- run this on a Debian $TARGET_CODENAME box"
else
    have_debs="$(ls -1 "$debs"/*.deb 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$have_debs" -ge 1 ] && [ "$FORCE" != 1 ]; then
        note "   current: $have_debs .debs (use --force to refresh)"
        deb_count="$have_debs"
    else
        note "   resolving dependency closure ..."
        # Top-level (^\w) lines from --recurse are the resolved real package
        # names; indented <virtual> lines are filtered out by the ^\w anchor.
        # Pre-Depends ARE followed (no --no-pre-depends) so hard boot-order deps
        # land in the closure too.
        closure="$(apt-cache depends --recurse --no-recommends --no-suggests \
                    --no-conflicts --no-breaks --no-replaces --no-enhances \
                    $SYS_PKGS 2>/dev/null | grep '^\w' | sort -u)"
        # Drop the elogind side of 'libsystemd0 | libelogind0' alternatives:
        # --recurse pulls BOTH providers, but every target here is systemd, where
        # libsystemd0 is the provider and libelogind0 CONFLICTS with it (dpkg
        # would refuse it). Excluding it keeps the closure clean + conflict-free.
        closure="$(printf '%s\n' "$closure" | grep -vxE 'elogind|libelogind0|libpam-elogind')"
        if [ -z "$closure" ]; then
            fail "debs: apt-cache resolved an empty closure (are the package names available?)"
        else
            debs_new="$debs.new"; rm -rf "$debs_new"; mkdir -p "$debs_new"
            note "   downloading $(printf '%s\n' "$closure" | wc -l | tr -d ' ') packages ..."
            # apt-get download writes into the CWD; run it there. Individual
            # unavailable virtuals are warned about, not fatal -- we gate on the
            # final count + a command-presence check on the target instead.
            ( cd "$debs_new" && apt-get download $closure ) || note "   WARN: some packages failed to download (see above)"
            built="$(ls -1 "$debs_new"/*.deb 2>/dev/null | wc -l | tr -d ' ')"
            if [ "$built" -ge 1 ]; then
                rm -rf "$debs"; mv "$debs_new" "$debs"
                deb_count="$built"; note "   done ($deb_count .debs)."
            else
                rm -rf "$debs_new"; fail "debs: no .debs downloaded"
            fi
        fi
    fi
fi
echo

# =============================================================================
# 4. Manifest + summary.
# =============================================================================
{
    printf '{\n'
    printf '  "generated": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
    printf '  "host": "%s",\n' "$(hostname 2>/dev/null || echo unknown)"
    printf '  "target": { "codename": "%s", "arch": "%s", "pyMinor": "%s" },\n' "$TARGET_CODENAME" "$ARCH" "$PY_MINOR"
    printf '  "items": {\n'
    printf '    "python": { "version": "%s", "file": "%s", "sha256": "%s" },\n' \
        "$py_ver" "$([ -n "$py_file" ] && basename "$py_file" || echo '')" "$py_sha"
    printf '    "wheels": { "lockHash": "%s", "count": %s },\n' "$lock_hash" "${wheel_count:-0}"
    printf '    "debs":   { "codename": "%s", "arch": "%s", "count": %s }\n' "$TARGET_CODENAME" "$ARCH" "${deb_count:-0}"
    printf '  },\n'
    printf '  "errors": [%s]\n' "$([ ${#errors[@]} -gt 0 ] && printf '"%s"' "${errors[0]}"; [ ${#errors[@]} -gt 1 ] && printf ', "%s"' "${errors[@]:1}")"
    printf '}\n'
} > "$manifest"
note "Manifest: $manifest"
echo
echo "Resolved:"
printf '  python   %s  (%s)\n' "$py_ver" "$([ -n "$py_file" ] && basename "$py_file" || echo MISSING)"
printf '  wheels   %s\n' "${wheel_count:-0}"
printf '  debs     %s (%s/%s)\n' "${deb_count:-0}" "$TARGET_CODENAME" "$ARCH"
echo

if [ ${#errors[@]} -gt 0 ]; then
    echo "prepare-offline-linux finished with ${#errors[@]} error(s):"
    for e in "${errors[@]}"; do echo "  - $e"; done
    exit 1
fi
echo "Offline bundle ready. install-linux.sh now runs without internet."
exit 0
