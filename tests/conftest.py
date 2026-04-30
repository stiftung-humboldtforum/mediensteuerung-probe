"""Shared pytest fixtures.

Most fixtures here are for the @pytest.mark.integration tests in
test_integration.py. They expect a real MQTT broker to be reachable
at PROBE_TEST_BROKER:PROBE_TEST_PORT (default 127.0.0.1:11883).

THE BROKER IS AUTO-STARTED IF NEEDED. pytest_configure checks the
host:port and, if nothing is listening, spawns mosquitto in a
subprocess for the duration of the test session — provided
'mosquitto' is on $PATH. Cleanup happens via pytest_unconfigure.

So for the common case 'pytest -m integration' just works:
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


BROKER_HOST = os.environ.get('PROBE_TEST_BROKER', '127.0.0.1')
BROKER_PORT = int(os.environ.get('PROBE_TEST_PORT', '11883'))
SRC_DIR = Path(__file__).parent.parent / 'src'

# Set in pytest_configure if we spawned our own broker; cleaned up in
# pytest_unconfigure. Optional[dict] statt 'dict | None' weil das
# PEP-604-Form auf Python 3.9 als Type-Annotation am Modul-Level einen
# TypeError wirft (3.9 unterstuetzt es nur in Funktions-Bodies oder
# mit 'from __future__ import annotations').
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


def pytest_configure(config):
    """Spawn an ephemeral mosquitto if no broker is up. Idempotent —
    if a broker is already reachable, this is a no-op."""
    global _AUTO_BROKER
    _AUTO_BROKER = _start_auto_broker()


def pytest_unconfigure(config):
    """Clean up the auto-spawned broker (if any)."""
    _stop_auto_broker()


def pytest_collection_modifyitems(config, items):
    """Skip @pytest.mark.integration tests if no broker is reachable
    after pytest_configure (= auto-spawn was attempted but failed,
    typically because mosquitto isn't installed)."""
    if _broker_reachable(BROKER_HOST, BROKER_PORT):
        return
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


# --- MQTT helper fixture --------------------------------------------------

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


# --- Probe subprocess fixture ---------------------------------------------

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

    env = {
        **os.environ,
        'PYTHONPATH': str(SRC_DIR),
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
    """Poll any probe/+/connected = '1' within timeout."""
    seen = []

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe('probe/+/connected')

    def on_message(client, userdata, msg):
        if msg.payload == b'1':
            seen.append(True)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f'pytest-wait-{time.time_ns()}',
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_HOST, BROKER_PORT, 60)
    client.loop_start()

    deadline = time.monotonic() + timeout
    while not seen and time.monotonic() < deadline:
        time.sleep(0.1)

    client.loop_stop()
    client.disconnect()
    return bool(seen)
