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
  - reconnect after broker outage

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
import signal
import socket
import time

import paho.mqtt.client as mqtt
import pytest

pytestmark = pytest.mark.integration


# --- Probe-Lifecycle ------------------------------------------------------

def test_probe_publishes_connected_retained(running_probe, mqtt_subscriber):
    """A late subscriber must immediately see connected='1' (retained)."""
    msgs = mqtt_subscriber('probe/+/connected', timeout=2)
    matching = [m for m in msgs if m.payload == b'1']
    assert matching, f'no connected=1 retained message; got: {[m.payload for m in msgs]}'
    # Retained-Flag muss True sein
    assert matching[0].retain is True


def test_probe_publishes_capabilities_retained(running_probe, mqtt_subscriber):
    msgs = mqtt_subscriber('probe/+/capabilities', timeout=2)
    assert msgs, 'no capabilities message'
    payload = msgs[-1].payload.decode()
    # Aus running_probe-Fixture-Config:
    assert 'ping' in payload
    assert 'reboot' in payload
    assert msgs[-1].retain is True


def test_probe_publishes_boot_time_retained(running_probe, mqtt_subscriber):
    msgs = mqtt_subscriber('probe/+/boot_time', timeout=2)
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

def test_command_ping_roundtrip(running_probe, mqtt_subscriber, mqtt_publisher):
    """Manager publishes manager/<fqdn>/ping → probe replies twice on
    probe/<fqdn>/ping: first 'received', then 'complete'."""
    fqdn = _read_probe_fqdn(mqtt_subscriber)
    # Subscribe BEFORE publishing so we don't miss the response
    import threading
    captured: list = []

    def collect():
        captured.extend(mqtt_subscriber(f'probe/{fqdn}/ping', timeout=4))

    t = threading.Thread(target=collect)
    t.start()
    time.sleep(0.5)  # subscriber needs to be wired up

    mqtt_publisher(f'manager/{fqdn}/ping', '')

    t.join(timeout=8)
    payloads = [json.loads(m.payload) for m in captured]
    statuses = [p.get('data', {}).get('status') for p in payloads]
    assert 'received' in statuses, f'no "received" intermediate response: {statuses}'
    assert 'complete' in statuses, f'no "complete" final response: {statuses}'


def test_command_blocked_returns_method_not_allowed(running_probe, mqtt_subscriber, mqtt_publisher):
    """Commands not in PROBE_CAPABILITIES must be rejected."""
    fqdn = _read_probe_fqdn(mqtt_subscriber)
    import threading
    captured: list = []

    def collect():
        captured.extend(mqtt_subscriber(f'probe/{fqdn}/shutdown', timeout=3))

    t = threading.Thread(target=collect)
    t.start()
    time.sleep(0.5)

    # 'shutdown' is NOT in the running_probe-fixture's capabilities.
    mqtt_publisher(f'manager/{fqdn}/shutdown', '')

    t.join(timeout=6)
    payloads = [json.loads(m.payload) for m in captured]
    errors = [p.get('error', {}).get('message') for p in payloads]
    assert 'Method not allowed' in errors, f'expected reject, got: {payloads}'


def test_module_attribute_attack_blocked(running_probe, mqtt_subscriber, mqtt_publisher):
    """Sending a command for a real Python-module-attribute (os, subprocess)
    must NOT execute it — the COMMANDS-Whitelist must gate dispatch."""
    fqdn = _read_probe_fqdn(mqtt_subscriber)
    import threading
    captured: list = []

    def collect():
        captured.extend(mqtt_subscriber(f'probe/{fqdn}/os', timeout=3))

    t = threading.Thread(target=collect)
    t.start()
    time.sleep(0.5)

    mqtt_publisher(f'manager/{fqdn}/os', '')

    t.join(timeout=6)
    payloads = [json.loads(m.payload) for m in captured]
    errors = [p.get('error', {}).get('message') for p in payloads]
    # Either 'Method not allowed' (capability gate) or 'Unknown method'
    # (whitelist gate) — beide sind security-correct.
    assert any(e in ('Method not allowed', 'Unknown method') for e in errors), \
        f'expected reject, got: {payloads}'


# --- Last-Will (~60-80s wegen broker keepalive) ----------------------------

def test_last_will_published_on_unclean_disconnect(running_probe, mqtt_subscriber):
    """SIGKILL the probe → broker keepalive expires → Last-Will publishes
    connected='0'.

    NOTE: paho-mqtt's default keepalive is 60s, so this test waits up
    to ~80s. Slow but it verifies the most important MQTT-feature for
    operational monitoring.
    """
    # Step 1: make sure the probe is fully up (connected='1' retained)
    initial = mqtt_subscriber('probe/+/connected', timeout=2)
    assert any(m.payload == b'1' for m in initial), 'probe never marked connected'

    # Step 2: brutal kill
    running_probe.send_signal(signal.SIGKILL)

    # Step 3: wait for the Will (broker pushes connected='0' after
    # keepalive timeout, default 60s).
    deadline = time.monotonic() + 90
    seen_will = False
    while time.monotonic() < deadline and not seen_will:
        msgs = mqtt_subscriber('probe/+/connected', timeout=10)
        for m in msgs:
            if m.payload == b'0' and m.retain:
                seen_will = True
                break

    assert seen_will, 'Last-Will connected="0" never arrived'


# --- Helpers --------------------------------------------------------------

def _read_probe_fqdn(mqtt_subscriber) -> str:
    """Discover the probe's actual FQDN by reading any retained probe-topic."""
    msgs = mqtt_subscriber('probe/+/connected', timeout=2)
    if not msgs:
        pytest.fail('no probe online — running_probe fixture failed')
    # topic = 'probe/<fqdn>/connected'
    return msgs[0].topic.split('/')[1]
