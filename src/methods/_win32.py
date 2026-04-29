"""Windows-specific sensor and command implementations."""
import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import time

import psutil


# --- Audio (pycaw) ---------------------------------------------------------

def _get_windows_volume():
    """Lazy IAudioEndpointVolume reference for the default sink."""
    from pycaw.pycaw import AudioUtilities
    speakers = AudioUtilities.GetSpeakers()
    return speakers.EndpointVolume


def is_muted():
    """Whether the default audio endpoint is muted (via pycaw COM)."""
    return bool(_get_windows_volume().GetMute())


def mute():
    """Mute the default audio endpoint."""
    _get_windows_volume().SetMute(1, None)


def unmute():
    """Unmute the default audio endpoint."""
    _get_windows_volume().SetMute(0, None)


# --- Power ----------------------------------------------------------------

def shutdown():
    """Trigger immediate poweroff via 'shutdown /s /t 0' (Windows
    built-in)."""
    cmd = ['shutdown', '/s', '/t', '0']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'shutdown failed (rc={result.returncode}): {result.stderr.strip()}')


def reboot():
    """Trigger immediate reboot via 'shutdown /r /t 0'."""
    cmd = ['shutdown', '/r', '/t', '0']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'reboot failed (rc={result.returncode}): {result.stderr.strip()}')


# --- LibreHardwareMonitor (CPU/GPU temps + fans) --------------------------

_lhm_computer = None


def _get_lhm_computer():
    """Lazy-init the LibreHardwareMonitor Computer singleton. Loads
    the .NET assembly via pythonnet, registers an atexit cleanup for
    Computer.Close()."""
    global _lhm_computer
    if _lhm_computer is None:
        import clr
        lib_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'lib', 'win32')
        if lib_path not in sys.path:
            sys.path.append(lib_path)
        clr.AddReference('LibreHardwareMonitorLib')
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
    """Aggregate Temperature- or Fan-Sensoren von der LHM-Hardware-
    Liste. Filtert auf CPU/GPU-Hardware (sonst kommen Mainboard- und
    SSD-Sensoren mit, die der Manager nicht braucht). Schema matcht
    Linux psutil: {hw_name: [{label, current}, ...]}."""
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


def temperatures():
    """CPU/GPU temperatures via LibreHardwareMonitor."""
    return _get_lhm_sensors('Temperature')


def fans():
    """CPU/GPU fan speeds via LibreHardwareMonitor."""
    return _get_lhm_sensors('Fan')


# --- Misc -----------------------------------------------------------------

def uptime():
    """Seconds since boot via psutil."""
    return time.time() - psutil.boot_time()


def display():
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
