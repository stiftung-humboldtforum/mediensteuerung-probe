"""Stubs for unsupported platforms (e.g. macOS dev machines).

All platform-specific operations raise NotImplementedError. Common
methods (ping, boot_time, mpv_file_pos_sec, easire) live in
methods/__init__.py and work on any platform.
"""
import platform


def _unsupported(name):
    raise NotImplementedError(f'{name} not supported on {platform.system()}')


def shutdown():
    _unsupported('shutdown')


def reboot():
    _unsupported('reboot')


def is_muted():
    _unsupported('is_muted')


def mute():
    _unsupported('mute')


def unmute():
    _unsupported('unmute')


def temperatures():
    return {}


def fans():
    return {}


def uptime():
    import psutil
    import time
    return time.time() - psutil.boot_time()


def display():
    return None
