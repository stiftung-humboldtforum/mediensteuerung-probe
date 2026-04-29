"""Shared pytest fixtures.

Most fixtures here are for the @pytest.mark.integration tests in
test_integration.py. They expect a real MQTT broker to be reachable
at PROBE_TEST_BROKER:PROBE_TEST_PORT (default 127.0.0.1:11883).

Spin one up locally via:
    docker compose -f docker-compose.test.yml up -d
or:
    mosquitto -p 11883 -v

CI uses a GitHub Actions service container, see .github/workflows/test.yml.
"""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest


BROKER_HOST = os.environ.get('PROBE_TEST_BROKER', '127.0.0.1')
BROKER_PORT = int(os.environ.get('PROBE_TEST_PORT', '11883'))
SRC_DIR = Path(__file__).parent.parent / 'src'


def _broker_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip @pytest.mark.integration tests if no broker is reachable."""
    if _broker_reachable(BROKER_HOST, BROKER_PORT):
        return
    skip = pytest.mark.skip(
        reason=f'integration: no MQTT broker at {BROKER_HOST}:{BROKER_PORT} '
               '(start mosquitto or docker compose -f docker-compose.test.yml up)'
    )
    for item in items:
        if 'integration' in item.keywords:
            item.add_marker(skip)


# --- MQTT helper fixture --------------------------------------------------

@pytest.fixture
def mqtt_subscriber():
    """Returns a function `subscribe(topic, timeout=10) -> list[(topic, payload)]`
    that records all messages on the topic-pattern until timeout, then disconnects.

    Used like:
        msgs = subscribe('probe/#', timeout=8)
        assert any(m.topic.endswith('/connected') and m.payload == b'1' for m in msgs)
    """
    clients = []

    def _subscribe(topic_pattern: str, timeout: float = 10.0) -> list:
        messages: list = []
        ready = []

        def on_connect(client, userdata, flags, reason_code, properties=None):
            client.subscribe(topic_pattern)
            ready.append(True)

        def on_message(client, userdata, msg):
            messages.append(msg)

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
        deadline = time.monotonic() + 5
        while not ready and time.monotonic() < deadline:
            time.sleep(0.05)

        time.sleep(timeout)
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
def running_probe(tmp_path):
    """Start the actual Probe-app as a subprocess against the broker.
    Yields a context with `proc` (Popen) and `fqdn` (string).

    Caller can read proc.stdout/.stderr for log assertions.
    """
    config_file = tmp_path / 'userconfig.txt'
    config_file.write_text(
        'PROBE_METHODS="ping,uptime,boot_time,easire"\n'
        'PROBE_CAPABILITIES="ping,reboot,mute,unmute"\n'
    )

    # Eindeutiger client_id pro Test damit parallele Runs nicht
    # kollidieren (gleicher FQDN + gleicher broker = zweite Connect
    # kickt erste raus).
    fqdn = f'integration-test-{os.getpid()}-{id(tmp_path)}'

    env = {
        **os.environ,
        'PYTHONPATH': str(SRC_DIR),
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

    # Sauberer Shutdown — SIGTERM, dann ggf. SIGKILL.
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
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
