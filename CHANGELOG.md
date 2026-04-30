# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For the historical migration from `avorus-probe` see
[`docs/migration-from-avorus.md`](docs/migration-from-avorus.md).

## [Unreleased]

## [0.2.0] — 2026-04-30

The first release after the `avorus-probe` fork. Major hardening
across security, robustness, cross-platform support, and testing.
102 tests, 85% coverage, full CI on Linux/macOS/Windows.

### Security
- **Command-Whitelist** (`COMMANDS` dict) replaces reflection-based
  dispatch in `Probe.on_message` — manager can no longer reach
  imported module attributes (`os`, `subprocess`, ...).
- **Sensor / Command separation** via explicit `SENSORS` / `COMMANDS`
  whitelists prevents `shutdown` accidentally landing in
  `PROBE_METHODS` and being polled every 5s.
- **Fail-closed** default for missing `PROBE_CAPABILITIES` (was
  permissive `wake,shutdown,reboot`).
- **Payload validation**: `args`/`kwargs` `isinstance`-checks in
  `parse_payload` block malicious JSON.
- **Topic-parsing** safe against malformed manager topics
  (no `IndexError`).
- **Loud `--no_tls` banner** + sd_notify `'UNSAFE: --no_tls active'`
  status when used against non-localhost broker.
- **TLS/mTLS path** verified by integration test (self-signed CA,
  `require_certificate=true`).

### Added
- **Cross-platform support**: Linux (`_linux.py`), Windows (`_win32.py`),
  graceful `_stub.py` fallback for macOS dev. Unified dispatch in
  `methods/__init__.py`.
- **MQTT Last-Will** (`probe/<fqdn>/connected = "0"`) so broker
  informs manager on unclean disconnects.
- **Retained** `connected` / `capabilities` / `boot_time` so newly
  subscribing managers see current state immediately.
- **QoS 1** for command responses and critical state topics.
- Sensor values for `display`, `easire`, `mpv_file_pos_sec` are now
  actually published (previously only ok/error in the errors-dict).
- **`sd_notify` watchdog** gated on Probe-Thread heartbeat — stalled
  probes get auto-restarted by systemd instead of the watchdog firing
  into a dead App-loop.
- **Exponential backoff** (5s → 60s) on reconnect failures.
- **`subprocess` timeouts** (3-5s) on all external tool calls (wpctl,
  xrandr, mpv_control) to prevent Probe-Thread from hanging.
- **`App.fqdn` cached** + explicit `_refresh_fqdn()` raising
  `FqdnChanged` (was a Property with side-effect + DNS-Lookup per
  read).
- **`connected_event`** (threading.Event) replaces blind
  `time.sleep(3)` after MQTT connect — no more reconnect-flap on slow
  brokers.
- **`_stop_event`** for interruptible `Probe.run` sleeps (was
  `time.sleep(5)` blocking the shutdown by up to 5s).
- **`notify.status`** now contains exception type + message instead
  of generic `Failed.`. Setup-failures also get a status update.
- **Heartbeat bump** in `on_connect` so the watchdog sees a live
  signal even before the first `call_methods()` cycle completes.
- Reference [`systemd/humboldt-probe.service`](systemd/humboldt-probe.service)
  unit file (Type=notify, WatchdogSec=30s).
- [`scripts/install-windows.ps1`](scripts/install-windows.ps1) —
  idempotent NSSM service setup.
- [`scripts/hardware-test-{linux.sh,windows.ps1}`](scripts/) — direct
  sensor invocation with PASS/FAIL reporting, runnable via SSH/RDP
  after deploy.
- [`scripts/smoke-test.sh`](scripts/smoke-test.sh) — pre-/post-deploy
  MQTT verification.
- [`scripts/mpv_control.example.sh`](scripts/mpv_control.example.sh)
  — reference impl for the optional `mpv_file_pos_sec` helper.
- [`pyproject.toml`](pyproject.toml) for project metadata + tool
  config (pytest, coverage).
- [`requirements.lock.txt`](requirements.lock.txt) via `pip-compile`
  for reproducible builds.
- [`Dockerfile.linux-test`](Dockerfile.linux-test) +
  [`docker-compose.linux-test.yml`](docker-compose.linux-test.yml) —
  Linux-codepath verification on macOS-Dev without VM.
- LICENSE placeholder (Stiftung action required).

### Changed
- **`paho-mqtt`** upgraded from 1.6.1 to 2.x with
  `CallbackAPIVersion.VERSION2` callbacks.
- `os.system()` → `subprocess.run([...])` for shutdown/reboot with
  proper error reporting (rc + stderr).
- All `shell=True` removed from sensor commands; were static strings,
  no injection risk, but cleaner without.
- `easire()` Linux unified with Windows on `psutil.process_iter`
  (was `ps ax | grep`).
- Windows `temperatures()` / `fans()` schema unified with Linux:
  multiple sensors per hardware (was first-only).
- `sys.coinit_flags = 0` moved from `methods/__init__.py` to top of
  `app.py` (had to happen before any other import touched COM).
- Loglevel default `INFO` (was `CRITICAL`); ISO-timestamps in log
  format.
- `errors`-topic published once per cycle instead of per-method
  (eliminated redundant publishes).
- `error_response` renamed to `status_response` (reflects that it
  carries data, not error).
- Platform-specific code split out of `methods/sensors.py` (deleted)
  into `_linux.py` / `_win32.py` / `_stub.py`.
- `requirements.txt` consistently pinned with major-caps; `pytest`
  moved to `requirements-dev.txt`.
- `userconfig.example.txt` now includes `mpv_file_pos_sec` and inline
  doc.
- `CHANGELOG_linux.md` renamed to
  [`docs/migration-from-avorus.md`](docs/migration-from-avorus.md)
  with note that it's a one-shot migration log.
- LHM/HidSharp DLLs (lib/win32/) now committed with `LICENSE.txt` for
  the bundled binaries (.gitignore had previously blocked them).
- MQTT-Keepalive configurable via `PROBE_MQTT_KEEPALIVE` env-var
  (Tests use 5s for fast Last-Will-Trigger; production keeps 60s).

### Fixed
- `try/except Exception` in `Probe.__init__` for missing config keys
  narrowed to `KeyError`.
- Unknown periodic methods log a warning instead of being silently
  dropped.
- `check_*` methods set `errors[name] = 'error'` even when the
  underlying sensor raises (previously stale-state).
- Capabilities re-publish in `call_methods` removed (was QoS 0 / not
  retained — pure traffic noise; on_connect already publishes
  retained).
- **`get_config`** ignores comment lines (`shlex.split` would
  otherwise tokenize `# Periodic Sensor-Polls` into garbage keys).
- **`App.run` STALL_TOLERANCE=2** for the initial Probe-Thread/App-
  Thread Start-Race.
- **`signal.SIGKILL` → `proc.kill()`** for Windows portability
  (POSIX SIGKILL, Windows TerminateProcess).
- **Python 3.9 compat**: `Optional[dict]` instead of `dict | None` at
  module level.
- **Windows-PowerShell CI compat**: `shell: bash` for cross-platform
  workflow steps.
- **`.pre-commit-config.yaml`** ruff-args YAML-Syntax (Block-style
  instead of inline-list with commas).

### Testing
- **102 tests** in 3 layers:
  - 91 unit (`test_misc` 18, `test_methods` 20, `test_methods_win32` 11,
    `test_probe` 30, `test_app` 12) — pure logic with mocks
  - 11 integration (`test_integration` — real Mosquitto roundtrips,
    incl. TLS-mTLS, Last-Will, Reconnect-Backoff)
  - CI lint: pre-commit + shellcheck + PSScriptAnalyzer
- **85% coverage** including subprocess-tracking via
  `tests/sitecustomize.py` (covers App.run real lifecycle, not just
  mock-paths).
- **TLS/mTLS integration test** with ephemeral self-signed CA
  ([`tests/_certs.py`](tests/_certs.py)).
- **Auto-Broker fixture**: `tests/conftest.py` spawns mosquitto for
  the test session if none reachable + mosquitto on `$PATH`.
- **Reconnect-Backoff integration test** verifies exponential
  progression directly from App logs.
- **Whitelist-Gate integration test** with custom capabilities marker
  (`@pytest.mark.probe_config`).
- **Real Linux + macOS dev + Windows CI**: matrix Linux (Python
  3.9-3.13) + macOS (3.12-3.13) + Windows 3.13. DeprecationWarning
  treated as test error.
- **`mqtt_subscriber` event-driven** (`min_count` parameter) for
  fast retained-message tests.
- **`xdist`-port-suffix** for parallel test sessions.

### Live-verified
End-to-end against `brew install mosquitto` during the test-deepening
phase: smoke-test 7/7, Last-Will via SIGKILL, Reconnect-Backoff
5s→10s→20s→40s with broker-down/up cycle, recovery + backoff-reset.

[Unreleased]: https://github.com/stiftung-humboldtforum/mediensteuerung-probe/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/stiftung-humboldtforum/mediensteuerung-probe/releases/tag/v0.2.0
