"""Linux-specific sensor and command implementations."""
import os
import subprocess
from typing import Optional

import psutil


# Absolute paths so the probe never relies on a writable PATH. Match
# the sudoers NOPASSWD entry in systemd/humboldt-probe.service.
_SHUTDOWN_BIN = '/sbin/shutdown'
_REBOOT_BIN = '/sbin/reboot'


def shutdown() -> None:
    """Trigger immediate poweroff via 'sudo /sbin/shutdown now'. The probe
    user must have a NOPASSWD sudoers entry for /sbin/shutdown."""
    cmd = ['sudo', _SHUTDOWN_BIN, 'now']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'shutdown failed (rc={result.returncode}): {result.stderr.strip()}')


def reboot() -> None:
    """Trigger immediate reboot via 'sudo /sbin/reboot now'. Same NOPASSWD
    requirement as shutdown()."""
    cmd = ['sudo', _REBOOT_BIN, 'now']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'reboot failed (rc={result.returncode}): {result.stderr.strip()}')


def is_muted() -> bool:
    """Whether the default PipeWire sink is muted. Captures stderr so a
    CalledProcessError surfaces wpctl's actual error message ('No
    devices found' / 'Failed to connect') instead of just a return code."""
    return 'MUTED' in subprocess.check_output(
        ['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'],
        stderr=subprocess.PIPE,
        timeout=3,
    ).decode()


def mute() -> None:
    """Mute the default PipeWire sink."""
    result = subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1'], capture_output=True, text=True, timeout=3)
    if result.returncode != 0:
        raise RuntimeError(f'mute failed (rc={result.returncode}): {result.stderr.strip()}')


def unmute() -> None:
    """Unmute the default PipeWire sink."""
    result = subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'], capture_output=True, text=True, timeout=3)
    if result.returncode != 0:
        raise RuntimeError(f'unmute failed (rc={result.returncode}): {result.stderr.strip()}')


def temperatures() -> dict[str, list[dict]]:
    """All hardware temperatures via psutil. Schema:
    {hw_name: [{label, current, high, critical}, ...]}."""
    sensors = psutil.sensors_temperatures()
    return {key: [temp._asdict() for temp in sensor] for key, sensor in sensors.items()}


def fans() -> dict[str, list[dict]]:
    """All fan speeds via psutil. Schema:
    {hw_name: [{label, current}, ...]}."""
    sensors = psutil.sensors_fans()
    return {key: [fan._asdict() for fan in sensor] for key, sensor in sensors.items()}


def uptime() -> float:
    """Seconds since boot, read from /proc/uptime (cheaper than psutil)."""
    with open('/proc/uptime', 'r') as f:
        content = f.read()
    parts = content.split()
    if not parts:
        raise RuntimeError('/proc/uptime returned no fields')
    return float(parts[0])


# Whitelist of env-vars passed to xrandr — avoids LD_PRELOAD and
# similar pollution from the parent process.
_XRANDR_ENV_KEYS = ('PATH', 'HOME', 'XAUTHORITY', 'XDG_RUNTIME_DIR')


def display() -> Optional[str]:
    """Active display mode as 'WIDTHxHEIGHT, RATE Hz', via xrandr.
    Returns None if xrandr fails or no active mode line is found.

    Multi-monitor: xrandr emits one '*' mode line per connected output.
    We prefer the output marked 'primary' (matches the kiosk-relevant
    screen); if no primary marker is present, fall back to the first
    '*' line.
    """
    env = {key: os.environ[key] for key in _XRANDR_ENV_KEYS if key in os.environ}
    env['DISPLAY'] = ':0'
    p = subprocess.run(
        ['xrandr', '--current'],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )
    if p.returncode != 0:
        return None
    lines = p.stdout.decode().splitlines()
    active_line = None
    in_primary = False
    for line in lines:
        # Output header line, e.g. 'HDMI-0 connected primary 1920x1080+0+0 ...'
        if ' connected' in line and not line.startswith(' '):
            in_primary = ' primary' in line
        elif in_primary and '*' in line:
            active_line = line
            break
    if active_line is None:
        fallback = [line for line in lines if '*' in line]
        if not fallback:
            return None
        active_line = fallback[0]
    parts = active_line.split()
    if len(parts) < 2:
        # Malformed mode line (xrandr output corrupted or unexpected
        # format) — better to surface 'no display' than crash the probe
        # cycle with IndexError.
        return None
    resolution = parts[0]
    rate = ''.join(c for c in parts[1] if c.isdigit() or c == '.')
    if not rate:
        return None
    return f'{resolution}, {rate} Hz'
