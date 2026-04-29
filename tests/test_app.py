"""Tests for the App layer (lifecycle, FQDN, banner logging, CLI)."""
import logging

import pytest
from click.testing import CliRunner
from unittest.mock import patch

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
