"""Windows-specific sensor and command implementations."""
import ctypes
import ctypes.wintypes
import hashlib
import os
import subprocess
import time
from typing import Optional

import psutil


# --- Audio (pycaw) ---------------------------------------------------------

def _get_windows_volume():
    """Lazy IAudioEndpointVolume reference for the default sink."""
    from pycaw.pycaw import AudioUtilities
    speakers = AudioUtilities.GetSpeakers()
    return speakers.EndpointVolume


def is_muted() -> bool:
    """Whether the default audio endpoint is muted (via pycaw COM)."""
    return bool(_get_windows_volume().GetMute())


def mute() -> None:
    """Mute the default audio endpoint."""
    _get_windows_volume().SetMute(1, None)


def unmute() -> None:
    """Unmute the default audio endpoint."""
    _get_windows_volume().SetMute(0, None)


# --- Power ----------------------------------------------------------------

def shutdown() -> None:
    """Trigger poweroff via 'shutdown /s /t 5'. The 5s delay lets paho flush
    the command-ack to the broker before the OS tears the process down (see
    reboot())."""
    cmd = ['shutdown', '/s', '/t', '5']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'shutdown failed (rc={result.returncode}): {result.stderr.strip()}')


def reboot() -> None:
    """Trigger reboot via 'shutdown /r /t 5'. The 5s delay lets paho flush the
    command-ack to the broker before the OS tears the process down -- a /t 0
    reboot raced the ack, so the manager never saw completion and re-issued
    the reboot on every reconnect (reboot loop)."""
    cmd = ['shutdown', '/r', '/t', '5']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'reboot failed (rc={result.returncode}): {result.stderr.strip()}')


# --- LibreHardwareMonitor (CPU/GPU temps + fans) --------------------------

_lhm_computer = None
# Cache the verified DLL set so we don't re-hash + re-spam the log
# every 5s when LHM-sensors poll. Once verified successfully for a
# given lib_path, the result is sticky for the process lifetime.
_dll_hashes_verified: set[str] = set()


def _verify_dll_hashes(lib_path: str) -> None:
    """Verify SHA256 hashes of bundled .NET DLLs against
    lib/win32/SHA256SUMS. Raises RuntimeError on mismatch — guards
    against tampered DLLs being silently loaded into the probe process.

    Idempotent: result cached per lib_path to keep the per-cycle
    sensor-poll path cheap.
    """
    if lib_path in _dll_hashes_verified:
        return
    manifest = os.path.join(lib_path, 'SHA256SUMS')
    if not os.path.isfile(manifest):
        raise RuntimeError(f'Hash manifest not found: {manifest}')
    with open(manifest, 'r') as f:
        expected = {}
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            digest, _, name = line.partition(' ')
            name = name.lstrip('*').strip()
            digest = digest.strip()
            # Reject path-traversal / absolute paths in the filename
            # column — only basenames inside lib_path are allowed.
            if not name or name != os.path.basename(name) or os.path.isabs(name):
                raise RuntimeError(f'Invalid manifest entry: {name!r}')
            if len(digest) != 64 or not all(c in '0123456789abcdef' for c in digest.lower()):
                raise RuntimeError(f'Invalid SHA256 digest for {name!r}: {digest!r}')
            expected[name] = digest.lower()
    for name, want in expected.items():
        path = os.path.join(lib_path, name)
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        got = h.hexdigest()
        if got != want:
            raise RuntimeError(
                f'DLL hash mismatch for {name}: expected {want}, got {got}'
            )
    _dll_hashes_verified.add(lib_path)


def _get_lhm_computer():
    """Lazy-init the LibreHardwareMonitor Computer singleton. Loads
    the .NET assembly via pythonnet, registers an atexit cleanup for
    Computer.Close()."""
    global _lhm_computer
    if _lhm_computer is None:
        import clr
        # Resolve the bundled DLL by absolute path. Avoids polluting
        # sys.path (where a writable directory could shadow the assembly
        # via a malicious DLL of the same name).
        lib_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'lib', 'win32')
        )
        _verify_dll_hashes(lib_path)
        dll_path = os.path.join(lib_path, 'LibreHardwareMonitorLib.dll')
        clr.AddReference(dll_path)
        from LibreHardwareMonitor.Hardware import Computer
        c = Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMotherboardEnabled = True
        c.IsControllerEnabled = True
        c.Open()
        import atexit
        _lhm_computer = c
        atexit.register(c.Close)
    return _lhm_computer


def _iter_lhm_sensors(target_name):
    """Yield (hardware, sensor_name, value) for every sensor of the given
    type ('Temperature' or 'Fan') across all hardware + sub-hardware."""
    c = _get_lhm_computer()
    from LibreHardwareMonitor.Hardware import SensorType
    target = getattr(SensorType, target_name)
    for hw in c.Hardware:
        hw.Update()
        sensors = list(hw.Sensors)
        for sub in hw.SubHardware:
            sub.Update()
            sensors.extend(sub.Sensors)
        for s in sensors:
            if s.SensorType == target:
                yield hw, str(s.Name), float(s.Value)


def temperatures() -> dict[str, list[dict]]:
    """CPU temperatures as the 'coretemp' payload the avorus-ui sensors
    component expects: a single 'coretemp' key whose first entry is the CPU
    package, the rest per-core, each with a numeric high/critical (= CPU
    TjMax) so the UI's hue maths (1 - current/high) works.

    avorus-ui renders ONLY temperatures['coretemp'] and treats [0] as the
    package, so the LHM per-hardware-model key ('Intel Core i9-...') showed
    nothing. We mirror the Linux psutil 'coretemp' shape instead.
    LibreHardwareMonitor reports per-core temps plus a "... Distance to TjMax"
    margin sensor; TjMax = core_temp + margin gives the real critical/high.
    """
    import re
    core_temp: dict[int, float] = {}
    core_dist: dict[int, float] = {}
    package: Optional[float] = None
    for hw, name, value in _iter_lhm_sensors('Temperature'):
        if str(hw.HardwareType) != 'Cpu':
            continue
        if name == 'CPU Package':
            package = round(value, 1)
            continue
        m = re.match(r'CPU Core #(\d+)( Distance to TjMax)?$', name)
        if not m:
            continue  # ignore 'Core Max' / 'Core Average' aggregates
        idx = int(m.group(1))
        if m.group(2):
            core_dist[idx] = value
        else:
            core_temp[idx] = round(value, 1)

    if package is None and not core_temp:
        return {}

    tjmax = None
    for idx, cur in core_temp.items():
        if idx in core_dist:
            tjmax = round(cur + core_dist[idx], 1)
            break
    if tjmax is None:
        tjmax = 100.0  # Intel desktop TjMax fallback

    coretemp: list[dict] = []
    if package is not None:
        coretemp.append({'label': 'Package id 0', 'current': package, 'high': tjmax, 'critical': tjmax})
    for idx in sorted(core_temp):
        coretemp.append({'label': f'Core {idx - 1}', 'current': core_temp[idx], 'high': tjmax, 'critical': tjmax})
    return {'coretemp': coretemp}


def fans() -> dict[str, list[dict]]:
    """System/chassis fan speeds under the 'dell_smm' key the avorus-ui Fans
    component renders (it shows only the Linux hwmon driver keys 'nct6795' /
    'dell_smm'; the kiosk fleet is Dell).

    Only NON-GPU fans are reported. The GPU fan is not a system fan, and
    publishing it under 'dell_smm' would surface a value the manager cannot
    tell apart from the real system fan -- a wrong reading is worse than none.
    On these Dell workstations LHM exposes ONLY the GPU fan (the CPU/chassis
    fans are governed by the Dell EC and expose no RPM to Windows), so this
    returns {} and the UI shows no fan. When a board does expose system/CPU
    fans to LHM they appear here.
    """
    out = []
    for hw, name, value in _iter_lhm_sensors('Fan'):
        if 'Gpu' in str(hw.HardwareType):
            continue  # GPU fan != system fan; would be a wrong dell_smm value
        out.append({'label': name, 'current': round(value, 1)})
    return {'dell_smm': out} if out else {}


# --- Misc -----------------------------------------------------------------

def uptime() -> float:
    """Seconds since boot via psutil."""
    return time.time() - psutil.boot_time()


def display() -> Optional[str]:
    """Active display mode as 'WIDTHxHEIGHT, RATE Hz'.

    Read via WMI Win32_VideoController, NOT EnumDisplaySettings: the probe
    runs as a service in session 0, where EnumDisplaySettings returns the
    headless session-0 default (1024x768) instead of the real console
    display. Win32_VideoController exposes the driver's current mode and is
    session-independent. Pick the controller with the largest current
    resolution (an inactive/secondary GPU reports a null resolution).
    """
    ps = (
        "Get-CimInstance Win32_VideoController | "
        "Where-Object { $_.CurrentHorizontalResolution } | "
        "Sort-Object CurrentHorizontalResolution -Descending | "
        "Select-Object -First 1 | ForEach-Object { "
        "\"$($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution), "
        "$($_.CurrentRefreshRate) Hz\" }"
    )
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    line = out.stdout.strip()
    return line or None
