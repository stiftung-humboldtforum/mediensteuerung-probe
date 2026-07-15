#!/usr/bin/env bash
# scripts/prepare-offline-linux.sh — build the OFFLINE install bundle for Linux.
#
# Linux counterpart to scripts/prepare-offline.ps1. Run ONCE on any Debian-based
# host with internet (Debian / Ubuntu / Pop!_OS / Mint ...); afterwards
# scripts/install-linux.sh installs everything with NO internet.
#
# Artefacts land in installers-linux/ (gitignored):
#   - python/  standalone Python (python-build-standalone, SHA256-verified) and
#   - wheels/  pip wheels: both distro-agnostic, built for the BUILD HOST's arch.
#   - debs/    the .deb closure for the TARGET Debian release: pipewire/
#              wireplumber/x11-xserver-utils/mosquitto-clients + full dep closure.
#   - bundle.manifest.linux.json   versions + sha256 + lockHash + target codename.
#
# The .deb closure is built against the target's Debian repos via an ISOLATED
# apt-root (a private apt config with an empty dpkg status), so the build host
# can be ANY Debian-based distro regardless of the target release -- no container,
# no chroot. apt's own solver resolves the closure (correct alternative providers,
# so e.g. libsystemd0 is chosen over libelogind0 automatically). Default target:
# Debian 13 "trixie" (--target-release to change). Cross-architecture is out of
# scope: the bundle is built for the build host's arch (amd64 or arm64).
#
# Flags: --target-release <codename> (default trixie), --mirror <url>,
#        --keyring <path>, --force, --offline. Env: PROBE_TARGET_RELEASE.
#
# Exit: 0 = bundle ready, 1 = at least one component failed.
set -uo pipefail

# --- Target profile -----------------------------------------------------------
PY_MINOR='3.11'                          # bundled Python minor (we ship it; independent of target distro)
SYS_PKGS='pipewire wireplumber x11-xserver-utils mosquitto-clients'
TARGET_RELEASE="${PROBE_TARGET_RELEASE:-trixie}"        # Debian 13 default
MIRROR="${PROBE_MIRROR:-http://deb.debian.org/debian}"  # Debian archive mirror
KEYRING="${PROBE_KEYRING:-/usr/share/keyrings/debian-archive-keyring.gpg}"

# Architecture derived from the build host (cross-arch is out of scope).
deb_arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
case "$deb_arch" in
    amd64) ARCH='x86_64';  PBS_TRIPLE='x86_64-unknown-linux-gnu' ;;
    arm64) ARCH='aarch64'; PBS_TRIPLE='aarch64-unknown-linux-gnu' ;;
    *) echo "Unsupported build-host architecture '$deb_arch' (amd64/arm64 only)." >&2; exit 2 ;;
esac

# --- Args -----------------------------------------------------------------
FORCE=0
OFFLINE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --force)          FORCE=1; shift ;;
        --offline)        OFFLINE=1; shift ;;
        --target-release) TARGET_RELEASE="$2"; shift 2 ;;
        --mirror)         MIRROR="$2"; shift 2 ;;
        --keyring)        KEYRING="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
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
# The .deb step needs a Debian-family apt-get + dpkg (the host distro/release is
# irrelevant -- the isolated apt-root targets the requested Debian release). Only
# enforced online; offline mode just validates an existing bundle.
host_id=''
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    host_id="$(. /etc/os-release 2>/dev/null; echo "${ID:-}")"
fi
if [ "$OFFLINE" != 1 ]; then
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
        fail "build host needs apt-get + dpkg (any Debian-based distro). Found ID='${host_id:-?}'."
        echo; echo "prepare-offline-linux aborted."; exit 1
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
# 3. .deb closure — built against the TARGET Debian release via an isolated
#    apt-root (empty dpkg status => apt's own solver resolves the FULL closure,
#    with correct alternative providers, e.g. libsystemd0 not libelogind0). Runs
#    from any Debian-based build host; no container, no chroot.
# =============================================================================
head1 ".deb closure (Debian $TARGET_RELEASE/$deb_arch, isolated apt-root): $SYS_PKGS"
if [ "$OFFLINE" = 1 ]; then
    deb_count="$(ls -1 "$debs"/*.deb 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$deb_count" -ge 1 ]; then note "   offline: keeping $deb_count .debs"
    else fail "debs: offline and none present"; fi
else
    have_debs="$(ls -1 "$debs"/*.deb 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$have_debs" -ge 1 ] && [ "$FORCE" != 1 ]; then
        note "   current: $have_debs .debs (use --force to refresh)"
        deb_count="$have_debs"
    elif [ ! -r "$KEYRING" ]; then
        fail "debs: Debian archive keyring not found at $KEYRING. On a non-Debian host run 'sudo apt-get install debian-archive-keyring' (or pass --keyring <path>)."
    else
        # apt-root on a NATIVE tmpfs/ext dir (not under installers-linux): apt
        # fchmod()s its list/cache temp files, which fails on filesystems that do
        # not support it (e.g. a checkout on a drvfs/CIFS mount). Only the final
        # .debs are copied back into installers-linux (a plain copy, FS-agnostic).
        aptroot="$(mktemp -d "${TMPDIR:-/tmp}/probe-aptroot.XXXXXX")"; debs_new="$debs.new"
        rm -rf "$debs_new"
        mkdir -p "$aptroot"/etc/apt/apt.conf.d "$aptroot"/etc/apt/preferences.d \
                 "$aptroot"/etc/apt/sources.list.d \
                 "$aptroot"/var/lib/apt/lists/partial \
                 "$aptroot"/var/cache/apt/archives/partial \
                 "$aptroot"/var/lib/dpkg
        : > "$aptroot/var/lib/dpkg/status"   # empty -> apt considers NOTHING installed
        printf 'deb [signed-by=%s] %s %s main\n' "$KEYRING" "$MIRROR" "$TARGET_RELEASE" \
            > "$aptroot/etc/apt/sources.list"
        aopts=(
            -o Dir::Etc::sourcelist="$aptroot/etc/apt/sources.list"
            -o Dir::Etc::sourceparts="$aptroot/etc/apt/sources.list.d"
            -o Dir::Etc::preferencesparts="$aptroot/etc/apt/preferences.d"
            -o Dir::State="$aptroot/var/lib/apt"
            -o Dir::State::status="$aptroot/var/lib/dpkg/status"
            -o Dir::Cache="$aptroot/var/cache/apt"
            -o APT::Architecture="$deb_arch"
            -o APT::Architectures="$deb_arch"
        )
        note "   apt-get update (Debian $TARGET_RELEASE via $MIRROR) ..."
        # Capture apt's output so a failure surfaces the real reason (the benign
        # 'unsandboxed as root' warning is not a failure and is ignored).
        if ! apt-get "${aopts[@]}" update >"$aptroot/update.log" 2>&1; then
            fail "debs: apt-get update against '$MIRROR $TARGET_RELEASE main' failed: $(grep -iE '^(E:|W: Failed|Err:)' "$aptroot/update.log" | head -3 | tr '\n' ' ')"
        else
            note "   resolving + downloading closure (apt solver) ..."
            # --download-only into the isolated cache; the empty dpkg status makes
            # apt plan to install the whole closure and simply download it. The
            # solver picks correct alternative providers, so no elogind hack is
            # needed. No change to the host system. The exit code IS checked: a
            # partial download (transient failure after some .debs landed) must be
            # a hard error, not a silently-incomplete bundle that passes a count
            # check and then fails dpkg on the offline target.
            if ! apt-get "${aopts[@]}" -y --no-install-recommends --download-only install $SYS_PKGS >"$aptroot/install.log" 2>&1; then
                rm -rf "$debs_new"
                fail "debs: apt download-only install failed (closure incomplete): $(grep -iE '^(E:|W: Failed|Err:)' "$aptroot/install.log" | head -3 | tr '\n' ' ')"
            else
                mkdir -p "$debs_new"
                find "$aptroot/var/cache/apt/archives" -maxdepth 1 -name '*.deb' -exec cp -t "$debs_new" {} + 2>/dev/null || true
                built="$(ls -1 "$debs_new"/*.deb 2>/dev/null | wc -l | tr -d ' ')"
                if [ "$built" -ge 1 ]; then
                    rm -rf "$debs"; mv "$debs_new" "$debs"
                    deb_count="$built"; note "   done ($deb_count .debs)."
                else
                    rm -rf "$debs_new"; fail "debs: apt downloaded no .debs (are $SYS_PKGS in $TARGET_RELEASE main?)."
                fi
            fi
        fi
        rm -rf "$aptroot"
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
    printf '  "target": { "codename": "%s", "arch": "%s", "pyMinor": "%s" },\n' "$TARGET_RELEASE" "$deb_arch" "$PY_MINOR"
    printf '  "items": {\n'
    printf '    "python": { "version": "%s", "file": "%s", "sha256": "%s" },\n' \
        "$py_ver" "$([ -n "$py_file" ] && basename "$py_file" || echo '')" "$py_sha"
    printf '    "wheels": { "lockHash": "%s", "count": %s },\n' "$lock_hash" "${wheel_count:-0}"
    printf '    "debs":   { "codename": "%s", "arch": "%s", "count": %s }\n' "$TARGET_RELEASE" "$deb_arch" "${deb_count:-0}"
    printf '  },\n'
    printf '  "errors": [%s]\n' "$([ ${#errors[@]} -gt 0 ] && printf '"%s"' "${errors[0]}"; [ ${#errors[@]} -gt 1 ] && printf ', "%s"' "${errors[@]:1}")"
    printf '}\n'
} > "$manifest"
note "Manifest: $manifest"
echo
echo "Resolved:"
printf '  python   %s  (%s)\n' "$py_ver" "$([ -n "$py_file" ] && basename "$py_file" || echo MISSING)"
printf '  wheels   %s\n' "${wheel_count:-0}"
printf '  debs     %s (Debian %s/%s)\n' "${deb_count:-0}" "$TARGET_RELEASE" "$deb_arch"
echo

if [ ${#errors[@]} -gt 0 ]; then
    echo "prepare-offline-linux finished with ${#errors[@]} error(s):"
    for e in "${errors[@]}"; do echo "  - $e"; done
    exit 1
fi
echo "Offline bundle ready. install-linux.sh now runs without internet."
exit 0
