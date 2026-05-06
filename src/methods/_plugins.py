"""Site-specific plugin sensors.

Sensors here depend on tooling deployed alongside the kiosk
(easire-player, mpv with mpv_control) and are NOT part of the
generic probe contract. Replace this file (or wipe its contents)
when deploying the probe to a site that uses different player
tooling — the core probe runs without these sensors.

Sensors register themselves via the `register_sensor` decorator from
`methods`, so adding a new one only requires writing the function
plus the decorator (no separate SENSORS-dict edit).
"""
import subprocess
from typing import Optional

import psutil

from . import register_sensor


@register_sensor('easire')
def easire() -> Optional[bool]:
    """Whether an 'easire-player' process is running (matched on
    process name OR any cmdline argument). Returns True or None
    (not False — None signals 'not present', preserving the original
    avorus-probe semantics for the manager-side)."""
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if 'easire-player' in (proc.info['name'] or ''):
                return True
            if any('easire-player' in arg for arg in (proc.info['cmdline'] or [])):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


@register_sensor('mpv_file_pos_sec')
def mpv_file_pos_sec() -> Optional[int]:
    """Current playback position of the kiosk mpv player in seconds.
    Requires the external 'mpv_control' helper script on PATH (see
    README). Returns None if mpv_control fails (mpv not running) or
    if its output is not a parseable number (e.g. 'nan'/'inf' from a
    paused/seeking mpv)."""
    p = subprocess.run(
        ['mpv_control', 'file_pos_sec'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3,
    )
    if p.returncode != 0:
        return None
    raw = p.stdout.strip().decode()
    if not raw:
        return None
    try:
        # Accept either '12' or '12.345' from third-party mpv_control
        # impls; nan/inf yield ValueError via int() and short-circuit
        # to None instead of crashing the sensor cycle.
        value = float(raw)
    except ValueError:
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return int(value)
