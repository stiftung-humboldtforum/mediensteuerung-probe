# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For the historical migration from `avorus-probe` see
[`docs/migration-from-avorus.md`](docs/migration-from-avorus.md).

## [Unreleased]

### Added
- **Auto-Broker fixture**: `tests/conftest.py:pytest_configure` startet
  bei `pytest -m integration` automatisch einen ephemeren Mosquitto in
  einem Subprocess wenn keiner erreichbar ist und `mosquitto` auf
  `$PATH` steht. Cleanup über `pytest_unconfigure`. Kein manuelles
  `docker compose up` oder `mosquitto -d` mehr nötig.
- **Windows-CI-Slot**: `windows-latest` Python 3.13 in der CI-Matrix
  verifiziert dass `_win32.py` import-clean ist und der Unit-Test-
  Suite gegen die Windows-Python-Version grün bleibt.
- **`scripts/hardware-test-linux.sh`** + **`scripts/hardware-test-windows.ps1`**:
  Direkt-Aufrufe der Plattform-spezifischen Funktionen (wpctl/xrandr/
  psutil-Sensors auf Linux; pycaw/LHM/Win32 auf Windows) mit PASS/FAIL-
  Reporting, runnable via SSH/RDP nach Deploy.

### Changed
- CI integration-Job vereinfacht — der explizite `Start test broker`-
  Step ist weg, der Auto-Broker übernimmt.
- **Bug-Fixes aus dem ersten Live-E2E-Test:**
  - `get_config` ignoriert jetzt Kommentar-Zeilen (R4 Userconfig hatte
    welche eingeführt, `shlex.split` zerlegte sie zu Garbage-Keys).
  - `App.run`-Watchdog-Stall-Tolerance: erst nach 3 stallenden Cycles
    Warning, fängt den initialen Probe-Thread/App-Thread Start-Race ab.
  - `.pre-commit-config.yaml` ruff-args YAML-Syntax (Block-Style statt
    Inline-List mit Kommas).
- **Live-verifiziert** durch lokalen E2E-Test gegen brew-mosquitto:
  Smoke-Test 7/7, Last-Will (SIGKILL → connected="0"), Reconnect-
  Backoff 5s→10s→20s→40s, Recovery + Backoff-Reset.

## [0.2.0] — 2026-04-29

### Security
- Command-Whitelist (`COMMANDS` dict) replaces reflection-based dispatch in `Probe.on_message` —
  manager can no longer reach imported module attributes (`os`, `subprocess`, ...).
- Sensor / Command separation via explicit `SENSORS` / `COMMANDS` whitelists prevents
  `shutdown` accidentally landing in `PROBE_METHODS` and being polled every 5s.
- Fail-closed default for missing `PROBE_CAPABILITIES` (was permissive
  `wake,shutdown,reboot`).
- Payload args/kwargs `isinstance` validation in `parse_payload`.
- Topic parsing safe against malformed manager topics (no IndexError).
- Loud banner warning when `--no_tls` is used against a non-localhost broker.

### Added
- Cross-platform support: Linux (`_linux.py`) and Windows (`_win32.py`) implementations,
  unified dispatch in `methods/__init__.py`, graceful `_stub.py` fallback for macOS dev.
- MQTT Last-Will (`probe/<fqdn>/connected = "0"`) so broker informs manager on unclean
  disconnects.
- Retained `connected` / `capabilities` / `boot_time` so newly subscribing managers see
  current state immediately.
- QoS 1 for command responses + critical state topics.
- Sensor values for `display`, `easire`, `mpv_file_pos_sec` are now actually published
  (previously only ok/error in the errors-dict).
- `sd_notify` watchdog gated on Probe-Thread heartbeat — stalled probes get auto-
  restarted by systemd instead of the watchdog firing into a dead App-loop.
- Exponential backoff (5s → 60s) on reconnect failures.
- `subprocess` timeouts (3-5s) on all external tool calls (wpctl, xrandr, mpv_control)
  to prevent the Probe-Thread from hanging when those tools are unresponsive.
- `App.fqdn` cached + explicit `_refresh_fqdn()` raising `FqdnChanged` (was a Property
  with side-effect + DNS-Lookup per read).
- `connected_event` (threading.Event) replaces blind `time.sleep(3)` after MQTT connect —
  no more reconnect-flap on slow brokers.
- `_stop_event` for interruptible `Probe.run` sleeps (was `time.sleep(5)` blocking the
  shutdown by up to 5s).
- `notify.status` now contains exception type + message instead of generic `Failed.`.
- Heartbeat bump in `on_connect` so the watchdog sees a live signal even before the first
  `call_methods()` cycle completes.
- Reference `systemd/humboldt-probe.service` unit file (Type=notify, WatchdogSec=30s).
- `scripts/install-windows.ps1` — idempotent NSSM service setup.
- GitHub Actions CI: pytest matrix Linux (Python 3.9-3.13) + macOS (3.12-3.13),
  DeprecationWarning treated as error.
- Coverage reporting in CI.
- Dependabot config (monthly pip + actions updates).
- Pre-commit hooks (whitespace, syntax checks, ruff lite).
- `pyproject.toml` for project metadata + tool config.
- `requirements.lock.txt` via `pip-compile` for reproducible builds.
- LICENSE placeholder (Stiftung action required).

### Changed
- `paho-mqtt` upgraded from 1.6.1 to 2.x with `CallbackAPIVersion.VERSION2` callbacks.
- `os.system()` → `subprocess.run([...])` for shutdown/reboot with proper error
  reporting (rc + stderr).
- All `shell=True` removed from sensor commands (xrandr, mpv_control); were static
  strings, no injection risk, but cleaner without.
- `easire()` Linux unified with Windows on `psutil.process_iter` (was `ps ax | grep`).
- Windows `temperatures()` / `fans()` schema unified with Linux: multiple sensors per
  hardware (was first-only).
- `sys.coinit_flags = 0` moved from `methods/__init__.py` to top of `app.py` (had to
  happen before any other import touched COM).
- Loglevel default `INFO` (was `CRITICAL`); ISO-timestamps in log format.
- `errors`-topic published once per cycle instead of per-method (N redundant publishes).
- `error_response` renamed to `status_response` (reflects that it carries data, not error).
- Plattform-specific code split out of `methods/sensors.py` and `methods/__init__.py`
  into `_linux.py` / `_win32.py` / `_stub.py`. Old `sensors.py` deleted.
- README rewritten: concrete MQTT-topic table (retain/qos/payload), pyproject section,
  reproducible-build workflow.
- `requirements.txt` consistently pinned with major-caps; `pytest` moved to
  `requirements-dev.txt`.
- `userconfig.example.txt` now includes `mpv_file_pos_sec` and inline doc.
- `CHANGELOG_linux.md` renamed to `docs/migration-from-avorus.md` with note that it's
  a one-shot migration log, not a living changelog.
- DLLs (LibreHardwareMonitorLib, HidSharp) now committed under `lib/win32/` with
  `LICENSE.txt` for the bundled binaries (.gitignore was previously blocking them).

### Fixed
- `try/except Exception` in `Probe.__init__` for missing config keys narrowed to
  `KeyError`.
- Unknown periodic methods log a warning instead of being silently dropped.
- `check_*` methods set `errors[name] = 'error'` even when the underlying sensor raises
  (previously stale-state).
- Capabilities re-publish in `call_methods` removed (was QoS 0 / not retained — pure
  traffic noise; on_connect already publishes retained).

### Tests
- 70 unit tests covering misc, methods (Linux paths + common), Probe-class +
  Probe-Thread lifecycle, App-layer (FQDN caching, no_tls banner, CLI validation).
- Integration tests for subprocess timeouts (TimeoutExpired propagation).
- DeprecationWarning treated as test error in CI.

[Unreleased]: https://github.com/stiftung-humboldtforum/mediensteuerung-probe/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/stiftung-humboldtforum/mediensteuerung-probe/releases/tag/v0.2.0
