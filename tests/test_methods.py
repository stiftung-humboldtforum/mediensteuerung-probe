import json
import subprocess
from unittest.mock import patch, mock_open
from collections import namedtuple

import pytest

from methods import call_method, ping, wake, boot_time, easire, mpv_file_pos_sec


def test_call_method_success():
    result = json.loads(call_method(lambda: 42))
    assert result['data']['status'] == 'complete'
    assert result['data']['result'] == 42


def test_call_method_with_args():
    def add(a, b):
        return a + b

    result = json.loads(call_method(add, 3, 7))
    assert result['data']['result'] == 10


def test_call_method_with_kwargs():
    def greet(name='world'):
        return f'hello {name}'

    result = json.loads(call_method(greet, name='test'))
    assert result['data']['result'] == 'hello test'


def test_call_method_exception():
    def failing():
        raise ValueError('test error')

    result = json.loads(call_method(failing))
    assert result['error']['message'] == 'ValueError'
    assert 'test error' in result['error']['errors']


def test_ping_returns_none():
    assert ping() is None


def test_wake_returns_awake():
    """wake() is a no-op acknowledgement — manager triggers WoL externally,
    but if the topic IS published (e.g. as confirm-roundtrip after the host
    woke up), probe must answer with a stable string instead of falling
    through to 'Unknown method'."""
    assert wake() == 'awake'


def test_call_method_wake_roundtrip_envelope():
    """End-to-end through call_method — wake's 'awake' must show up as
    the result in the response envelope."""
    result = json.loads(call_method(wake))
    assert result['data']['status'] == 'complete'
    assert result['data']['result'] == 'awake'


def test_call_method_with_unserializable_exception_args():
    """call_method must not crash json.dumps when an exception carries
    non-JSON-serializable args (e.g. an open file or a ctypes pointer).
    The repr() fallback in _safe_args keeps the publish cycle alive."""
    class _NotJsonSerializable:
        def __repr__(self):
            return '<sentinel>'

    def failing():
        raise RuntimeError('boom', _NotJsonSerializable())

    result = json.loads(call_method(failing))
    assert result['error']['message'] == 'RuntimeError'
    # First arg is the plain string, second is the repr fallback
    assert result['error']['errors'][0] == 'boom'
    assert result['error']['errors'][1] == '<sentinel>'


def test_call_method_with_broken_repr_does_not_crash():
    """Even a __repr__ that raises must not break the publish cycle —
    _safe_args wraps repr() in try/except and emits a placeholder."""
    class _BrokenRepr:
        def __repr__(self):
            raise RuntimeError('repr-bug')

    def failing():
        raise ValueError('outer', _BrokenRepr())

    result = json.loads(call_method(failing))
    assert result['error']['message'] == 'ValueError'
    assert result['error']['errors'][0] == 'outer'
    assert '_BrokenRepr' in result['error']['errors'][1]
    assert 'unrepresentable' in result['error']['errors'][1]


def test_mpv_file_pos_sec_accepts_float_output():
    """Some mpv_control reference impls round in shell, others print
    raw 'time-pos' as float — probe must tolerate both."""
    with patch('methods.subprocess.run') as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = b'12.345\n'
        assert mpv_file_pos_sec() == 12


def test_mpv_file_pos_sec_handles_nan():
    """mpv reports 'nan' on paused/seeking — must not crash."""
    with patch('methods.subprocess.run') as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = b'nan\n'
        assert mpv_file_pos_sec() is None


def test_mpv_file_pos_sec_handles_garbage():
    """Non-parseable output → None instead of ValueError crash."""
    with patch('methods.subprocess.run') as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = b'NOT_A_NUMBER\n'
        assert mpv_file_pos_sec() is None


def test_boot_time_returns_float():
    assert isinstance(boot_time(), float)


# --- Linux platform tests --------------------------------------------------
# These import from methods._linux directly so they run regardless of host
# platform; subprocess/psutil get patched per test.

@patch('methods._linux.subprocess.check_output')
def test_is_muted_linux(mock_output):
    mock_output.return_value = b'Volume: 0.50 [MUTED]'
    from methods._linux import is_muted
    assert is_muted() is True


@patch('methods._linux.subprocess.check_output')
def test_is_muted_linux_not_muted(mock_output):
    mock_output.return_value = b'Volume: 0.50'
    from methods._linux import is_muted
    assert is_muted() is False


def test_uptime_linux():
    from methods import _linux
    with patch.object(_linux, 'open', mock_open(read_data='12345.67 98765.43'), create=True):
        assert _linux.uptime() == 12345.67


@patch('methods._linux.psutil.sensors_temperatures', create=True)
def test_temperatures_linux(mock_temps):
    STemp = namedtuple('shwtemp', ['label', 'current', 'high', 'critical'])
    mock_temps.return_value = {
        'coretemp': [STemp(label='Core 0', current=45.0, high=80.0, critical=100.0)]
    }
    from methods._linux import temperatures
    result = temperatures()
    assert 'coretemp' in result
    assert result['coretemp'][0]['current'] == 45.0
    assert result['coretemp'][0]['label'] == 'Core 0'


@patch('methods._linux.psutil.sensors_fans', create=True)
def test_fans_linux(mock_fans):
    SFan = namedtuple('sfan', ['label', 'current'])
    mock_fans.return_value = {
        'thinkpad': [SFan(label='Fan 1', current=2500)]
    }
    from methods._linux import fans
    result = fans()
    assert 'thinkpad' in result
    assert result['thinkpad'][0]['current'] == 2500


# --- Linux display() output parsing ---------------------------------------

# Realistic xrandr output snippet (only the relevant section):
_XRANDR_OUTPUT_NORMAL = b"""\
Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767
HDMI-0 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   1920x1080     60.00*+  74.97
   1280x1024     75.02
"""

_XRANDR_OUTPUT_NO_ACTIVE = b"""\
Screen 0: minimum 8 x 8, current 0 x 0, maximum 32767 x 32767
HDMI-0 connected (normal left inverted right x axis y axis)
   1920x1080     60.00
   1280x1024     75.02
"""

_XRANDR_OUTPUT_HIGH_REFRESH = b"""\
Screen 0: minimum 8 x 8, current 2560 x 1440, maximum 32767 x 32767
DP-0 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   2560x1440    144.00*+ 120.00 100.00 60.00
"""

# Two connected outputs; the kiosk-relevant 'primary' is the SECOND one.
# Plain "first '*' wins" parsing would return the wrong monitor — test
# guards against that regression.
_XRANDR_OUTPUT_MULTI_MONITOR = b"""\
Screen 0: minimum 8 x 8, current 5760 x 1080, maximum 32767 x 32767
HDMI-1 connected 3840x2160+1920+0 (normal left inverted right x axis y axis) 597mm x 336mm
   3840x2160     30.00*+  29.97
DP-2 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   1920x1080     60.00*+  74.97
"""


@patch('methods._linux.subprocess.run')
def test_display_linux_parses_normal_output(mock_run):
    """Standard xrandr output with active mode '*+' marker."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=['xrandr', '--current'], returncode=0,
        stdout=_XRANDR_OUTPUT_NORMAL, stderr=b'',
    )
    from methods._linux import display
    assert display() == '1920x1080, 60.00 Hz'


@patch('methods._linux.subprocess.run')
def test_display_linux_no_active_mode_returns_none(mock_run):
    """If xrandr has no '*' marker (display unconfigured)."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=['xrandr', '--current'], returncode=0,
        stdout=_XRANDR_OUTPUT_NO_ACTIVE, stderr=b'',
    )
    from methods._linux import display
    assert display() is None


@patch('methods._linux.subprocess.run')
def test_display_linux_high_refresh_rate(mock_run):
    """144Hz monitors must parse without errors."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=['xrandr', '--current'], returncode=0,
        stdout=_XRANDR_OUTPUT_HIGH_REFRESH, stderr=b'',
    )
    from methods._linux import display
    assert display() == '2560x1440, 144.00 Hz'


@patch('methods._linux.subprocess.run')
def test_display_linux_multi_monitor_picks_primary(mock_run):
    """When xrandr lists two connected outputs each with their own '*'
    mode line, display() must return the 'primary' output's mode, not
    the first output encountered."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=['xrandr', '--current'], returncode=0,
        stdout=_XRANDR_OUTPUT_MULTI_MONITOR, stderr=b'',
    )
    from methods._linux import display
    assert display() == '1920x1080, 60.00 Hz'


@patch('methods._linux.subprocess.run')
def test_display_linux_xrandr_fails_returns_none(mock_run):
    """xrandr exit-non-zero (e.g. no DISPLAY) → None."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=['xrandr', '--current'], returncode=1,
        stdout=b'', stderr=b"Can't open display",
    )
    from methods._linux import display
    assert display() is None


# --- Subprocess-Timeout coverage (R1) -------------------------------------

@patch('methods._linux.subprocess.check_output')
def test_is_muted_linux_timeout_propagates(mock_output):
    mock_output.side_effect = subprocess.TimeoutExpired(['wpctl'], 3)
    from methods._linux import is_muted
    with pytest.raises(subprocess.TimeoutExpired):
        is_muted()


@patch('methods._linux.subprocess.run')
def test_display_linux_timeout_returns_none_via_caller(mock_run):
    """display() doesn't catch TimeoutExpired itself — caller (Probe.check_display)
    is supposed to. Here we just verify the raise propagates cleanly."""
    mock_run.side_effect = subprocess.TimeoutExpired(['xrandr', '--current'], 5)
    from methods._linux import display
    with pytest.raises(subprocess.TimeoutExpired):
        display()


@patch('methods.subprocess.run')
def test_mpv_file_pos_sec_timeout_propagates(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(['mpv_control'], 3)
    with pytest.raises(subprocess.TimeoutExpired):
        mpv_file_pos_sec()


# --- Common easire (psutil-based, platform-agnostic) ----------------------

@patch('methods.psutil.process_iter')
def test_easire_running(mock_iter):
    proc = type('P', (), {'info': {'name': 'easire-player', 'cmdline': []}})()
    mock_iter.return_value = [proc]
    assert easire() is True


@patch('methods.psutil.process_iter')
def test_easire_not_running(mock_iter):
    mock_iter.return_value = []
    assert easire() is None
