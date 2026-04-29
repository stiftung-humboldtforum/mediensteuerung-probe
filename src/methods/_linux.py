"""Linux-specific sensor and command implementations."""
import os
import subprocess

import psutil


def shutdown():
    cmd = ['sudo', 'shutdown', 'now']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'shutdown failed (rc={result.returncode}): {result.stderr.strip()}')


def reboot():
    cmd = ['sudo', 'reboot', 'now']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'reboot failed (rc={result.returncode}): {result.stderr.strip()}')


def is_muted():
    return 'MUTED' in subprocess.check_output(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'], timeout=3).decode()


def mute():
    subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1'], timeout=3)


def unmute():
    subprocess.run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'], timeout=3)


def temperatures():
    sensors = psutil.sensors_temperatures()
    return {key: [temp._asdict() for temp in sensor] for key, sensor in sensors.items()}


def fans():
    sensors = psutil.sensors_fans()
    return {key: [fan._asdict() for fan in sensor] for key, sensor in sensors.items()}


def uptime():
    with open('/proc/uptime', 'r') as f:
        return float(f.read().split(' ')[0])


def display():
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
