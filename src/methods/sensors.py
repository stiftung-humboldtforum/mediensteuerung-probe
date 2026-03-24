import os
import platform
import subprocess
import time
import psutil

_lhm_computer = None

def _get_lhm_computer():
    global _lhm_computer
    if _lhm_computer is None:
        import clr
        import sys
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
    c = _get_lhm_computer()
    from LibreHardwareMonitor.Hardware import SensorType
    target = {'Temperature': SensorType.Temperature, 'Fan': SensorType.Fan}[sensor_type]
    results = {}
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
            if key in results:
                continue
            if sensor_type == 'Temperature' and is_cpu_gpu:
                results[key] = [{'label': label, 'current': round(float(sensor.Value), 1)}]
            elif sensor_type == 'Fan' and ('cpu' in label.lower() or 'gpu' in label.lower()):
                results[key] = [{'label': label, 'current': round(float(sensor.Value), 1)}]
    return results

def temperatures():
    if platform.system() == 'Linux':
        sensors = psutil.sensors_temperatures()
        return {key: [temp._asdict() for temp in sensor] for key, sensor in sensors.items()}
    elif platform.system() == 'Windows':
        return _get_lhm_sensors('Temperature')
    else:
        return {}

def fans():
    if platform.system() == 'Linux':
        sensors = psutil.sensors_fans()
        return {key: [fan._asdict() for fan in sensor] for key, sensor in sensors.items()}
    elif platform.system() == 'Windows':
        return _get_lhm_sensors('Fan')
    else:
        return {}

def boot_time():
    return psutil.boot_time()

def uptime():
    if platform.system() == 'Linux':
        with open('/proc/uptime', 'r') as f:
            return float(f.read().split(' ')[0])
    else:
        return time.time() - psutil.boot_time()

def mpv_file_pos_sec():
    p = subprocess.run('mpv_control file_pos_sec', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    if p.returncode == 0:
        return int(p.stdout.strip().decode())
    return None


def display():
    if platform.system() == 'Linux':
        p = subprocess.run('xrandr --current', env={**os.environ, 'DISPLAY': ':0'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        if p.returncode == 0:
            mode = [line for line in p.stdout.decode().splitlines() if '*' in line]
            if len(mode) > 0:
                mode = mode[0]
                mode = ' '.join(mode.split()).split(' ')
                resolution = mode[0]
                rate = ''.join([c for c in mode[1] if c.isdigit() or c == '.'])
                return f'{resolution}, {rate} Hz'
    elif platform.system() == 'Windows':
        import ctypes
        import ctypes.wintypes

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


def easire():
    if platform.system() == 'Linux':
        p = subprocess.run('ps ax | grep -q [e]asire-player', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        if p.returncode == 0:
            return True
    elif platform.system() == 'Windows':
        for proc in psutil.process_iter(['name', 'cmdline']):
            try:
                if 'easire-player' in (proc.info['name'] or ''):
                    return True
                if any('easire-player' in arg for arg in (proc.info['cmdline'] or [])):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return None
