"""methods package — sensor and command dispatch.

Platform-specific implementations live in _linux.py / _win32.py.
Other platforms are not supported — import fails loudly so a
mis-deployed probe is caught at startup, not silently in production.

Site-specific sensors (easire, mpv_file_pos_sec) live in _plugins.py
and register themselves at import time via the `register_sensor`
decorator. Core does not know about them.

SENSORS — names allowed in PROBE_METHODS (periodic polling).
COMMANDS — names allowed in PROBE_CAPABILITIES (manager → probe RPC).
"""
import platform
from typing import Callable

import psutil

from misc import make_response

# Platform dispatch — captured once at import time.
_system = platform.system()
if _system == 'Linux':
    from . import _linux as _impl
elif _system == 'Windows':
    from . import _win32 as _impl
else:
    raise RuntimeError(
        f'Unsupported platform: {_system!r}. Probe runs on Linux + Windows only.'
    )


# --- Whitelists + registration --------------------------------------------

SENSORS: dict[str, Callable] = {}
COMMANDS: dict[str, Callable] = {}


def register_sensor(name: str):
    """Decorator: add a function to SENSORS under the given name."""
    def deco(fn: Callable) -> Callable:
        SENSORS[name] = fn
        return fn
    return deco


def register_command(name: str):
    """Decorator: add a function to COMMANDS under the given name."""
    def deco(fn: Callable) -> Callable:
        COMMANDS[name] = fn
        return fn
    return deco


# --- Common helpers --------------------------------------------------------

_SAFE_ARG_MAX_LEN = 1024  # cap per-arg repr to keep MQTT payloads bounded


def _safe_args(args: tuple) -> list:
    """Coerce e.args to JSON-serializable strings — exceptions can carry
    file objects, ctypes pointers, etc. that json.dumps would reject and
    crash the publish cycle.

    Each entry is capped at _SAFE_ARG_MAX_LEN to prevent megabyte-sized
    error envelopes (e.g. an exception carrying a long __cause__ chain).

    A `__repr__` that itself raises (rare but possible on partially-
    initialised C-extension objects) is caught and replaced with a
    placeholder so the publish cycle never crashes inside error-reporting.
    """
    def _truncate(s: str) -> str:
        if len(s) > _SAFE_ARG_MAX_LEN:
            return s[:_SAFE_ARG_MAX_LEN - 3] + '...'
        return s

    safe = []
    for a in args:
        if isinstance(a, (int, float, bool)) or a is None:
            safe.append(a)
        elif isinstance(a, str):
            safe.append(_truncate(a))
        else:
            try:
                safe.append(_truncate(repr(a)))
            except Exception:
                safe.append(f'<unrepresentable {type(a).__name__}>')
    return safe


def call_method(method: Callable, *args, **kwargs) -> str:
    """Wrap a function call in the standard probe-response envelope.
    On success returns {"data": {"status": "complete", "result": <value>}};
    on exception returns {"error": {"message": <ExceptionName>, "errors": <args>}}.

    Catches Exception (not BaseException) so signal-driven exits
    (KeyboardInterrupt, SystemExit) still propagate up to the probe
    lifecycle layer instead of being swallowed into an MQTT payload.
    """
    try:
        result = method(*args, **kwargs)
        response = make_response(data=dict(status='complete', result=result))
    except Exception as e:
        response = make_response(error=dict(message=type(e).__name__, errors=_safe_args(e.args)))
    return response


# --- Common sensors --------------------------------------------------------

@register_sensor('ping')
@register_command('ping')
def ping() -> None:
    """No-op heartbeat marker. Acts as both a periodic sensor and a
    manager-invokable command."""
    return None


@register_command('wake')
def wake() -> str:
    """No-op acknowledgement of a 'wake' command. Wake-on-LAN is
    triggered externally by the manager (the target machine is asleep
    and cannot receive MQTT). Probe declares the capability so the
    manager UI shows the button; if the manager *does* publish to
    `manager/<fqdn>/wake` once the host is up, this returns a stable
    'awake' marker instead of the confusing 'Unknown method' error."""
    return 'awake'


@register_sensor('boot_time')
def boot_time() -> float:
    """Unix-epoch seconds at which the system booted."""
    return psutil.boot_time()


# --- Platform-specific re-exports + registration --------------------------

shutdown = _impl.shutdown
reboot = _impl.reboot
is_muted = _impl.is_muted
mute = _impl.mute
unmute = _impl.unmute
temperatures = _impl.temperatures
fans = _impl.fans
uptime = _impl.uptime
display = _impl.display

for _name, _fn in (
    ('temperatures', temperatures),
    ('fans', fans),
    ('uptime', uptime),
    ('display', display),
    ('is_muted', is_muted),
):
    SENSORS[_name] = _fn

for _name, _fn in (
    ('shutdown', shutdown),
    ('reboot', reboot),
    ('mute', mute),
    ('unmute', unmute),
):
    COMMANDS[_name] = _fn


# --- Site-specific plugins -------------------------------------------------
# Imported last so register_sensor/register_command are already defined.
# Operators replacing this file (or wiping its contents) get a probe
# without easire/mpv_file_pos_sec — generic core stays usable.
from . import _plugins  # noqa: E402, F401
# Re-export plugin functions at the package level so existing call sites
# (`from methods import easire`) keep working.
from ._plugins import easire, mpv_file_pos_sec  # noqa: E402, F401
