"""Stubs for unsupported platforms (e.g. macOS dev machines).

Power-/Audio-Commands raise NotImplementedError so a probe accidentally
running here can't pretend to mute/shutdown the host. Read-only sensors
return empty/None so test discovery and import-time checks don't crash.
"""
import platform
import time
from typing import NoReturn, Optional

import psutil


def _unsupported(name: str) -> NoReturn:
    raise NotImplementedError(f'{name} not supported on {platform.system()}')


def shutdown() -> NoReturn:
    _unsupported('shutdown')


def reboot() -> NoReturn:
    _unsupported('reboot')


def is_muted() -> NoReturn:
    _unsupported('is_muted')


def mute() -> NoReturn:
    _unsupported('mute')


def unmute() -> NoReturn:
    _unsupported('unmute')


def temperatures() -> dict[str, list[dict]]:
    return {}


def fans() -> dict[str, list[dict]]:
    return {}


def uptime() -> float:
    return time.time() - psutil.boot_time()


def display() -> Optional[str]:
    return None
