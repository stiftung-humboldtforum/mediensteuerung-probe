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
    """Trigger immediate poweroff via 'shutdown /s /t 0' (Windows
    built-in)."""
    cmd = ['shutdown', '/s', '/t', '0']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'shutdown failed (rc={result.returncode}): {result.stderr.strip()}')


def reboot() -> None:
    """Trigger immediate reboot via 'shutdown /r /t 0'."""
    cmd = ['shutdown', '/r', '/t', '0']
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


def _get_lhm_sensors(sensor_type):
    """Aggregate Temperature or Fan sensors from the LHM hardware
    list. Filters to CPU/GPU hardware so motherboard and SSD sensors
    don't sneak in (the manager doesn't need them). Temperature schema
    matches Linux psutil: {hw_name: [{label, current, high, critical}, ...]};
    fans: {hw_name: [{label, current}, ...]}."""
    c = _get_lhm_computer()
    from LibreHardwareMonitor.Hardware import SensorType
    target = {'Temperature': SensorType.Temperature, 'Fan': SensorType.Fan}[sensor_type]
    results: dict[str, list[dict]] = {}
    for hw in c.Hardware:
        hw.Update()
        hw_type = str(hw.HardwareType)
        is_cpu_gpu = hw_type == 'Cpu' or 'Gpu' in hw_type
        all_sensors = []
        for sub in hw.SubHardware:
            sub.Update()
            all_sensors.extend(sub.Sensors)
        all_sensors.extend(hw.Sensors)
        for sensor in all_sensors:
            if sensor.SensorType != target:
                continue
            label = str(sensor.Name)
            key = str(hw.Name)
            entry = {'label': label, 'current': round(float(sensor.Value), 1)}
            if sensor_type == 'Temperature' and is_cpu_gpu:
                # LHM exposes a "... Distance to TjMax" sensor as a Temperature
                # type, but its value is a margin, not a temperature -- drop it
                # so it is not shown as a bogus reading.
                if 'distance to tjmax' in label.lower():
                    continue
                # Match the Linux psutil schema exactly: each temperature entry
                # carries high/critical. LHM has no per-sensor threshold, so
                # report None -- the keys are present, so a manager that reads
                # entry['high']/['critical'] renders this like the Linux payload
                # instead of skipping CPU temps on a KeyError.
                entry['high'] = None
                entry['critical'] = None
                results.setdefault(key, []).append(entry)
            elif sensor_type == 'Fan' and ('cpu' in label.lower() or 'gpu' in label.lower()):
                results.setdefault(key, []).append(entry)
    return results


def temperatures() -> dict[str, list[dict]]:
    """CPU/GPU temperatures via LibreHardwareMonitor."""
    return _get_lhm_sensors('Temperature')


def fans() -> dict[str, list[dict]]:
    """CPU/GPU fan speeds via LibreHardwareMonitor."""
    return _get_lhm_sensors('Fan')


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
