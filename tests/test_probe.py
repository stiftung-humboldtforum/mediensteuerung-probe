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


def test_probe_init_missing_methods():
    client = Mock()
    config = {'PROBE_CAPABILITIES': 'shutdown'}
    probe = Probe('test.local', client=client, config=config)
    assert probe.methods == {}


def test_probe_methods_strips_whitespace():
    """'mute, unmute' (operator-formatted) must parse same as 'mute,unmute'."""
    probe = _make_probe(methods='ping, temperatures , fans', capabilities='mute, unmute')
    assert 'ping' in probe.methods
    assert 'temperatures' in probe.methods
    assert 'fans' in probe.methods
    assert probe._allowed_methods == {'mute', 'unmute'}


def test_probe_unknown_capabilities_logged(caplog):
    """Capability typo (e.g. 'reebot') must surface a warning so operators
    catch the mistake in journalctl rather than discovering the missing
    button only when they try to use it."""
    import logging
    with caplog.at_level(logging.WARNING):
        _make_probe(capabilities='mute,reeboot,wake')
    messages = [r.getMessage() for r in caplog.records]
    assert any('unknown commands' in m and 'reeboot' in m for m in messages)


def test_probe_version_published_on_connect():
    """on_connect must publish probe/<fqdn>/version retained — manager
    fleet-dashboard relies on it for version-drift detection."""
    from misc import VERSION
    probe = _make_probe()
    probe.on_connect(probe.client, None, flags=Mock(), reason_code=0)
    publish_calls = probe.client.publish.call_args_list
    version_topic_calls = [c for c in publish_calls if c.args[0].endswith('/version')]
    assert len(version_topic_calls) == 1
    call = version_topic_calls[0]
    # Publish keyword args: payload, qos, retain
    assert call.kwargs.get('payload') == VERSION
    assert call.kwargs.get('retain') is True
    assert call.kwargs.get('qos') == 1


def test_on_disconnect_sets_flag():
    probe = _make_probe()
    probe.connected_event.set()
    probe.on_disconnect(Mock(), None, disconnect_flags=0, reason_code=0)
    assert probe.is_connected is False
    assert not probe.connected_event.is_set()


def test_on_connect_sets_event():
    probe = _make_probe()
    assert not probe.connected_event.is_set()
    probe.on_connect(probe.client, None, flags=Mock(), reason_code=0)
    assert probe.connected_event.is_set()
    assert probe.is_connected is True


@patch('methods.display', return_value='1920x1080, 60 Hz')
def test_check_display_ok(mock_display):
    probe = _make_probe()
    probe.check_display()
    assert probe.errors['display'] == 'ok'
    topic, payload = probe.client.publish.call_args[0]
    assert topic == 'probe/test.local/v1/display'
    assert json.loads(payload)['data']['result'] == '1920x1080, 60 Hz'


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


@patch('methods.mpv_file_pos_sec', side_effect=ValueError('boom'))
def test_check_playback_pos_exception(mock_mpv):
    probe = _make_probe()
    probe.check_playback_pos()
    assert probe.errors['playback'] == 'error'


@patch('methods.display', side_effect=RuntimeError('xrandr down'))
def test_check_display_exception(mock_display):
    probe = _make_probe()
    probe.check_display()
    assert probe.errors['display'] == 'error'


@patch('methods.easire', side_effect=OSError('proc gone'))
def test_check_easire_exception(mock_easire):
    probe = _make_probe()
    probe.check_easire()
    assert probe.errors['easire'] == 'error'


@patch('methods.display')
def test_check_display_subprocess_timeout(mock_display):
    """If display() raises TimeoutExpired (from xrandr hang), check_display
    must record 'error' and not crash the Probe-Thread."""
    import subprocess
    mock_display.side_effect = subprocess.TimeoutExpired(['xrandr'], 5)
    probe = _make_probe()
    probe.check_display()
    assert probe.errors['display'] == 'error'


@patch('methods.mpv_file_pos_sec')
def test_check_playback_subprocess_timeout(mock_mpv):
    """Same for mpv_control hangs."""
    import subprocess
    mock_mpv.side_effect = subprocess.TimeoutExpired(['mpv_control'], 3)
    probe = _make_probe()
    probe.check_playback_pos()
    assert probe.errors['playback'] == 'error'


def test_on_message_blocked():
    probe = _make_probe(capabilities='mute,unmute')
    client = Mock()
    msg = Mock()
    msg.topic = 'manager/test.local/v1/shutdown'
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
    msg.topic = 'manager/test.local/v1/mute'
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
    msg.topic = 'manager/test.local/v1/mute'
    msg.payload = json.dumps({'args': [1], 'kwargs': {'test': True}}).encode()

    with patch('methods.mute') as mock_mute:
        probe.on_message(client, None, msg)
        mock_mute.assert_called_once_with(1, test=True)


def test_on_message_unknown_method():
    probe = _make_probe(capabilities='nonexistent')
    client = Mock()
    msg = Mock()
    msg.topic = 'manager/test.local/v1/nonexistent'
    msg.payload = b''

    probe.on_message(client, None, msg)

    published = client.publish.call_args_list
    assert len(published) == 2
    response = json.loads(published[1][0][1])
    assert response['error']['message'] == 'Unknown method'


def test_on_message_malformed_topic_short():
    probe = _make_probe()
    client = Mock()
    msg = Mock()
    msg.topic = 'manager/test.local'
    msg.payload = b''
    probe.on_message(client, None, msg)
    assert client.publish.call_count == 0


def test_on_message_malformed_topic_empty_method():
    probe = _make_probe()
    client = Mock()
    msg = Mock()
    msg.topic = 'manager/test.local/'
    msg.payload = b''
    probe.on_message(client, None, msg)
    assert client.publish.call_count == 0


def test_on_message_module_attribute_blocked():
    probe = _make_probe(capabilities='os,subprocess,call_method')
    client = Mock()

    for forbidden in ('os', 'subprocess', 'call_method'):
        client.reset_mock()
        msg = Mock()
        msg.topic = f'manager/test.local/v1/{forbidden}'
        msg.payload = b''
        probe.on_message(client, None, msg)
        published = client.publish.call_args_list
        final = json.loads(published[-1][0][1])
        assert final['error']['message'] == 'Unknown method', f'{forbidden} was not blocked'


def test_missing_capabilities_fail_closed():
    client = Mock()
    config = {'PROBE_METHODS': 'ping'}
    probe = Probe('test.local', client=client, config=config)
    assert probe.capabilities == ''
    assert probe._allowed_methods == {''}

    msg = Mock()
    msg.topic = 'manager/test.local/v1/shutdown'
    msg.payload = b''
    probe.on_message(client, None, msg)
    published = client.publish.call_args_list
    response = json.loads(published[-1][0][1])
    assert response['error']['message'] == 'Method not allowed'


def test_periodic_methods_excludes_commands():
    probe = _make_probe(methods='ping,shutdown,reboot,mute,temperatures')
    assert 'shutdown' not in probe.methods
    assert 'reboot' not in probe.methods
    assert 'mute' not in probe.methods
    assert 'ping' in probe.methods
    assert 'temperatures' in probe.methods


# --- Polling ---------------------------------------------------------------

def test_poll_late_binds_via_methods_module():
    """Patches applied AFTER probe init must still take effect — the
    polling loop late-binds via getattr(methods, name)."""
    probe = _make_probe(methods='temperatures')
    with patch('methods.temperatures', return_value={'patched': [{'current': 99}]}):
        probe.poll()
    # Find the temperatures publish (errors-publish is the last call).
    calls = [c for c in probe.client.publish.call_args_list if c[0][0].endswith('/temperatures')]
    assert len(calls) == 1
    payload = json.loads(calls[0][0][1])
    assert payload['data']['result'] == {'patched': [{'current': 99}]}


def test_poll_publishes_errors_topic_per_cycle():
    """Every poll cycle ends with one errors-topic publish summarising
    per-sensor status."""
    probe = _make_probe(methods='ping')
    probe.poll()
    calls = [c for c in probe.client.publish.call_args_list if c[0][0].endswith('/errors')]
    assert len(calls) == 1


def test_is_connected_mirrors_connected_event():
    """is_connected is now a thin property over connected_event so both
    stay in sync without separate book-keeping."""
    probe = _make_probe()
    assert probe.is_connected is False
    probe.connected_event.set()
    assert probe.is_connected is True
    probe.connected_event.clear()
    assert probe.is_connected is False
