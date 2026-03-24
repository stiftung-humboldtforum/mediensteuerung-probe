import json
from unittest.mock import patch, MagicMock
from collections import namedtuple

from methods import call_method, ping
from methods.sensors import uptime, temperatures, fans


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


@patch('methods.platform.system', return_value='Linux')
@patch('methods.subprocess.check_output')
def test_is_muted_linux(mock_output, mock_platform):
    mock_output.return_value = b'Volume: 0.50 [MUTED]'
    from methods import is_muted
    assert is_muted() is True


@patch('methods.platform.system', return_value='Linux')
@patch('methods.subprocess.check_output')
def test_is_muted_linux_not_muted(mock_output, mock_platform):
    mock_output.return_value = b'Volume: 0.50'
    from methods import is_muted
    assert is_muted() is False


@patch('methods.sensors.platform.system', return_value='Linux')
@patch('methods.sensors.open', create=True)
def test_uptime_linux(mock_open, mock_platform):
    mock_open.return_value.__enter__ = lambda s: s
    mock_open.return_value.__exit__ = MagicMock(return_value=False)
    mock_open.return_value.read = lambda: '12345.67 98765.43'
    assert uptime() == 12345.67


@patch('methods.sensors.platform.system', return_value='Linux')
@patch('methods.sensors.psutil.sensors_temperatures', create=True)
def test_temperatures_linux(mock_temps, mock_platform):
    STemp = namedtuple('shwtemp', ['label', 'current', 'high', 'critical'])
    mock_temps.return_value = {
        'coretemp': [STemp(label='Core 0', current=45.0, high=80.0, critical=100.0)]
    }
    result = temperatures()
    assert 'coretemp' in result
    assert result['coretemp'][0]['current'] == 45.0
    assert result['coretemp'][0]['label'] == 'Core 0'


@patch('methods.sensors.platform.system', return_value='Linux')
@patch('methods.sensors.psutil.sensors_fans', create=True)
def test_fans_linux(mock_fans, mock_platform):
    SFan = namedtuple('sfan', ['label', 'current'])
    mock_fans.return_value = {
        'thinkpad': [SFan(label='Fan 1', current=2500)]
    }
    result = fans()
    assert 'thinkpad' in result
    assert result['thinkpad'][0]['current'] == 2500
