"""Tests for the App layer (lifecycle, FQDN, banner logging, CLI)."""
import logging

import pytest
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from app import App, FqdnChanged, main


def _make_app(**overrides):
    defaults = dict(
        config={'PROBE_METHODS': 'ping', 'PROBE_CAPABILITIES': 'ping'},
        mqtt_hostname='localhost',
        mqtt_port=1883,
        ca_certificate=None,
        certfile=None,
        keyfile=None,
        no_tls=True,
        notify=None,
    )
    defaults.update(overrides)
    return App(**defaults)


# --- FQDN handling --------------------------------------------------------

def test_app_init_caches_fqdn():
    with patch('app.socket.getfqdn', return_value='cache.test'):
        app = _make_app()
    assert app.fqdn == 'cache.test'


def test_fqdn_property_does_not_call_dns():
    """The cached property must not re-resolve."""
    with patch('app.socket.getfqdn', return_value='initial.test'):
        app = _make_app()
    with patch('app.socket.getfqdn', return_value='changed.test') as mock_getfqdn:
        # Repeated reads — must not call DNS
        for _ in range(5):
            assert app.fqdn == 'initial.test'
        assert mock_getfqdn.call_count == 0


def test_refresh_fqdn_no_change_is_silent():
    with patch('app.socket.getfqdn', return_value='same.test'):
        app = _make_app()
    with patch('app.socket.getfqdn', return_value='same.test'):
        # No exception, no return value
        app._refresh_fqdn()
    assert app.fqdn == 'same.test'


def test_refresh_fqdn_raises_on_change():
    with patch('app.socket.getfqdn', return_value='old.test'):
        app = _make_app()
    with patch('app.socket.getfqdn', return_value='new.test'):
        with pytest.raises(FqdnChanged) as exc_info:
            app._refresh_fqdn()
    assert 'old.test' in str(exc_info.value)
    assert 'new.test' in str(exc_info.value)
    # And the cache is now updated to the new value
    assert app.fqdn == 'new.test'


# --- CLI / no_tls banner ---------------------------------------------------

def test_main_requires_certs_without_no_tls():
    runner = CliRunner()
    result = runner.invoke(main, [
        '--config_file', 'nonexistent.txt',
        '--mqtt_hostname', 'broker.local',
    ])
    assert result.exit_code != 0
    assert '--ca_certificate' in result.output


def test_no_tls_banner_localhost(caplog):
    """Localhost gets the friendly banner."""
    runner = CliRunner()
    with caplog.at_level(logging.WARNING):
        # We don't actually care about successful run; we just want to
        # exercise the banner path. Suppress App.run() side-effects.
        with patch('app.App.run'):
            runner.invoke(main, [
                '--config_file', '/dev/null',
                '--mqtt_hostname', '127.0.0.1',
                '--no_tls',
                '--loglevel', 'INFO',
            ])
    messages = [r.getMessage() for r in caplog.records]
    assert any('localhost broker' in m.lower() or 'local testing only' in m for m in messages)
    assert not any('NO AUTH, NO ENCRYPTION' in m for m in messages)


def test_no_tls_banner_remote(caplog):
    """Non-local broker gets the loud security warning."""
    runner = CliRunner()
    with caplog.at_level(logging.WARNING):
        with patch('app.App.run'):
            runner.invoke(main, [
                '--config_file', '/dev/null',
                '--mqtt_hostname', 'broker.production.example',
                '--no_tls',
                '--loglevel', 'INFO',
            ])
    messages = [r.getMessage() for r in caplog.records]
    assert any('NO AUTH, NO ENCRYPTION' in m for m in messages)
    assert any('Production deployments MUST use TLS' in m for m in messages)


# --- Backoff math ----------------------------------------------------------

def test_backoff_constants():
    """Sanity: initial < max, both positive."""
    assert 0 < App.BACKOFF_INITIAL < App.BACKOFF_MAX
    # Doubling stays within MAX
    capped = min(App.BACKOFF_INITIAL * 2 ** 10, App.BACKOFF_MAX)
    assert capped == App.BACKOFF_MAX


# --- sd_notify status sequence --------------------------------------------

def _make_notify_mock():
    """Returns a mock that mimics sd_notify.Notifier — enabled() always
    returns True so App treats it as 'systemd is watching'."""
    notify = MagicMock()
    notify.enabled.return_value = True
    return notify


def test_notify_status_sequence_on_setup_failure():
    """When _setup() raises (e.g. TLS-cert missing), App.run should NOT
    call notify.ready() (we never reached connected) — only the
    'Setup failed: <type>' status update.
    """
    notify = _make_notify_mock()
    app = _make_app(notify=notify)

    # Make _setup raise on first call so run-loop hits the exception
    # path immediately. Then break out of the infinite loop by raising
    # KeyboardInterrupt on the second iteration's sleep.
    setup_calls = []

    def _failing_setup():
        setup_calls.append(1)
        if len(setup_calls) >= 2:
            raise KeyboardInterrupt
        raise RuntimeError('TLS handshake failed')

    with patch.object(app, '_setup', side_effect=_failing_setup), \
         patch('app.time.sleep'):  # skip backoff sleep
        with pytest.raises(KeyboardInterrupt):
            app.run()

    # ready() should NOT have been called — we never reached connected
    notify.ready.assert_not_called()
    # status() called with 'Setup failed: RuntimeError: TLS handshake failed'
    status_calls = [c.args[0] for c in notify.status.call_args_list]
    assert any('Setup failed' in s and 'RuntimeError' in s for s in status_calls), \
        f'expected Setup-failed-status with RuntimeError; got: {status_calls}'


def test_notify_status_includes_exception_type_and_message():
    """notify.status('Failed: ...') must contain the exception class name
    AND a snippet of the message — Operator can debug from the systemd
    status line alone (S-R1 / N7)."""
    notify = _make_notify_mock()
    app = _make_app(notify=notify)

    raised = []

    def _failing_setup():
        raised.append(1)
        if len(raised) >= 2:
            raise KeyboardInterrupt
        raise ConnectionRefusedError('broker dead at 192.0.2.1:1883')

    with patch.object(app, '_setup', side_effect=_failing_setup), \
         patch('app.time.sleep'):
        with pytest.raises(KeyboardInterrupt):
            app.run()

    status_calls = [c.args[0] for c in notify.status.call_args_list]
    failed_status = next((s for s in status_calls if 'failed' in s.lower()), None)
    assert failed_status is not None
    assert 'ConnectionRefusedError' in failed_status
    assert 'broker dead' in failed_status


def test_notify_disabled_means_no_calls():
    """If sd_notify.Notifier reports enabled=False (= not under systemd),
    App.run must NOT make any notify-calls — they'd be no-ops anyway
    but cleaner not to."""
    notify = MagicMock()
    notify.enabled.return_value = False  # not under systemd
    app = _make_app(notify=notify)
    assert app.notify_enabled is False

    raised = []

    def _setup():
        raised.append(1)
        if len(raised) >= 2:
            raise KeyboardInterrupt
        raise RuntimeError('boom')

    with patch.object(app, '_setup', side_effect=_setup), \
         patch('app.time.sleep'):
        with pytest.raises(KeyboardInterrupt):
            app.run()

    notify.ready.assert_not_called()
    notify.status.assert_not_called()
    notify.notify.assert_not_called()


def test_notify_none_means_no_attribute_errors():
    """If sd_notify is unavailable (Linux-only dep), notify is None.
    App.run must handle that gracefully."""
    app = _make_app(notify=None)
    assert app.notify is None
    assert app.notify_enabled is False

    raised = []

    def _setup():
        raised.append(1)
        if len(raised) >= 2:
            raise KeyboardInterrupt
        raise RuntimeError('boom')

    with patch.object(app, '_setup', side_effect=_setup), \
         patch('app.time.sleep'):
        # Must not crash with AttributeError on None.notify(...)
        with pytest.raises(KeyboardInterrupt):
            app.run()
