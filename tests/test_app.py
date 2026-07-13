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

def test_fqdn_property_does_not_call_dns():
    """The cached property must not re-resolve."""
    with patch('app.socket.getfqdn', return_value='initial.test'):
        app = _make_app()
    with patch('app.socket.getfqdn', return_value='changed.test') as mock_getfqdn:
        # Repeated reads — must not call DNS
        for _ in range(5):
            assert app.fqdn == 'initial.test'
        assert mock_getfqdn.call_count == 0


def test_refresh_fqdn_raises_on_change():
    with patch('app.socket.getfqdn', return_value='old.test'):
        app = _make_app()
    with patch('app.socket.getfqdn', return_value='new.test'):
        with pytest.raises(FqdnChanged) as exc_info:
            app._refresh_fqdn()
    assert 'old.test' in str(exc_info.value)
    assert 'new.test' in str(exc_info.value)
    # The exception carries the new FQDN as a typed attribute so the
    # run-loop can install it AFTER tearing down the old probe.
    assert exc_info.value.new_fqdn == 'new.test'
    # The cached _fqdn has NOT been mutated yet — old identity stays
    # valid until a successful _setup() with the new value.
    assert app.fqdn == 'old.test'


# --- Explicit identity override (--client_id / PROBE_CLIENT_ID) ------------

def test_explicit_client_id_overrides_getfqdn():
    """A valid explicit client_id pins identity and must NOT consult DNS."""
    with patch('app.socket.getfqdn', return_value='dns.fallback') as mock_getfqdn:
        app = _make_app(client_id='kosmo-cm-02.kosmo')
    assert app.fqdn == 'kosmo-cm-02.kosmo'
    assert app._identity_pinned is True
    assert mock_getfqdn.call_count == 0


def test_client_id_unset_falls_back_to_getfqdn():
    """No override -> historic behavior: identity from socket.getfqdn()."""
    with patch('app.socket.getfqdn', return_value='host.dotted.fqdn'):
        app = _make_app(client_id=None)
    assert app.fqdn == 'host.dotted.fqdn'
    assert app._identity_pinned is False


def test_invalid_client_id_warns_and_falls_back(caplog):
    """A metachar/whitespace-CONTAINING override warns loudly and falls
    back to getfqdn rather than pinning a broken identity. The warning is
    load-bearing: without it a misconfigured PROBE_CLIENT_ID silently
    degrades to the bare-hostname identity (device invisible to manager)."""
    for bad in ('bad/id', 'has space', 'wild+card', 'hash#tag'):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            with patch('app.socket.getfqdn', return_value='dns.fallback'):
                app = _make_app(client_id=bad)
        assert app.fqdn == 'dns.fallback'
        assert app._identity_pinned is False
        assert any('Ignoring invalid explicit MQTT identity' in r.getMessage()
                   for r in caplog.records), f'no warning for {bad!r}'


def test_empty_client_id_treated_as_unset_without_warning(caplog):
    """Empty / whitespace-only means 'not set': silent getfqdn fallback,
    deliberately NO warning (an absent override is not a misconfiguration)."""
    for empty in ('', '   '):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            with patch('app.socket.getfqdn', return_value='dns.fallback'):
                app = _make_app(client_id=empty)
        assert app.fqdn == 'dns.fallback'
        assert app._identity_pinned is False
        assert not any('MQTT identity' in r.getMessage() for r in caplog.records)


def test_refresh_fqdn_noop_when_client_id_pinned():
    """With a pinned identity, _refresh_fqdn must be inert — otherwise a
    differing socket.getfqdn() would raise FqdnChanged and the run-loop
    would overwrite the override (the exact multi-NIC bug this fixes)."""
    with patch('app.socket.getfqdn', return_value='old.dns'):
        app = _make_app(client_id='pinned.identity')
    with patch('app.socket.getfqdn', return_value='new.dns'):
        app._refresh_fqdn()  # must NOT raise
    assert app.fqdn == 'pinned.identity'


# --- CLI / no_tls banner ---------------------------------------------------

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


def test_no_tls_remote_refuses(caplog):
    """Non-local broker + --no_tls: loud warning AND refuse to start
    (exit 2), so the wide-open mode is never reached by accident."""
    runner = CliRunner()
    with caplog.at_level(logging.WARNING):
        with patch('app.App.run') as run:
            result = runner.invoke(main, [
                '--config_file', '/dev/null',
                '--mqtt_hostname', 'broker.production.example',
                '--no_tls',
                '--loglevel', 'INFO',
            ])
    messages = [r.getMessage() for r in caplog.records]
    assert any('NO AUTH, NO ENCRYPTION' in m for m in messages)
    assert any('Production deployments MUST use TLS' in m for m in messages)
    assert result.exit_code == 2
    run.assert_not_called()


def test_no_tls_remote_override_allows(caplog):
    """PROBE_ALLOW_INSECURE_REMOTE=1 overrides the refusal — main proceeds
    to App.run() (still logging the loud warning)."""
    runner = CliRunner()
    with caplog.at_level(logging.WARNING):
        with patch('app.App.run') as run:
            result = runner.invoke(main, [
                '--config_file', '/dev/null',
                '--mqtt_hostname', 'broker.production.example',
                '--no_tls',
                '--loglevel', 'INFO',
            ], env={'PROBE_ALLOW_INSECURE_REMOTE': '1'})
    messages = [r.getMessage() for r in caplog.records]
    assert any('NO AUTH, NO ENCRYPTION' in m for m in messages)
    assert result.exit_code == 0
    run.assert_called_once()


# --- Identity precedence at the CLI/config boundary ------------------------

def test_cli_client_id_beats_config_key(tmp_path):
    """--client_id (CLI) wins over PROBE_CLIENT_ID (config key)."""
    cfg = tmp_path / 'userconfig.txt'
    cfg.write_text('PROBE_METHODS="ping"\nPROBE_CAPABILITIES="ping"\n'
                   'PROBE_CLIENT_ID="from-config"\n')
    runner = CliRunner()
    with patch('app.App') as FakeApp:
        runner.invoke(main, [
            '--config_file', str(cfg),
            '--mqtt_hostname', '127.0.0.1', '--no_tls',
            '--client_id', 'from-cli',
        ])
    assert FakeApp.call_args.kwargs.get('client_id') == 'from-cli'


def test_config_client_id_used_when_no_cli_flag(tmp_path):
    """With no --client_id, PROBE_CLIENT_ID from the config file is used."""
    cfg = tmp_path / 'userconfig.txt'
    cfg.write_text('PROBE_METHODS="ping"\nPROBE_CAPABILITIES="ping"\n'
                   'PROBE_CLIENT_ID="from-config"\n')
    runner = CliRunner()
    with patch('app.App') as FakeApp:
        runner.invoke(main, [
            '--config_file', str(cfg),
            '--mqtt_hostname', '127.0.0.1', '--no_tls',
        ])
    assert FakeApp.call_args.kwargs.get('client_id') == 'from-config'


def test_unusable_cli_client_id_falls_through_to_config(tmp_path):
    """An unusable CLI value (empty or metachar-invalid) must NOT shadow a
    valid PROBE_CLIENT_ID — the precedence chain is a usability fallback,
    not a raw is-None check. Regression: `--client_id ""` used to silently
    demote the config pin to getfqdn()."""
    cfg = tmp_path / 'userconfig.txt'
    cfg.write_text('PROBE_METHODS="ping"\nPROBE_CAPABILITIES="ping"\n'
                   'PROBE_CLIENT_ID="from-config"\n')
    runner = CliRunner()
    for unusable in ('', '   ', 'bad/id'):
        with patch('app.App') as FakeApp:
            runner.invoke(main, [
                '--config_file', str(cfg),
                '--mqtt_hostname', '127.0.0.1', '--no_tls',
                '--client_id', unusable,
            ])
        assert FakeApp.call_args.kwargs.get('client_id') == 'from-config', \
            f'CLI {unusable!r} shadowed the config identity'


def test_no_identity_anywhere_passes_none(tmp_path):
    """No CLI flag and no config key -> client_id=None (getfqdn path)."""
    cfg = tmp_path / 'userconfig.txt'
    cfg.write_text('PROBE_METHODS="ping"\nPROBE_CAPABILITIES="ping"\n')
    runner = CliRunner()
    with patch('app.App') as FakeApp:
        runner.invoke(main, [
            '--config_file', str(cfg),
            '--mqtt_hostname', '127.0.0.1', '--no_tls',
        ])
    assert FakeApp.call_args.kwargs.get('client_id') is None


# --- Lifecycle / cleanup --------------------------------------------------

def test_app_stop_is_idempotent_without_setup():
    """stop() must not raise when called before _setup() ever ran —
    e.g. when run() exits before the first cycle."""
    app = _make_app()
    app.stop()  # no mqtt_client, no probe yet — must be a no-op


def test_app_stop_calls_loop_stop_after_loop_start():
    """Reconnect-Cycle: every loop_start() needs a loop_stop() in
    teardown — otherwise the paho network thread leaks."""
    app = _make_app()
    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    app.mqtt_client = fake_client
    app.stop()
    fake_client.disconnect.assert_called_once()
    fake_client.loop_stop.assert_called_once()


def test_app_stop_does_not_publish_offline():
    """Graceful stop must NOT publish `connected` — Manager's `on_connected`
    handler ignores the payload and would mark the device ONLINE on any
    arrival of the topic. Manager detects unclean disconnects via
    ping-timeout (ping_max_interval ~30s) instead."""
    app = _make_app()
    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    app.mqtt_client = fake_client
    app.stop()
    pub_calls = [c for c in fake_client.publish.call_args_list
                 if c.args and c.args[0].endswith('/connected')]
    assert pub_calls == []


def test_app_stop_loop_stop_called_even_if_not_connected():
    """loop_start() runs after connect() — but if connect() raised
    *after* loop_start(), is_connected may be False yet the thread
    exists. loop_stop() must run regardless."""
    app = _make_app()
    fake_client = MagicMock()
    fake_client.is_connected.return_value = False
    app.mqtt_client = fake_client
    app.stop()
    fake_client.disconnect.assert_not_called()
    fake_client.loop_stop.assert_called_once()


def test_backoff_sleep_pings_notify_within_watchdog_window():
    """During long backoff sleeps the sd_notify-Watchdog must keep
    receiving pings — otherwise systemd would mark the unit stalled
    and restart it mid-backoff."""
    notify = _make_notify_mock()
    app = _make_app(notify=notify)
    sleeps: list = []
    t = [0.0]

    def _advance(s):
        sleeps.append(s)
        t[0] += s

    with patch('app.time.monotonic', side_effect=lambda: t[0]), \
         patch('app.time.sleep', side_effect=_advance):
        app._backoff_sleep(45)

    # 45s with BACKOFF_NOTIFY_INTERVAL=15 → 3 chunks → 3 notify-pings
    assert sum(sleeps) >= 45
    assert notify.notify.call_count >= 3


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
    status line alone."""
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
