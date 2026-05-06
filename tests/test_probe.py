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
    probe.is_connected = True
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


def test_on_connect_bumps_heartbeat():
    """Connect handshake itself counts as initial heartbeat — saves
    App.run from waiting up to 5s for the first call_methods() cycle
    before the watchdog gets its first ping."""
    probe = _make_probe()
    assert probe.heartbeat == 0
    probe.on_connect(probe.client, None, flags=Mock(), reason_code=0)
    assert probe.heartbeat == 1


@patch('methods.display', return_value='1920x1080, 60 Hz')
def test_check_display_ok(mock_display):
    probe = _make_probe()
    probe.check_display()
    assert probe.errors['display'] == 'ok'
    topic, payload = probe.client.publish.call_args[0]
    assert topic == 'probe/test.local/display'
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
        msg.topic = f'manager/test.local/{forbidden}'
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
    msg.topic = 'manager/test.local/shutdown'
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


# --- Threading lifecycle (Probe.start/stop integration) ----------------

def test_probe_thread_starts_and_stops_quickly():
    """stop() must wake the run-loop out of its 5s wait immediately."""
    import time as _time
    probe = _make_probe()
    probe.start()
    try:
        # Thread läuft (im wait, weil is_connected False)
        assert probe.is_alive()
    finally:
        t0 = _time.monotonic()
        probe.stop()
        probe.join(timeout=2)
        elapsed = _time.monotonic() - t0
    assert not probe.is_alive(), 'thread did not terminate within 2s'
    assert elapsed < 1.0, f'stop took {elapsed:.2f}s — Event-wait should be near-instant'


def test_probe_thread_bumps_heartbeat_when_connected():
    """Probe.run() should call call_methods() and bump heartbeat at
    least once when is_connected is True."""
    import time as _time
    probe = _make_probe(methods='ping')
    probe.is_connected = True
    initial = probe.heartbeat
    probe.start()
    try:
        # Erster Cycle ist sofort (is_connected=True beim Start),
        # ohne 5s wait.
        deadline = _time.monotonic() + 1.0
        while probe.heartbeat == initial and _time.monotonic() < deadline:
            _time.sleep(0.05)
    finally:
        probe.stop()
        probe.join(timeout=2)
    assert probe.heartbeat > initial, 'heartbeat did not increment'


def test_probe_thread_idle_when_disconnected():
    """Probe.run with is_connected=False must NOT call call_methods()
    (no publishes happen)."""
    import time as _time
    probe = _make_probe(methods='ping')
    probe.is_connected = False
    probe.client.reset_mock()
    probe.start()
    try:
        _time.sleep(0.2)
    finally:
        probe.stop()
        probe.join(timeout=2)
    assert probe.client.publish.call_count == 0
    assert probe.heartbeat == 0


def test_call_methods_late_binds_via_methods_module():
    """Patches applied AFTER probe init must still take effect."""
    probe = _make_probe(methods='temperatures')
    probe.is_connected = True
    with patch('methods.temperatures', return_value={'patched': [{'current': 99}]}):
        probe.call_methods()
    # Find the temperatures publish (errors-publish ist letzter Aufruf)
    calls = [c for c in probe.client.publish.call_args_list if c[0][0].endswith('/temperatures')]
    assert len(calls) == 1
    payload = json.loads(calls[0][0][1])
    assert payload['data']['result'] == {'patched': [{'current': 99}]}
