#!/usr/bin/env bash
# scripts/hardware-test-linux.sh — Hardware-Probe auf einem Linux-Kiosk-PC
#
# Läuft die plattform-spezifischen Linux-Funktionen (wpctl, xrandr,
# psutil-Sensoren, /proc/uptime, easire-Detection) direkt aus, ohne
# MQTT — also reine Hardware-/OS-Verifikation.
#
# Auszuführen NACH Deploy auf der Ziel-Hardware. Idealer Use-Case:
#
#   ssh kiosk-01 "cd /opt/humboldt-probe && bash scripts/hardware-test-linux.sh"
#
# Audio-Tests sind invasiv (toggle mute), ergänzbar via env-var
# SKIP_AUDIO=1 wenn keine Show-Unterbrechung gewünscht.
#
# Exit codes:
#   0 — alle Tests OK (oder skipped per env)
#   1 — mindestens ein Test failed
#   2 — Voraussetzungen fehlen

set -u
set -o pipefail

PASS=0
FAIL=0

step() {
    local name="$1"; shift
    local cmd="$1"; shift
    local expect_pattern="${1:-.}"

    local out
    if out=$(eval "$cmd" 2>&1); then
        if echo "$out" | grep -qE "$expect_pattern"; then
            printf '[OK  ] %s — %s\n' "$name" "$(echo "$out" | head -c 100)"
            PASS=$((PASS + 1))
            return 0
        else
            printf '[FAIL] %s — output didn'\''t match /%s/: %s\n' "$name" "$expect_pattern" "$(echo "$out" | head -c 200)"
            FAIL=$((FAIL + 1))
            return 1
        fi
    else
        printf '[FAIL] %s — command failed: %s\n' "$name" "$(echo "$out" | head -c 200)"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || { echo "ERROR: cannot cd to $REPO_ROOT" >&2; exit 2; }

# Python-Binary auswählen (venv falls vorhanden, sonst system).
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PY="$REPO_ROOT/.venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi
if [ -z "${PY:-}" ]; then
    echo "ERROR: no Python interpreter found" >&2
    exit 2
fi

if [ "$(uname)" != "Linux" ]; then
    echo "ERROR: this script is for Linux only (host is $(uname))" >&2
    exit 2
fi

echo "Linux hardware smoke test on $(hostname -f) — Python: $PY"
echo

export PYTHONPATH="$REPO_ROOT/src"

# --- Display (xrandr) ----------------------------------------------------
step 'display: xrandr returns mode' \
     "DISPLAY=:0 $PY -c 'from methods._linux import display; print(display())'" \
     '[0-9]+x[0-9]+'

# --- Uptime (/proc/uptime) -----------------------------------------------
step 'uptime: /proc/uptime parsed' \
     "$PY -c 'from methods._linux import uptime; print(uptime())'" \
     '^[0-9]+\.[0-9]+'

# --- Temperatures (psutil) -----------------------------------------------
step 'temperatures: psutil.sensors_temperatures non-empty' \
     "$PY -c 'from methods._linux import temperatures as t; r = t(); print(r); assert r, \"empty\"'" \
     '.+'

# --- Fans (psutil) — kann leer sein, deshalb nur kein crash ---------------
step 'fans: psutil.sensors_fans returns dict' \
     "$PY -c 'from methods._linux import fans; r = fans(); print(type(r).__name__, r)'" \
     '^dict'

# --- Audio (wpctl) — invasiv -------------------------------------------
if [ "${SKIP_AUDIO:-0}" = "1" ]; then
    echo "[SKIP] audio toggle (SKIP_AUDIO=1)"
else
    step 'audio: wpctl get-volume parses' \
         "$PY -c 'from methods._linux import is_muted; print(is_muted())'" \
         '^(True|False)$'

    # Toggle-Test: state lesen, mute, lesen, unmute, lesen
    step 'audio: mute toggle works' \
         "$PY -c '
from methods._linux import is_muted, mute, unmute
before = is_muted()
mute(); after_mute = is_muted()
unmute(); after_unmute = is_muted()
assert after_mute is True, f\"after mute: {after_mute}\"
assert after_unmute is False, f\"after unmute: {after_unmute}\"
print(\"toggle ok, restored=\", before)
'" \
         'toggle ok'
fi

# --- easire-Detection ----------------------------------------------------
step 'easire: psutil.process_iter runs' \
     "$PY -c 'from methods import easire; r = easire(); print(\"running\" if r else \"not running\")'" \
     '(running|not running)'

# --- mpv_control (optional) ----------------------------------------------
if command -v mpv_control >/dev/null 2>&1; then
    step 'mpv_control: returns int seconds' \
         "$PY -c 'from methods import mpv_file_pos_sec; print(mpv_file_pos_sec())'" \
         '^([0-9]+|None)'
else
    echo "[SKIP] mpv_control not on PATH"
fi

# --- sudo permissions for shutdown/reboot --------------------------------
# Nicht ausführen, nur Berechtigung prüfen.
step 'sudo: NOPASSWD shutdown configured' \
     'sudo -n -l /sbin/shutdown 2>&1 || sudo -n -l /usr/sbin/shutdown 2>&1' \
     '(NOPASSWD|shutdown)'

echo
echo '──────────────────────────────────────────────'
echo "Result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
