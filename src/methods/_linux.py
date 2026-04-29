"""Linux-specific sensor and command implementations."""
import os
import subprocess
from typing import Optional

import psutil


def shutdown() -> None:
    """Trigger immediate poweroff via 'sudo shutdown now'. The probe
    user must have a NOPASSWD sudoers entry for /sbin/shutdown."""
    cmd = ['sudo', 'shutdown', 'now']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'shutdown failed (rc={result.returncode}): {result.stderr.strip()}')


def reboot() -> None:
    """Trigger immediate reboot via 'sudo reboot now'. Same NOPASSWD
    requirement as shutdown()."""
    cmd = ['sudo', 'reboot', 'now']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'reboot failed (rc={result.returncode}): {result.stderr.strip()}')


def is_muted() -> bool:
    """Whether the default PipeWire sink is muted."""
    return 'MUTED' in subprocess.check_output(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'], timeout=3).decode()


def mute() -> None:
    """Mute the default PipeWire sink."""
    subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1'], timeout=3)


def unmute() -> None:
    """Unmute the default PipeWire sink."""
    subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'], timeout=3)


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
        return float(f.read().split(' ')[0])


def display() -> Optional[str]:
    """Active display mode as 'WIDTHxHEIGHT, RATE Hz', via xrandr.
    Returns None if xrandr fails or no active mode line is found."""
    p = subprocess.run(
        ['xrandr', '--current'],
        env={**os.environ, 'DISPLAY': ':0'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )
    if p.returncode != 0:
        return None
    mode = [line for line in p.stdout.decode().splitlines() if '*' in line]
    if not mode:
        return None
    parts = ' '.join(mode[0].split()).split(' ')
    resolution = parts[0]
    rate = ''.join(c for c in parts[1] if c.isdigit() or c == '.')
    return f'{resolution}, {rate} Hz'
