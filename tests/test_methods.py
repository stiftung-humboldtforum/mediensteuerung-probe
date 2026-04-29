import json
from unittest.mock import patch, mock_open
from collections import namedtuple

from methods import call_method, ping, boot_time, easire


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
