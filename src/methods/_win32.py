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
    don't sneak in (the manager doesn't need them). Schema matches
    the Linux psutil one: {hw_name: [{label, current}, ...]}."""
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
            # Schema matches Linux psutil (mehrere Sensoren pro Hardware).
            if sensor_type == 'Temperature' and is_cpu_gpu:
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
    """Active display mode as 'WIDTHxHEIGHT, RATE Hz' via the Win32
    EnumDisplaySettingsW API."""
    class DEVMODE(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", ctypes.wintypes.WCHAR * 32),
            ("dmSpecVersion", ctypes.wintypes.WORD),
            ("dmDriverVersion", ctypes.wintypes.WORD),
            ("dmSize", ctypes.wintypes.WORD),
            ("dmDriverExtra", ctypes.wintypes.WORD),
            ("dmFields", ctypes.wintypes.DWORD),
            ("dmPositionX", ctypes.c_long),
            ("dmPositionY", ctypes.c_long),
            ("dmDisplayOrientation", ctypes.wintypes.DWORD),
            ("dmDisplayFixedOutput", ctypes.wintypes.DWORD),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", ctypes.wintypes.WCHAR * 32),
            ("dmLogPixels", ctypes.wintypes.WORD),
            ("dmBitsPerPel", ctypes.wintypes.DWORD),
            ("dmPelsWidth", ctypes.wintypes.DWORD),
            ("dmPelsHeight", ctypes.wintypes.DWORD),
            ("dmDisplayFlags", ctypes.wintypes.DWORD),
            ("dmDisplayFrequency", ctypes.wintypes.DWORD),
        ]

    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    ENUM_CURRENT_SETTINGS = -1
    if ctypes.windll.user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return f'{dm.dmPelsWidth}x{dm.dmPelsHeight}, {dm.dmDisplayFrequency} Hz'
    return None
