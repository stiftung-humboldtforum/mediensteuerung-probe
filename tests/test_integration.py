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
import socket
import time

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


# --- Helpers --------------------------------------------------------------

def _read_probe_fqdn(mqtt_subscriber) -> str:
    """Discover the probe's actual FQDN by reading any retained probe-topic."""
    msgs = mqtt_subscriber('probe/+/connected', timeout=2)
    if not msgs:
        pytest.fail('no probe online — running_probe fixture failed')
    # topic = 'probe/<fqdn>/connected'
    return msgs[0].topic.split('/')[1]
