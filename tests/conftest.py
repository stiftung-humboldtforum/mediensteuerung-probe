"""Shared pytest fixtures and pytest hooks.

The bulk of this module exists for the @pytest.mark.integration tests
in tests/test_integration.py. Unit tests don't touch any of it.

Sections:
  1. Configuration   — env-vars (PROBE_TEST_BROKER/PORT)
  2. Pytest hooks    — skip integration tests when no broker reachable
  3. MQTT fixtures   — mqtt_subscriber, mqtt_publisher
  4. Probe fixture   — running_probe spawns src/app.py as a subprocess

Integration tests need an externally running MQTT broker. Point
PROBE_TEST_BROKER / PROBE_TEST_PORT at it (default 127.0.0.1:11883).
If no broker is reachable, integration tests are skipped automatically.

  mosquitto -p 11883 -v &
  PROBE_TEST_BROKER=staging.mqtt PROBE_TEST_PORT=1883 pytest -m integration
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest


# =====================================================================
# 1. Configuration
# =====================================================================

BROKER_HOST = os.environ.get('PROBE_TEST_BROKER', '127.0.0.1')
BROKER_PORT = int(os.environ.get('PROBE_TEST_PORT', '11883'))
SRC_DIR = Path(__file__).parent.parent / 'src'


def _broker_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# =====================================================================
# 2. Pytest hooks
# =====================================================================

def pytest_collection_modifyitems(config, items):
    """Skip integration tests if no broker is reachable on
    BROKER_HOST:BROKER_PORT. Unit tests are unaffected."""
    has_integration = any('integration' in item.keywords for item in items)
    if not has_integration:
        return
    if _broker_reachable(BROKER_HOST, BROKER_PORT):
        return

    skip = pytest.mark.skip(
        reason=(
            f'integration: no MQTT broker at {BROKER_HOST}:{BROKER_PORT} '
            f'(start one and set PROBE_TEST_BROKER / PROBE_TEST_PORT)'
        )
    )
    for item in items:
        if 'integration' in item.keywords:
            item.add_marker(skip)


# =====================================================================
# 3. MQTT-Client fixtures (subscriber + publisher)
# =====================================================================

@pytest.fixture
def mqtt_subscriber():
    """Returns a function `subscribe(topic, timeout=10, min_count=0) -> list[Message]`
    that records all messages on the topic-pattern.

    - If min_count > 0: returns as soon as min_count messages have been
      collected (or timeout, whichever comes first).
    - If min_count == 0: collects for the full timeout (back-compat).
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

        # Wait for the subscribe-handshake before starting the timeout
        # clock — otherwise early retained messages are missed.
        handshake_timeout = max(5.0, min(timeout, 30.0))
        if not ready.wait(timeout=handshake_timeout):
            raise TimeoutError(
                f'subscribe handshake never completed within {handshake_timeout}s'
            )

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
    """One-shot publisher. Used like: publish('manager/host/ping', '')"""
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
# 4. Probe-Subprocess fixture
# =====================================================================

@pytest.fixture
def running_probe(request, tmp_path):
    """Start the actual Probe-app as a subprocess against the broker.
    Yields the Popen process.

    Tests can override PROBE_METHODS / PROBE_CAPABILITIES via
    @pytest.mark.probe_config(methods=..., capabilities=...).
    """
    marker = request.node.get_closest_marker('probe_config')
    if marker:
        methods_csv = marker.kwargs.get('methods', 'ping,uptime,boot_time,easire')
        caps_csv = marker.kwargs.get('capabilities', 'ping,reboot,mute,unmute,wake')
    else:
        methods_csv = 'ping,uptime,boot_time,easire'
        caps_csv = 'ping,reboot,mute,unmute,wake'

    config_file = tmp_path / 'userconfig.txt'
    config_file.write_text(
        f'PROBE_METHODS="{methods_csv}"\n'
        f'PROBE_CAPABILITIES="{caps_csv}"\n'
    )

    env = {
        **os.environ,
        'PYTHONPATH': str(SRC_DIR),
        # Short MQTT keepalive so the Last-Will test does not have to
        # wait the broker default (~90s) for the session to expire.
        'PROBE_MQTT_KEEPALIVE': os.environ.get('PROBE_MQTT_KEEPALIVE', '5'),
    }

    proc = subprocess.Popen(
        [
            sys.executable, str(SRC_DIR / 'app.py'),
            '--config_file', str(config_file),
            '--mqtt_hostname', BROKER_HOST,
            '--mqtt_port', str(BROKER_PORT),
            '--no_tls',
            '--loglevel', 'WARNING',
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if not _wait_for_probe_connected(timeout=10):
        proc.terminate()
        try:
            stdout, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
        pytest.fail(
            f'Probe did not appear in MQTT within 10s. '
            f'Probe output:\n{stdout}'
        )

    yield proc

    # Use communicate() so stdout is drained — proc.wait() blocks if the
    # subprocess buffer (~65 KB) fills, since the parent never reads it.
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def _wait_for_probe_connected(timeout: float = 10.0) -> bool:
    """Wait for any probe/+/connected = '1' within timeout. Event-driven,
    not polled."""
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
