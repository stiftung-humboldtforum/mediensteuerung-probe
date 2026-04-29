"""methods package — sensor and command dispatch.

Platform-specific implementations live in _linux.py / _win32.py.
_stub.py provides graceful fallbacks for unsupported platforms (e.g.
macOS dev machines running tests).

Common implementations (ping, boot_time, mpv_file_pos_sec, easire)
that don't differ across platforms live in this module.

SENSORS — names allowed in PROBE_METHODS (periodic polling).
COMMANDS — names allowed in PROBE_CAPABILITIES (manager → probe RPC).
"""
import platform
import subprocess

import psutil

from misc import make_response

# Platform dispatch — captured once at import time.
_system = platform.system()
if _system == 'Linux':
    from . import _linux as _impl
elif _system == 'Windows':
    from . import _win32 as _impl
else:
    from . import _stub as _impl


# --- Common helpers --------------------------------------------------------

def call_method(method, *args, **kwargs):
    try:
        result = method(*args, **kwargs)
        response = make_response(data=dict(status='complete', result=result))
    except Exception as e:
        response = make_response(error=dict(message=type(e).__name__, errors=e.args))
    return response


# --- Common sensors --------------------------------------------------------

def ping():
    return None


def boot_time():
    return psutil.boot_time()


def mpv_file_pos_sec():
    p = subprocess.run(
        ['mpv_control', 'file_pos_sec'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3,
    )
    if p.returncode == 0:
        return int(p.stdout.strip().decode())
    return None


def easire():
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if 'easire-player' in (proc.info['name'] or ''):
                return True
            if any('easire-player' in arg for arg in (proc.info['cmdline'] or [])):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


# --- Platform-specific re-exports -----------------------------------------

shutdown = _impl.shutdown
reboot = _impl.reboot
is_muted = _impl.is_muted
mute = _impl.mute
unmute = _impl.unmute
temperatures = _impl.temperatures
fans = _impl.fans
uptime = _impl.uptime
display = _impl.display


# --- Whitelists -----------------------------------------------------------

SENSORS = {
    'ping': ping,
    'temperatures': temperatures,
    'fans': fans,
    'uptime': uptime,
    'boot_time': boot_time,
    'mpv_file_pos_sec': mpv_file_pos_sec,
    'display': display,
    'easire': easire,
    'is_muted': is_muted,
}

COMMANDS = {
    'shutdown': shutdown,
    'reboot': reboot,
    'mute': mute,
    'unmute': unmute,
    'ping': ping,
}
