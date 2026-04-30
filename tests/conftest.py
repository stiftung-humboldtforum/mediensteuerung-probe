"""Shared pytest fixtures and pytest hooks.

The bulk of this module exists for the @pytest.mark.integration tests
in tests/test_integration.py. Unit tests don't touch any of it.

== Sections ==
    1. Configuration   — env-vars (BROKER_HOST/PORT), worker-port-suffix
    2. Auto-Broker     — spawn/cleanup of an ephemeral mosquitto if
                         no external broker is reachable
    3. Pytest hooks    — pytest_unconfigure, pytest_collection_modifyitems
                         (the lazy auto-broker spawn lives here)
    4. MQTT fixtures   — mqtt_subscriber (event-driven, min_count),
                         mqtt_publisher
    5. Probe fixture   — running_probe: spawns src/app.py as a
                         subprocess, with subprocess-coverage hooked
                         and PROBE_MQTT_KEEPALIVE=5 for fast Last-Will
    6. TLS-Broker      — separate mosquitto with self-signed CA +
                         require_certificate=true for the mTLS
                         integration test (tests/_certs.py)

== Auto-Broker behavior ==
The auto-broker is spawned LAZILY in pytest_collection_modifyitems —
only when the collected test set actually contains integration tests.
For 'pytest tests/test_misc.py' (unit-only) no broker is started.

So 'pytest -m integration' just works:
  - CI: apt-installed mosquitto on PATH → auto-spawned
  - macOS dev: brew install mosquitto → auto-spawned
  - User-supplied broker (e.g. staging): set PROBE_TEST_BROKER, the
    auto-start logic detects it's already reachable and skips spawn

Manual override paths (still supported):
  docker compose -f docker-compose.test.yml up -d   # external container
  mosquitto -p 11883 -v &                           # external process
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
import pytest


# =====================================================================
# 1. Configuration
# =====================================================================

BROKER_HOST = os.environ.get('PROBE_TEST_BROKER', '127.0.0.1')

def _broker_port() -> int:
    """Pick a unique port per pytest-xdist worker so parallel test
    sessions don't collide on the same auto-broker. Falls back to the
    user-supplied PROBE_TEST_PORT or 11883 when no xdist is active."""
    base = int(os.environ.get('PROBE_TEST_PORT', '11883'))
    worker = os.environ.get('PYTEST_XDIST_WORKER', '')
    if worker.startswith('gw'):
        try:
            return base + int(worker[2:])
        except ValueError:
            pass
    return base

BROKER_PORT = _broker_port()
SRC_DIR = Path(__file__).parent.parent / 'src'

# =====================================================================
# 2. Auto-Broker — ephemeral mosquitto spawned for the test session
# =====================================================================

# Set in pytest_collection_modifyitems if we spawned our own broker;
# cleaned up in pytest_unconfigure. Optional[dict] statt 'dict | None'
# weil das PEP-604-Form auf Python 3.9 als Type-Annotation am Modul-
# Level einen TypeError wirft (3.9 unterstuetzt es nur in Funktions-
# Bodies oder mit 'from __future__ import annotations').
_AUTO_BROKER: Optional[dict] = None


def _broker_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_auto_broker():
    """If no broker is listening on BROKER_HOST:BROKER_PORT and
    mosquitto is on PATH, spawn one and return its handle dict.
    Returns None if a broker is already up or mosquitto is missing."""
    if _broker_reachable(BROKER_HOST, BROKER_PORT):
        return None  # external broker — leave it alone

    mosquitto_bin = shutil.which('mosquitto')
    if not mosquitto_bin:
        return None  # nothing to spawn — integration tests will skip

    config_dir = tempfile.mkdtemp(prefix='humboldt-test-broker-')
    config_path = os.path.join(config_dir, 'broker.conf')
    log_path = os.path.join(config_dir, 'mosquitto.log')
    with open(config_path, 'w') as f:
        f.write(
            f'listener {BROKER_PORT} {BROKER_HOST}\n'
            'allow_anonymous true\n'
            'persistence false\n'
        )

    log = open(log_path, 'w')
    proc = subprocess.Popen(
        [mosquitto_bin, '-c', config_path],
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    # Wait for the broker to accept connections — up to 6 seconds.
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if _broker_reachable(BROKER_HOST, BROKER_PORT):
            print(
                f'\n[conftest] auto-started mosquitto pid={proc.pid} '
                f'on {BROKER_HOST}:{BROKER_PORT} (log: {log_path})',
                file=sys.stderr,
            )
            return {'proc': proc, 'config_dir': config_dir, 'log': log}
        if proc.poll() is not None:
            # Mosquitto crashed early — surface the log
            log.close()
            with open(log_path) as f:
                log_content = f.read()
            shutil.rmtree(config_dir, ignore_errors=True)
            raise RuntimeError(
                f'auto-mosquitto exited with code {proc.returncode}:\n{log_content}'
            )
        time.sleep(0.1)

    # Timeout — kill and raise
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    shutil.rmtree(config_dir, ignore_errors=True)
    raise RuntimeError(
        f'auto-mosquitto did not become reachable on '
        f'{BROKER_HOST}:{BROKER_PORT} within 6s'
    )


def _stop_auto_broker():
    global _AUTO_BROKER
    if _AUTO_BROKER is None:
        return
    proc = _AUTO_BROKER['proc']
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    _AUTO_BROKER['log'].close()
    shutil.rmtree(_AUTO_BROKER['config_dir'], ignore_errors=True)
    _AUTO_BROKER = None


# =====================================================================
# 3. Pytest hooks
# =====================================================================

def pytest_unconfigure(config):
    """Clean up the auto-spawned broker (if any)."""
    _stop_auto_broker()


def pytest_collection_modifyitems(config, items):
    """Spawn the auto-broker LAZILY — only if the collected test set
    actually contains integration tests. Saves the 1-2s broker-startup
    overhead for unit-only test runs (e.g. `pytest tests/test_misc.py`).

    If integration tests are present and no broker is reachable, try to
    spawn one. If that also fails (mosquitto not on PATH), skip all
    integration tests with a clear message.
    """
    global _AUTO_BROKER

    has_integration = any('integration' in item.keywords for item in items)
    if not has_integration:
        return  # no integration tests selected → no broker needed

    if _broker_reachable(BROKER_HOST, BROKER_PORT):
        return  # external broker available → use it

    # Try to spawn one. _start_auto_broker returns None if mosquitto
    # isn't on PATH OR if a broker became reachable in the meantime.
    _AUTO_BROKER = _start_auto_broker()
    if _broker_reachable(BROKER_HOST, BROKER_PORT):
        return  # spawn succeeded

    # Last resort: no broker, no mosquitto → skip integration tests.
    skip = pytest.mark.skip(
        reason=(
            f'integration: no MQTT broker at {BROKER_HOST}:{BROKER_PORT} '
            f'and mosquitto is not on $PATH (install it or set '
            f'PROBE_TEST_BROKER to point at an existing broker)'
        )
    )
    for item in items:
        if 'integration' in item.keywords:
            item.add_marker(skip)


# =====================================================================
# 4. MQTT-Client fixtures (subscriber + publisher)
# =====================================================================

@pytest.fixture
def mqtt_subscriber():
    """Returns a function `subscribe(topic, timeout=10, min_count=0) -> list[Message]`
    that records all messages on the topic-pattern.

    - If min_count > 0: returns as soon as min_count messages have been
      collected (or timeout, whichever comes first).
    - If min_count == 0: collects for the full timeout (back-compat).

    Used like:
        msgs = subscribe('probe/+/connected', timeout=5, min_count=1)
        assert msgs[0].payload == b'1'
    """
    from threading import Event
    clients = []

    def _subscribe(topic_pattern: str, timeout: float = 10.0,
                   min_count: int = 0) -> list:
        messages: list = []
        ready = Event()
        enough = Event()

        def on_connect(client, userdata, flags, reason_code, properties=None):
            client.subscribe(topic_pattern)
            ready.set()

        def on_message(client, userdata, msg):
            messages.append(msg)
            if min_count and len(messages) >= min_count:
                enough.set()

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f'pytest-sub-{id(messages)}',
        )
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(BROKER_HOST, BROKER_PORT, 60)
        client.loop_start()
        clients.append(client)

        # Warte auf den Subscribe-Handshake bevor wir die Timeout-Uhr
        # starten — sonst würden frühe retained messages verpasst.
        if not ready.wait(timeout=5):
            raise TimeoutError('subscribe handshake never completed')

        if min_count:
            enough.wait(timeout=timeout)  # event-driven: returns ASAP
        else:
            time.sleep(timeout)  # back-compat: collect-for-timeout
        return list(messages)

    yield _subscribe

    for c in clients:
        c.loop_stop()
        c.disconnect()


@pytest.fixture
def mqtt_publisher():
    """One-shot publisher. Used like:
        publish('manager/host/ping', '')
    """
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id='pytest-pub',
    )
    client.connect(BROKER_HOST, BROKER_PORT, 60)
    client.loop_start()

    def _publish(topic: str, payload: str = '', qos: int = 0):
        info = client.publish(topic, payload, qos=qos)
        info.wait_for_publish(timeout=5)

    yield _publish

    client.loop_stop()
    client.disconnect()


# =====================================================================
# 5. Probe-Subprocess fixture
# =====================================================================

@pytest.fixture
def running_probe(request, tmp_path):
    """Start the actual Probe-app as a subprocess against the broker.
    Yields the Popen process.

    Tests can override PROBE_METHODS / PROBE_CAPABILITIES via
    @pytest.mark.probe_config(methods=..., capabilities=...).

    Example:
        @pytest.mark.probe_config(capabilities='os,subprocess,call_method')
        def test_whitelist_gate(running_probe, ...):
            # 'os' is in capabilities → Capability-Gate lets it through,
            # but COMMANDS-Whitelist rejects it as 'Unknown method'
    """
    marker = request.node.get_closest_marker('probe_config')
    if marker:
        methods_csv = marker.kwargs.get('methods', 'ping,uptime,boot_time,easire')
        caps_csv = marker.kwargs.get('capabilities', 'ping,reboot,mute,unmute')
    else:
        methods_csv = 'ping,uptime,boot_time,easire'
        caps_csv = 'ping,reboot,mute,unmute'

    config_file = tmp_path / 'userconfig.txt'
    config_file.write_text(
        f'PROBE_METHODS="{methods_csv}"\n'
        f'PROBE_CAPABILITIES="{caps_csv}"\n'
    )

    # Eindeutiger client_id pro Test damit parallele Runs nicht
    # kollidieren (gleicher FQDN + gleicher broker = zweite Connect
    # kickt erste raus).
    fqdn = f'integration-test-{os.getpid()}-{id(tmp_path)}'

    # tests/-Verzeichnis auf PYTHONPATH damit der Subprocess das
    # tests/sitecustomize.py findet, das den COVERAGE_PROCESS_START-
    # Hook installiert. SRC_DIR fuer den eigentlichen Probe-Code.
    tests_dir = Path(__file__).parent
    env = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join([str(tests_dir), str(SRC_DIR)]),
        # Subprocess-Coverage: zeigt sitecustomize an pyproject's
        # [tool.coverage.run]-Konfig. parallel=true sorgt dafuer dass
        # die Daten in .coverage.<pid> Fragmenten landen, die pytest-cov
        # via combine zusammenfuehrt.
        'COVERAGE_PROCESS_START': str(Path(__file__).parent.parent / 'pyproject.toml'),
        # Kurzes MQTT-Keepalive (5s) damit der Last-Will-Test nicht
        # 90s warten muss bis der Broker die Session als tot deklariert.
        'PROBE_MQTT_KEEPALIVE': os.environ.get('PROBE_MQTT_KEEPALIVE', '5'),
        # FQDN wird via socket.getfqdn() gelesen — unter macOS/Linux
        # kein env-Override. Tests müssen mit dem real-FQDN leben oder
        # den Probe-Output auf dem Topic-Pfad probe/+/... matchen.
    }

    proc = subprocess.Popen(
        [
            sys.executable, str(SRC_DIR / 'app.py'),
            '--config_file', str(config_file),
            '--mqtt_hostname', BROKER_HOST,
            '--mqtt_port', str(BROKER_PORT),
            '--no_tls',
            '--loglevel', 'WARNING',  # weniger noise im Test-Output
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Warte bis die Probe wirklich connected ist — wir checken via
    # MQTT (das Subscribe greift retained=True topics sofort).
    if not _wait_for_probe_connected(timeout=10):
        proc.terminate()
        try:
            stdout = proc.stdout.read() if proc.stdout else ''
        except Exception:
            stdout = ''
        proc.wait(timeout=5)
        pytest.fail(
            f'Probe did not appear in MQTT within 10s. '
            f'Probe output:\n{stdout}'
        )

    yield proc

    # Sauberer Shutdown via terminate() — auf POSIX SIGTERM,
    # auf Windows TerminateProcess. Plattform-portabel.
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()  # POSIX SIGKILL / Windows TerminateProcess (force)
        proc.wait()


def _wait_for_probe_connected(timeout: float = 10.0) -> bool:
    """Wait for any probe/+/connected = '1' within timeout. Returns
    immediately as soon as the message arrives (event-driven, no
    polling)."""
    from threading import Event
    seen = Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe('probe/+/connected')

    def on_message(client, userdata, msg):
        if msg.payload == b'1':
            seen.set()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f'pytest-wait-{time.time_ns()}',
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_HOST, BROKER_PORT, 60)
    client.loop_start()
    try:
        return seen.wait(timeout=timeout)
    finally:
        client.loop_stop()
        client.disconnect()


# =====================================================================
# 6. TLS-Broker (separate mosquitto with self-signed CA + mTLS)
# =====================================================================

@pytest.fixture(scope='session')
def tls_broker(tmp_path_factory):
    """Spawns a separate TLS-enabled mosquitto on a free port for the
    duration of the test session. Generates self-signed CA + server +
    client certs via cryptography lib (no openssl-CLI dependency).

    Yields a SimpleNamespace with .host, .port, .ca, .client_cert,
    .client_key paths. Caller can use these as App-CLI args and as
    paho-mqtt tls_set() inputs.

    Skips the dependent test if mosquitto is not on PATH.
    """
    from types import SimpleNamespace
    sys.path.insert(0, str(Path(__file__).parent))
    import _certs  # type: ignore[import-not-found]

    mosquitto_bin = shutil.which('mosquitto')
    if not mosquitto_bin:
        pytest.skip('mosquitto not on PATH — TLS-integration test requires it')

    cert_dir = tmp_path_factory.mktemp('tls-certs')
    paths = _certs.make_ca_and_certs(cert_dir)

    # Pick a free port distinct from BROKER_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((BROKER_HOST, 0))
        tls_port = s.getsockname()[1]

    # Mosquitto-config mit TLS + require_certificate (mTLS).
    config_dir = tempfile.mkdtemp(prefix='humboldt-tls-broker-')
    config_path = os.path.join(config_dir, 'broker.conf')
    log_path = os.path.join(config_dir, 'mosquitto.log')
    with open(config_path, 'w') as f:
        f.write(
            f'listener {tls_port} {BROKER_HOST}\n'
            f'cafile {paths["ca"]}\n'
            f'certfile {paths["server_cert"]}\n'
            f'keyfile {paths["server_key"]}\n'
            'require_certificate true\n'
            'allow_anonymous true\n'
            'persistence false\n'
        )

    log = open(log_path, 'w')
    proc = subprocess.Popen(
        [mosquitto_bin, '-c', config_path],
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    # Wait for the TLS broker to accept connections (TCP-bound = ready)
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if _broker_reachable(BROKER_HOST, tls_port):
            break
        if proc.poll() is not None:
            log.close()
            with open(log_path) as f:
                log_content = f.read()
            shutil.rmtree(config_dir, ignore_errors=True)
            pytest.fail(
                f'TLS-mosquitto exited rc={proc.returncode}:\n{log_content}'
            )
        time.sleep(0.1)
    else:
        proc.terminate()
        log.close()
        shutil.rmtree(config_dir, ignore_errors=True)
        pytest.fail(f'TLS-mosquitto not reachable on {BROKER_HOST}:{tls_port}')

    handle = SimpleNamespace(
        host=BROKER_HOST,
        port=tls_port,
        ca=str(paths['ca']),
        client_cert=str(paths['client_cert']),
        client_key=str(paths['client_key']),
    )

    yield handle

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log.close()
    shutil.rmtree(config_dir, ignore_errors=True)
