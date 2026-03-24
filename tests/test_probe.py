import json
from unittest.mock import Mock, patch

from probe import Probe


def _make_probe(methods='ping,temperatures', capabilities='mute,unmute,shutdown'):
    client = Mock()
    config = {
        'PROBE_METHODS': methods,
        'PROBE_CAPABILITIES': capabilities,
    }
    return Probe('test.local', client=client, config=config)


def test_probe_init_parses_config():
    probe = _make_probe(methods='ping,temperatures,fans')
    assert 'ping' in probe.methods
    assert 'temperatures' in probe.methods
    assert 'fans' in probe.methods


def test_probe_init_missing_methods():
    client = Mock()
    config = {'PROBE_CAPABILITIES': 'shutdown'}
    probe = Probe('test.local', client=client, config=config)
    assert probe.methods == {}


def test_probe_allowed_methods():
    probe = _make_probe(capabilities='mute,unmute,reboot')
    assert probe._allowed_methods == {'mute', 'unmute', 'reboot'}


def test_on_disconnect_sets_flag():
    probe = _make_probe()
    probe.is_connected = True
    probe.on_disconnect(Mock(), None, 0)
    assert probe.is_connected is False


@patch('methods.display', return_value='1920x1080, 60 Hz')
def test_check_display_ok(mock_display):
    probe = _make_probe()
    probe.check_display()
    assert probe.errors['display'] == 'ok'


@patch('methods.display', return_value=None)
def test_check_display_error(mock_display):
    probe = _make_probe()
    probe.check_display()
    assert probe.errors['display'] == 'error'


@patch('methods.easire', return_value=True)
def test_check_easire_ok(mock_easire):
    probe = _make_probe()
    probe.check_easire()
    assert probe.errors['easire'] == 'ok'


@patch('methods.easire', return_value=None)
def test_check_easire_error(mock_easire):
    probe = _make_probe()
    probe.check_easire()
    assert probe.errors['easire'] == 'error'


@patch('methods.mpv_file_pos_sec', return_value=120)
def test_check_playback_pos_ok(mock_mpv):
    probe = _make_probe()
    probe.playback_pos = 100
    probe.check_playback_pos()
    assert probe.errors['playback'] == 'ok'
    assert probe.playback_pos == 120


@patch('methods.mpv_file_pos_sec', return_value=100)
def test_check_playback_pos_stale(mock_mpv):
    probe = _make_probe()
    probe.playback_pos = 100
    probe.check_playback_pos()
    assert probe.errors['playback'] == 'error'


def test_on_message_blocked():
    probe = _make_probe(capabilities='mute,unmute')
    client = Mock()
    msg = Mock()
    msg.topic = 'manager/test.local/shutdown'
    msg.payload = b''

    probe.on_message(client, None, msg)

    published = client.publish.call_args_list
    assert len(published) == 1
    response = json.loads(published[0][0][1])
    assert response['error']['message'] == 'Method not allowed'


def test_on_message_allowed():
    probe = _make_probe(capabilities='mute,unmute')
    client = Mock()
    msg = Mock()
    msg.topic = 'manager/test.local/mute'
    msg.payload = b''

    with patch('methods.mute') as mock_mute:
        probe.on_message(client, None, msg)
        mock_mute.assert_called_once()

    published = client.publish.call_args_list
    assert len(published) == 2
    received = json.loads(published[0][0][1])
    assert received['data']['status'] == 'received'


def test_on_message_with_payload():
    probe = _make_probe(capabilities='mute,unmute')
    client = Mock()
    msg = Mock()
    msg.topic = 'manager/test.local/mute'
    msg.payload = json.dumps({'args': [1], 'kwargs': {'test': True}}).encode()

    with patch('methods.mute') as mock_mute:
        probe.on_message(client, None, msg)
        mock_mute.assert_called_once_with(1, test=True)


def test_on_message_unknown_method():
    probe = _make_probe(capabilities='nonexistent')
    client = Mock()
    msg = Mock()
    msg.topic = 'manager/test.local/nonexistent'
    msg.payload = b''

    probe.on_message(client, None, msg)

    published = client.publish.call_args_list
    assert len(published) == 2
    response = json.loads(published[1][0][1])
    assert response['error']['message'] == 'Unknown method'
