"""Mock-based tests for methods/_win32.py.

These tests run on any platform — pycaw, pythonnet and ctypes.windll
are heavily mocked. They cover the 'has the function correct shape'
class of bugs (signature changes, COM-call ordering, LHM hardware-
filter logic) without needing actual Windows audio + admin rights.

For real Windows hardware verification see scripts/hardware-test-windows.ps1.
"""
import sys
from unittest.mock import MagicMock, patch

import pytest


def _stub_win_modules():
    """Pre-populate sys.modules with stub pycaw/clr/LHM modules so
    methods._win32 can be imported on Linux/macOS without the real
    libraries. Returns a dict of the inserted mocks for assertion."""
    # pycaw
    pycaw_root = MagicMock()
    pycaw_pycaw = MagicMock()
    pycaw_root.pycaw = pycaw_pycaw
    sys.modules.setdefault('pycaw', pycaw_root)
    sys.modules.setdefault('pycaw.pycaw', pycaw_pycaw)

    # pythonnet (clr)
    sys.modules.setdefault('clr', MagicMock())

    # LibreHardwareMonitor.Hardware
    lhm_root = MagicMock()
    lhm_hw = MagicMock()
    lhm_root.Hardware = lhm_hw
    sys.modules.setdefault('LibreHardwareMonitor', lhm_root)
    sys.modules.setdefault('LibreHardwareMonitor.Hardware', lhm_hw)

    return {
        'pycaw_pycaw': pycaw_pycaw,
        'lhm_hw': lhm_hw,
    }


_WIN_MOCKS = _stub_win_modules()


@pytest.fixture(autouse=True)
def _reset_win_mocks_and_lhm():
    """Per-test isolation: reset module-level mocks (pycaw_pycaw, lhm_hw),
    clear cached _lhm_computer, and clear _dll_hashes_verified so
    leftover MagicMock or hash-cache state from one test cannot bleed
    into the next (LHM-mocks chain through SubHardware/Sensors lists
    that retain references; the hash cache is a process-level set that
    sticks once a lib_path verifies)."""
    _WIN_MOCKS['pycaw_pycaw'].reset_mock(return_value=True, side_effect=True)
    _WIN_MOCKS['lhm_hw'].reset_mock(return_value=True, side_effect=True)
    try:
        from methods import _win32
        _win32._lhm_computer = None
        _win32._dll_hashes_verified.clear()
    except ImportError:
        pass
    yield


# --- Audio -----------------------------------------------------------------

def test_win32_is_muted_calls_pycaw_GetMute():
    from methods import _win32
    speakers = MagicMock()
    speakers.EndpointVolume.GetMute.return_value = 1
    _WIN_MOCKS['pycaw_pycaw'].AudioUtilities.GetSpeakers.return_value = speakers
    assert _win32.is_muted() is True
    speakers.EndpointVolume.GetMute.assert_called_once()


def test_win32_mute_calls_SetMute_with_1():
    from methods import _win32
    speakers = MagicMock()
    _WIN_MOCKS['pycaw_pycaw'].AudioUtilities.GetSpeakers.return_value = speakers
    _win32.mute()
    speakers.EndpointVolume.SetMute.assert_called_once_with(1, None)


def test_win32_unmute_calls_SetMute_with_0():
    from methods import _win32
    speakers = MagicMock()
    _WIN_MOCKS['pycaw_pycaw'].AudioUtilities.GetSpeakers.return_value = speakers
    _win32.unmute()
    speakers.EndpointVolume.SetMute.assert_called_once_with(0, None)


# --- Power -----------------------------------------------------------------

@patch('methods._win32.subprocess.run')
def test_win32_shutdown_calls_shutdown_s(mock_run):
    mock_run.return_value.returncode = 0
    from methods import _win32
    _win32.shutdown()
    args = mock_run.call_args[0][0]
    assert args == ['shutdown', '/s', '/t', '5']


@patch('methods._win32.subprocess.run')
def test_win32_reboot_calls_shutdown_r(mock_run):
    mock_run.return_value.returncode = 0
    from methods import _win32
    _win32.reboot()
    args = mock_run.call_args[0][0]
    assert args == ['shutdown', '/r', '/t', '5']


@patch('methods._win32.subprocess.run')
def test_win32_shutdown_raises_RuntimeError_on_nonzero_rc(mock_run):
    mock_run.return_value.returncode = 5
    mock_run.return_value.stderr = 'access denied'
    from methods import _win32
    with pytest.raises(RuntimeError, match='shutdown failed'):
        _win32.shutdown()


# --- Uptime ----------------------------------------------------------------

@patch('methods._win32.psutil.boot_time', return_value=1700000000.0)
@patch('methods._win32.time.time', return_value=1700001000.0)
def test_win32_uptime_returns_seconds_since_boot(mock_time, mock_boot):
    from methods import _win32
    assert _win32.uptime() == 1000.0


# --- LHM Sensoren ----------------------------------------------------------

def _make_lhm_sensor(name, value, sensor_type, target_type):
    s = MagicMock()
    s.SensorType = sensor_type
    s.Name = name
    s.Value = value
    return s


def _make_lhm_hardware(hw_name, hw_type, sensors, sub_sensors=()):
    hw = MagicMock()
    hw.Name = hw_name
    hw.HardwareType = hw_type
    hw.Sensors = sensors
    sub = MagicMock()
    sub.Sensors = sub_sensors
    hw.SubHardware = [sub] if sub_sensors else []
    return hw


def test_win32_lhm_temperatures_coretemp_shape():
    """CPU temps are remapped to the avorus-ui 'coretemp' payload: package
    first, then per-core, with numeric high/critical (TjMax = core + margin).
    Aggregates ('Core Max') and non-CPU hardware are dropped."""
    from methods import _win32

    _win32._lhm_computer = None

    SensorType = MagicMock()
    SensorType.Temperature = 'TEMP'
    SensorType.Fan = 'FAN'
    _WIN_MOCKS['lhm_hw'].SensorType = SensorType

    pkg = _make_lhm_sensor('CPU Package', 55.0, 'TEMP', 'TEMP')
    core1 = _make_lhm_sensor('CPU Core #1', 40.0, 'TEMP', 'TEMP')
    dist1 = _make_lhm_sensor('CPU Core #1 Distance to TjMax', 60.0, 'TEMP', 'TEMP')
    agg = _make_lhm_sensor('Core Max', 55.0, 'TEMP', 'TEMP')
    mb = _make_lhm_sensor('Motherboard', 35.0, 'TEMP', 'TEMP')

    cpu_hw = _make_lhm_hardware('Intel Core i7', 'Cpu', [pkg, core1, dist1, agg])
    mb_hw = _make_lhm_hardware('Z690 Board', 'Motherboard', [mb])

    computer = MagicMock()
    computer.Hardware = [cpu_hw, mb_hw]
    _WIN_MOCKS['lhm_hw'].Computer.return_value = computer

    result = _win32.temperatures()

    assert list(result.keys()) == ['coretemp']
    ct = result['coretemp']
    assert ct[0]['label'] == 'Package id 0' and ct[0]['current'] == 55.0
    assert ct[1]['label'] == 'Core 0' and ct[1]['current'] == 40.0
    # TjMax = 40 + 60 = 100 -> numeric high/critical for the UI hue maths
    assert ct[0]['high'] == 100.0 and ct[1]['critical'] == 100.0
    # aggregates ('Core Max') and motherboard temps dropped
    assert all('Max' not in e['label'] for e in ct)


def test_win32_lhm_fans_system_only_under_dell_smm():
    """System/chassis fans (non-GPU hardware) land under 'dell_smm'; the GPU
    fan is excluded -- it is not a system fan and would be a wrong value the
    manager cannot distinguish from one."""
    from methods import _win32

    _win32._lhm_computer = None

    SensorType = MagicMock()
    SensorType.Temperature = 'TEMP'
    SensorType.Fan = 'FAN'
    _WIN_MOCKS['lhm_hw'].SensorType = SensorType

    cpu_fan = _make_lhm_sensor('CPU Fan', 1500, 'FAN', 'FAN')
    sys_fan = _make_lhm_sensor('System Fan #1', 800, 'FAN', 'FAN')
    gpu_fan = _make_lhm_sensor('GPU Fan #1', 2000, 'FAN', 'FAN')

    mb_hw = _make_lhm_hardware('Dell Board', 'Motherboard', [cpu_fan, sys_fan])
    gpu_hw = _make_lhm_hardware('NVIDIA RTX 4080', 'GpuNvidia', [gpu_fan])

    computer = MagicMock()
    computer.Hardware = [mb_hw, gpu_hw]
    _WIN_MOCKS['lhm_hw'].Computer.return_value = computer

    result = _win32.fans()

    assert list(result.keys()) == ['dell_smm']
    labels = [s['label'] for s in result['dell_smm']]
    assert 'CPU Fan' in labels
    assert 'System Fan #1' in labels
    assert 'GPU Fan #1' not in labels


def test_win32_lhm_fans_gpu_only_returns_empty():
    """Dell workstations expose only the GPU fan to LHM -> no system fan ->
    return {} rather than a misleading GPU-as-system reading."""
    from methods import _win32

    _win32._lhm_computer = None

    SensorType = MagicMock()
    SensorType.Temperature = 'TEMP'
    SensorType.Fan = 'FAN'
    _WIN_MOCKS['lhm_hw'].SensorType = SensorType

    gpu_fan = _make_lhm_sensor('GPU Fan', 1500, 'FAN', 'FAN')
    gpu_hw = _make_lhm_hardware('NVIDIA Quadro RTX 4000', 'GpuNvidia', [gpu_fan])

    computer = MagicMock()
    computer.Hardware = [gpu_hw]
    _WIN_MOCKS['lhm_hw'].Computer.return_value = computer

    assert _win32.fans() == {}


# --- Display (Win32 EnumDisplaySettings) -----------------------------------

@patch('methods._win32.subprocess.run')
def test_win32_display_returns_resolution_string(mock_run):
    """display() returns the WMI Win32_VideoController line verbatim
    ('WIDTHxHEIGHT, RATE Hz'). EnumDisplaySettings is no longer used --
    it is session-bound and wrong from the session-0 service."""
    from methods import _win32
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = '3840x2160, 30 Hz\n'
    assert _win32.display() == '3840x2160, 30 Hz'


# --- DLL hash verification ------------------------------------------------

def test_verify_dll_hashes_ok(tmp_path):
    """Manifest matches actual file hashes → no exception, set entry added."""
    from methods import _win32
    _win32._dll_hashes_verified.clear()

    dll = tmp_path / 'fake.dll'
    dll.write_bytes(b'\x00\x01\x02\x03')
    import hashlib
    digest = hashlib.sha256(dll.read_bytes()).hexdigest()
    (tmp_path / 'SHA256SUMS').write_text(f'{digest} *fake.dll\n')

    _win32._verify_dll_hashes(str(tmp_path))
    assert str(tmp_path) in _win32._dll_hashes_verified


def test_verify_dll_hashes_mismatch_raises(tmp_path):
    from methods import _win32
    _win32._dll_hashes_verified.clear()

    (tmp_path / 'fake.dll').write_bytes(b'tampered')
    bogus = '0' * 64
    (tmp_path / 'SHA256SUMS').write_text(f'{bogus} *fake.dll\n')

    with pytest.raises(RuntimeError, match='hash mismatch'):
        _win32._verify_dll_hashes(str(tmp_path))


def test_verify_dll_hashes_missing_manifest_raises(tmp_path):
    from methods import _win32
    _win32._dll_hashes_verified.clear()
    with pytest.raises(RuntimeError, match='Hash manifest not found'):
        _win32._verify_dll_hashes(str(tmp_path))


def test_verify_dll_hashes_path_traversal_rejected(tmp_path):
    """Manifest with `../` in filename column must be refused — the
    DLL outside lib_path could be a system file the attacker chose."""
    from methods import _win32
    _win32._dll_hashes_verified.clear()

    (tmp_path / 'SHA256SUMS').write_text(f'{"0"*64} *../etc/passwd\n')
    with pytest.raises(RuntimeError, match='Invalid manifest entry'):
        _win32._verify_dll_hashes(str(tmp_path))


def test_verify_dll_hashes_invalid_digest_rejected(tmp_path):
    from methods import _win32
    _win32._dll_hashes_verified.clear()

    (tmp_path / 'fake.dll').write_bytes(b'')
    (tmp_path / 'SHA256SUMS').write_text('not-a-hex-digest *fake.dll\n')
    with pytest.raises(RuntimeError, match='Invalid SHA256 digest'):
        _win32._verify_dll_hashes(str(tmp_path))


def test_verify_dll_hashes_cached(tmp_path):
    """Second call must short-circuit (no re-hash) if already verified."""
    from methods import _win32
    _win32._dll_hashes_verified.clear()

    dll = tmp_path / 'fake.dll'
    dll.write_bytes(b'first content')
    import hashlib
    digest = hashlib.sha256(dll.read_bytes()).hexdigest()
    (tmp_path / 'SHA256SUMS').write_text(f'{digest} *fake.dll\n')
    _win32._verify_dll_hashes(str(tmp_path))

    # Tamper after first call — second call must NOT re-detect
    dll.write_bytes(b'tampered after first verify')
    _win32._verify_dll_hashes(str(tmp_path))  # cached → no exception


@patch('methods._win32.subprocess.run')
def test_win32_display_returns_none_on_failure(mock_run):
    """Non-zero powershell/WMI exit → display() returns None."""
    from methods import _win32
    mock_run.return_value.returncode = 1
    mock_run.return_value.stdout = ''
    assert _win32.display() is None
