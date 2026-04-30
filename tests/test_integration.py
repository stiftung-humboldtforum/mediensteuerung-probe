"""End-to-end integration tests against a real MQTT broker.

These tests start the actual Probe-app as a subprocess and verify
its behavior over a real MQTT connection. They catch infrastructure-
level bugs that mock-based unit tests cannot:

  - actual paho-mqtt v2 callback wiring
  - retained-message semantics
  - QoS 1 acknowledgements
  - topic subscription patterns (noLocal, wildcards)
  - Last-Will publishing on unclean disconnect
  - command-response roundtrip via real broker
  - exponential backoff on broker outage

All tests in this file are auto-skipped if no broker is reachable —
see conftest.py:pytest_collection_modifyitems.

Run locally:
    docker compose -f docker-compose.test.yml up -d   # or: mosquitto -p 11883
    pytest -m integration
    docker compose -f docker-compose.test.yml down

Run in CI: see .github/workflows/test.yml integration job.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

pytestmark = pytest.mark.integration


# --- Probe-Lifecycle ------------------------------------------------------

def test_probe_publishes_connected_retained(running_probe, mqtt_subscriber):
    """A late subscriber must immediately see connected='1' (retained)."""
    msgs = mqtt_subscriber('probe/+/connected', timeout=5, min_count=1)
    matching = [m for m in msgs if m.payload == b'1']
    assert matching, f'no connected=1 retained message; got: {[m.payload for m in msgs]}'
    # Retained-Flag muss True sein
    assert matching[0].retain is True


def test_probe_publishes_capabilities_retained(running_probe, mqtt_subscriber):
    msgs = mqtt_subscriber('probe/+/capabilities', timeout=5, min_count=1)
    assert msgs, 'no capabilities message'
    payload = msgs[-1].payload.decode()
    # Aus running_probe-Fixture-Config:
    assert 'ping' in payload
    assert 'reboot' in payload
    assert msgs[-1].retain is True


def test_probe_publishes_boot_time_retained(running_probe, mqtt_subscriber):
    msgs = mqtt_subscriber('probe/+/boot_time', timeout=5, min_count=1)
    assert msgs, 'no boot_time message'
    parsed = json.loads(msgs[-1].payload)
    assert parsed['data']['status'] == 'complete'
    assert isinstance(parsed['data']['result'], (int, float))
    assert parsed['data']['result'] > 0
    assert msgs[-1].retain is True


# --- Periodic-Sensor-Cycle ------------------------------------------------

def test_probe_periodic_cycle_publishes_sensors(running_probe, mqtt_subscriber):
    """Probe.run() runs one cycle every 5s. Within ~8s we must see at
    least one ping, one uptime, and one errors topic."""
    msgs = mqtt_subscriber('probe/+/+', timeout=8)
    topics = {m.topic.split('/')[-1] for m in msgs}
    assert 'ping' in topics
    assert 'uptime' in topics
    assert 'errors' in topics
    assert 'boot_time' in topics  # retained


# --- Manager-Command Roundtrip --------------------------------------------

def _command_roundtrip(mqtt_subscriber, mqtt_publisher, fqdn, command,
                       expected_msgs=2, timeout=5):
    """Helper: subscribe to probe/<fqdn>/<command> in background thread,
    publish manager/<fqdn>/<command>, return collected payloads."""
    import threading
    captured: list = []

    def collect():
        captured.extend(mqtt_subscriber(
            f'probe/{fqdn}/{command}', timeout=timeout, min_count=expected_msgs))

    t = threading.Thread(target=collect)
    t.start()
    time.sleep(0.3)  # subscriber needs to be wired up
    mqtt_publisher(f'manager/{fqdn}/{command}', '')
    t.join(timeout=timeout + 2)
    return [json.loads(m.payload) for m in captured]


def test_command_ping_roundtrip(running_probe, mqtt_subscriber, mqtt_publisher):
    """Manager publishes manager/<fqdn>/ping → probe replies twice on
    probe/<fqdn>/ping: first 'received', then 'complete'."""
    fqdn = _read_probe_fqdn(mqtt_subscriber)
    payloads = _command_roundtrip(mqtt_subscriber, mqtt_publisher, fqdn, 'ping',
                                   expected_msgs=2)
    statuses = [p.get('data', {}).get('status') for p in payloads]
    assert 'received' in statuses, f'no "received" intermediate response: {statuses}'
    assert 'complete' in statuses, f'no "complete" final response: {statuses}'


def test_command_blocked_returns_method_not_allowed(running_probe, mqtt_subscriber, mqtt_publisher):
    """Commands not in PROBE_CAPABILITIES must be rejected via the
    Capability-Gate (S4)."""
    fqdn = _read_probe_fqdn(mqtt_subscriber)
    # 'shutdown' is NOT in the running_probe-fixture's capabilities.
    payloads = _command_roundtrip(mqtt_subscriber, mqtt_publisher, fqdn,
                                   'shutdown', expected_msgs=1)
    errors = [p.get('error', {}).get('message') for p in payloads]
    assert 'Method not allowed' in errors, f'expected Capability-Gate reject, got: {payloads}'


def test_capability_gate_blocks_module_attribute(running_probe, mqtt_subscriber, mqtt_publisher):
    """Capability-Gate (S4) layer: 'os' is not in capabilities → reject
    with 'Method not allowed' before even reaching the Whitelist-Gate."""
    fqdn = _read_probe_fqdn(mqtt_subscriber)
    payloads = _command_roundtrip(mqtt_subscriber, mqtt_publisher, fqdn,
                                   'os', expected_msgs=1)
    errors = [p.get('error', {}).get('message') for p in payloads]
    assert 'Method not allowed' in errors, f'expected Capability-Gate reject, got: {payloads}'


@pytest.mark.probe_config(capabilities='os,subprocess,call_method,getattr')
def test_whitelist_gate_blocks_module_attribute(running_probe, mqtt_subscriber, mqtt_publisher):
    """Whitelist-Gate (S1) layer: an attacker who has full access to
    PROBE_CAPABILITIES (= broker ACLs misconfigured) STILL cannot reach
    arbitrary module attributes. 'os' is in capabilities → Capability-
    Gate allows, but COMMANDS-dict doesn't have it → 'Unknown method'.

    This is the critical second-line-of-defense that test_capability_
    gate_blocks_module_attribute does NOT exercise.
    """
    fqdn = _read_probe_fqdn(mqtt_subscriber)
    for forbidden in ('os', 'subprocess', 'call_method', 'getattr'):
        payloads = _command_roundtrip(mqtt_subscriber, mqtt_publisher, fqdn,
                                       forbidden, expected_msgs=2)
        errors = [p.get('error', {}).get('message') for p in payloads]
        assert 'Unknown method' in errors, (
            f"{forbidden!r} reached the Whitelist-Gate but wasn't rejected "
            f'as "Unknown method"; got: {payloads}'
        )


# --- Last-Will (~60-80s wegen broker keepalive) ----------------------------

def test_last_will_published_on_unclean_disconnect(running_probe, mqtt_subscriber):
    """SIGKILL the probe → broker keepalive expires → Last-Will publishes
    connected='0'.

    Uses PROBE_MQTT_KEEPALIVE=5 (set by running_probe-fixture) so the
    broker detects the dead session after ~7-8s instead of paho-mqtt's
    default 60s+. Total test time: ~10-15s.
    """
    # Step 1: make sure the probe is fully up (connected='1' retained)
    initial = mqtt_subscriber('probe/+/connected', timeout=5, min_count=1)
    assert any(m.payload == b'1' for m in initial), 'probe never marked connected'

    # Step 2: brutal kill — proc.kill() ist plattform-portabel
    # (POSIX SIGKILL, Windows TerminateProcess), löst kein sauberes
    # disconnect aus → Broker triggert Last-Will nach Keepalive-Timeout.
    running_probe.kill()

    # Step 3: wait for the Will. With keepalive=5, the broker should
    # publish connected='0' within ~10-15s.
    deadline = time.monotonic() + 25
    seen_will = False
    while time.monotonic() < deadline and not seen_will:
        msgs = mqtt_subscriber('probe/+/connected', timeout=5, min_count=1)
        for m in msgs:
            if m.payload == b'0' and m.retain:
                seen_will = True
                break

    assert seen_will, 'Last-Will connected="0" never arrived'


# --- Reconnect / Backoff (no broker = no auto-skip relevant) --------------

def _find_free_port() -> int:
    """Pick a TCP port that's currently free (no listener)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_probe_exponential_backoff_on_dead_broker(tmp_path):
    """Probe gegen einen Port wo nichts laeuft → App.run muss
    exponentiell zurückfallende Reconnect-in-Ns Logs erzeugen
    (5 → 10 → 20 → ...).

    Independent vom Auto-Broker — wir starten den Probe-Subprocess
    selbst gegen einen unbenutzten Port.
    """
    dead_port = _find_free_port()
    config_file = tmp_path / 'userconfig.txt'
    config_file.write_text(
        'PROBE_METHODS="ping"\nPROBE_CAPABILITIES="ping"\n'
    )
    src_dir = Path(__file__).parent.parent / 'src'
    tests_dir = Path(__file__).parent
    # Subprocess-Coverage aktivieren (parallel=true in pyproject sorgt
    # dafuer dass mehrere subprocess-coverage-Runs nicht kollidieren).
    # Damit traegt dieser Test zur Coverage von app.py-Reconnect-Loop
    # bei, was sonst 35s laufendem Subprocess ohne Coverage-Wert waere.
    env = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join([str(tests_dir), str(src_dir)]),
        'COVERAGE_PROCESS_START': str(src_dir.parent / 'pyproject.toml'),
    }

    proc = subprocess.Popen(
        [
            sys.executable, str(src_dir / 'app.py'),
            '--config_file', str(config_file),
            '--mqtt_hostname', '127.0.0.1',
            '--mqtt_port', str(dead_port),
            '--no_tls',
            '--loglevel', 'INFO',
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    backoffs = []
    deadline = time.monotonic() + 35
    try:
        while len(backoffs) < 3 and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    pytest.fail(f'probe-process exited unexpectedly: rc={proc.returncode}')
                continue
            m = re.search(r'(?:Reconnect in|retrying in) (\d+)s', line)
            if m:
                backoffs.append(int(m.group(1)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    assert len(backoffs) >= 3, (
        f'expected at least 3 backoff messages, got: {backoffs}. '
        f'(BACKOFF_INITIAL=5s, so ≥3 should arrive within 35s)'
    )
    # Exponentielle Progression — jeder ≥ vorheriger
    assert backoffs[0] <= backoffs[1] <= backoffs[2], (
        f'backoff not monotonic: {backoffs}'
    )
    # Erster sollte nahe BACKOFF_INITIAL=5 sein (kann 5 oder 10 sein
    # je nachdem ob die erste connect-attempt selbst sleep'te)
    assert backoffs[0] in (5, 10), f'first backoff should be 5 or 10, got {backoffs[0]}'
    # Wachstum erkennbar (mind. einer ist > als der erste)
    assert max(backoffs) > backoffs[0], (
        f'no growth observed: {backoffs}'
    )


# --- TLS / mTLS (L-T3) ----------------------------------------------------

def test_probe_connects_via_tls(tls_broker, tmp_path):
    """Verifies the production-relevant `--ca_certificate / --certfile /
    --keyfile` codepath in App._setup. Uses a separate TLS-broker with
    require_certificate=true (= mTLS), so the probe MUST present its
    client cert or the connection is rejected.

    Catches: TLS context misconfiguration, cert-path file-not-found,
    paho-mqtt tls_set() API drift on future libmosquitto/openssl
    upgrades, certificate-validation regressions.
    """
    config_file = tmp_path / 'userconfig.txt'
    config_file.write_text(
        'PROBE_METHODS="ping"\nPROBE_CAPABILITIES="ping"\n'
    )
    src_dir = Path(__file__).parent.parent / 'src'
    tests_dir = Path(__file__).parent
    env = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join([str(tests_dir), str(src_dir)]),
        'COVERAGE_PROCESS_START': str(src_dir.parent / 'pyproject.toml'),
        'PROBE_MQTT_KEEPALIVE': '5',
    }

    proc = subprocess.Popen(
        [
            sys.executable, str(src_dir / 'app.py'),
            '--config_file', str(config_file),
            '--mqtt_hostname', tls_broker.host,
            '--mqtt_port', str(tls_broker.port),
            '--ca_certificate', tls_broker.ca,
            '--certfile', tls_broker.client_cert,
            '--keyfile', tls_broker.client_key,
            # bewusst KEIN --no_tls — das ist genau der Pfad den wir testen
            '--loglevel', 'WARNING',
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Subscribe-Client mit denselben certs gegen den TLS-Broker
    seen_connected = []

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe('probe/+/connected')

    def on_message(client, userdata, msg):
        if msg.payload == b'1':
            seen_connected.append(True)

    sub = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id='pytest-tls-sub',
    )
    sub.on_connect = on_connect
    sub.on_message = on_message
    sub.tls_set(
        ca_certs=tls_broker.ca,
        certfile=tls_broker.client_cert,
        keyfile=tls_broker.client_key,
    )

    try:
        sub.connect(tls_broker.host, tls_broker.port, 30)
        sub.loop_start()

        deadline = time.monotonic() + 15
        while not seen_connected and time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout = proc.stdout.read() if proc.stdout else ''
                pytest.fail(
                    f'probe-process exited unexpectedly (rc={proc.returncode}). '
                    f'Output:\n{stdout}'
                )
            time.sleep(0.2)

        assert seen_connected, (
            'probe never published connected="1" via TLS — TLS-handshake '
            'or mTLS-cert-presentation likely failed. '
            f'Probe output:\n{proc.stdout.read() if proc.stdout else ""}'
        )
    finally:
        sub.loop_stop()
        sub.disconnect()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# --- Helpers --------------------------------------------------------------

def _read_probe_fqdn(mqtt_subscriber) -> str:
    """Discover the probe's actual FQDN by reading any retained probe-topic."""
    msgs = mqtt_subscriber('probe/+/connected', timeout=5, min_count=1)
    if not msgs:
        pytest.fail('no probe online — running_probe fixture failed')
    # topic = 'probe/<fqdn>/connected'
    return msgs[0].topic.split('/')[1]
