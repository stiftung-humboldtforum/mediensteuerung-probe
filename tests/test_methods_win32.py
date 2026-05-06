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
    assert args == ['shutdown', '/s', '/t', '0']


@patch('methods._win32.subprocess.run')
def test_win32_reboot_calls_shutdown_r(mock_run):
    mock_run.return_value.returncode = 0
    from methods import _win32
    _win32.reboot()
    args = mock_run.call_args[0][0]
    assert args == ['shutdown', '/r', '/t', '0']


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


def test_win32_lhm_temperatures_filters_to_cpu_gpu():
    """Mainboard / disk sensors must be filtered out — only CPU/GPU
    temperatures land in the result."""
    from methods import _win32

    # Reset any previously cached LHM state
    _win32._lhm_computer = None

    SensorType = MagicMock()
    SensorType.Temperature = 'TEMP'
    SensorType.Fan = 'FAN'
    _WIN_MOCKS['lhm_hw'].SensorType = SensorType

    cpu_sensor = _make_lhm_sensor('Core 0', 55.0, 'TEMP', 'TEMP')
    mb_sensor = _make_lhm_sensor('Motherboard', 35.0, 'TEMP', 'TEMP')
    gpu_sensor = _make_lhm_sensor('GPU Core', 60.0, 'TEMP', 'TEMP')

    cpu_hw = _make_lhm_hardware('Intel Core i7', 'Cpu', [cpu_sensor])
    mb_hw = _make_lhm_hardware('Z690 Board', 'Motherboard', [mb_sensor])
    gpu_hw = _make_lhm_hardware('NVIDIA RTX 4080', 'GpuNvidia', [gpu_sensor])

    computer = MagicMock()
    computer.Hardware = [cpu_hw, mb_hw, gpu_hw]
    _WIN_MOCKS['lhm_hw'].Computer.return_value = computer

    result = _win32.temperatures()

    assert 'Intel Core i7' in result, 'CPU temp should appear'
    assert 'NVIDIA RTX 4080' in result, 'GPU temp should appear'
    assert 'Z690 Board' not in result, 'Motherboard temp should be filtered out'
    assert result['Intel Core i7'][0]['current'] == 55.0
    assert result['NVIDIA RTX 4080'][0]['current'] == 60.0


def test_win32_lhm_fans_only_keeps_cpu_gpu_labeled():
    """Fan-Filter: nur Sensoren mit 'cpu' oder 'gpu' im Label — case
    insensitive."""
    from methods import _win32

    _win32._lhm_computer = None

    SensorType = MagicMock()
    SensorType.Temperature = 'TEMP'
    SensorType.Fan = 'FAN'
    _WIN_MOCKS['lhm_hw'].SensorType = SensorType

    cpu_fan = _make_lhm_sensor('CPU Fan', 1500, 'FAN', 'FAN')
    case_fan = _make_lhm_sensor('Case Fan #1', 800, 'FAN', 'FAN')
    gpu_fan = _make_lhm_sensor('GPU Fan #1', 2000, 'FAN', 'FAN')

    cpu_hw = _make_lhm_hardware('Intel Core i7', 'Cpu',
                                 [cpu_fan, case_fan])
    gpu_hw = _make_lhm_hardware('NVIDIA RTX 4080', 'GpuNvidia',
                                 [gpu_fan])

    computer = MagicMock()
    computer.Hardware = [cpu_hw, gpu_hw]
    _WIN_MOCKS['lhm_hw'].Computer.return_value = computer

    result = _win32.fans()

    # CPU- und GPU-Fans drin, Case-Fan gefiltert
    flat = [(hw, s['label']) for hw, sensors in result.items() for s in sensors]
    labels = [label for (_, label) in flat]
    assert 'CPU Fan' in labels
    assert 'GPU Fan #1' in labels
    assert 'Case Fan #1' not in labels


# --- Display (Win32 EnumDisplaySettings) -----------------------------------

@patch('methods._win32.ctypes')
def test_win32_display_returns_resolution_string(mock_ctypes):
    """Mock the EnumDisplaySettingsW call — verify return-format is
    'WIDTHxHEIGHT, RATE Hz'."""
    from methods import _win32

    # EnumDisplaySettingsW returns truthy; DEVMODE struct gets filled.
    mock_ctypes.windll.user32.EnumDisplaySettingsW.return_value = 1

    # Configure the DEVMODE-instance the code creates locally — patching
    # ctypes.Structure isn't easy, so we route via the side_effect of
    # EnumDisplaySettingsW: it 'fills' the struct passed in.
    def _fill_devmode(name, mode_num, dm_byref):
        dm = dm_byref._obj
        dm.dmPelsWidth = 1920
        dm.dmPelsHeight = 1080
        dm.dmDisplayFrequency = 60
        return 1

    mock_ctypes.windll.user32.EnumDisplaySettingsW.side_effect = _fill_devmode
    # ctypes.byref must still wrap the DEVMODE so we can read it back
    mock_ctypes.byref.side_effect = lambda x: type('Byref', (), {'_obj': x})()
    # ctypes.sizeof needed for dm.dmSize
    mock_ctypes.sizeof.return_value = 220
    # Need the wintypes still
    import ctypes as real_ctypes
    mock_ctypes.Structure = real_ctypes.Structure
    mock_ctypes.wintypes = real_ctypes.wintypes
    mock_ctypes.c_long = real_ctypes.c_long
    mock_ctypes.c_short = real_ctypes.c_short

    result = _win32.display()
    assert result == '1920x1080, 60 Hz'


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


@patch('methods._win32.ctypes')
def test_win32_display_returns_none_on_api_failure(mock_ctypes):
    """EnumDisplaySettingsW returning 0 → display() returns None."""
    from methods import _win32

    mock_ctypes.windll.user32.EnumDisplaySettingsW.return_value = 0
    mock_ctypes.sizeof.return_value = 220
    mock_ctypes.byref = lambda x: x
    import ctypes as real_ctypes
    mock_ctypes.Structure = real_ctypes.Structure
    mock_ctypes.wintypes = real_ctypes.wintypes
    mock_ctypes.c_long = real_ctypes.c_long
    mock_ctypes.c_short = real_ctypes.c_short

    assert _win32.display() is None
