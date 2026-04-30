# Humboldt-Probe

Humboldt-Probe is a software component designed to be installed on computers within AV/Digital Media installations. It enables these computers to communicate with a manager service, providing real-time control and reporting health and status metrics back to the system via MQTT.

## Features

- **MQTT Integration:** Secure client connection to an MQTT broker for telemetry and command exchange.
- **Cross-Platform:** Runs on Linux and Windows with platform-specific sensor implementations.
- **Hardware Monitoring:** CPU/GPU temperatures and fan speeds via psutil (Linux) or LibreHardwareMonitor (Windows).
- **Audio Control:** Mute/unmute via wpctl/PipeWire (Linux) or pycaw/COM (Windows).
- **Service Watchdog:** Utilizes `sd_notify` for systemd notification (Linux).
- **Resilience:** Built-in retry mechanism for intermittent network issues.
- **Secure Communication:** TLS support for MQTT.

## Project Structure

```
humboldt-probe/
  src/
    app.py                       # Lifecycle: CLI, MQTT client, reconnect/backoff loop, sd_notify
    probe.py                     # Probe thread (periodic polling + manager-command dispatch)
    methods/
      __init__.py                # Platform-dispatch + common sensors (ping, boot_time, mpv_file_pos_sec, easire) + SENSORS/COMMANDS whitelists
      _linux.py                  # Linux impl (wpctl, xrandr, psutil, /proc/uptime)
      _win32.py                  # Windows impl (pycaw, LibreHardwareMonitor, Win32 EnumDisplaySettingsW)
      _stub.py                   # Fallback for unsupported platforms (macOS dev)
    misc/
      __init__.py                # Config parser, JSON envelope, payload validation

  tests/
    conftest.py                  # Auto-Broker, MQTT fixtures, running_probe, tls_broker (see top-of-file sections)
    sitecustomize.py             # subprocess-coverage hook (Probe-subprocess covered too)
    _certs.py                    # ephemeral CA + server + client certs for TLS test
    test_misc.py                 # 18 tests: config parser, JSON envelope, payload validation
    test_methods.py              # 20 tests: Linux paths + common sensors + display parser
    test_methods_win32.py        # 11 tests: Windows paths via mocks (pycaw, LHM, ctypes)
    test_probe.py                # 30 tests: Probe class + threading + whitelist gates
    test_app.py                  # 12 tests: FQDN caching, --no_tls banner, sd_notify sequences
    test_integration.py          # 9 tests: end-to-end against real Mosquitto

  scripts/
    smoke-test.sh                # Pre-/post-deploy MQTT verification (7 checks)
    hardware-test-linux.sh       # Hardware sensors via SSH after Linux deploy
    hardware-test-windows.ps1    # Hardware sensors via RDP/PSRemoting after Windows deploy
    install-windows.ps1          # Idempotent NSSM service setup
    mpv_control.example.sh       # Reference impl for the optional mpv_file_pos_sec helper

  systemd/humboldt-probe.service # Reference unit (Type=notify, WatchdogSec=30s)

  lib/win32/                     # LibreHardwareMonitorLib.dll + HidSharp.dll + LICENSE.txt
  mosquitto/test.conf            # mosquitto config used by docker-compose.test.yml

  docs/
    testing.md                   # Test strategy: unit / integration / hardware / CI
    migration-from-avorus.md     # Historical fork log (one-shot, not a living changelog)

  .github/
    workflows/test.yml           # Matrix Linux 3.9-3.13 + macOS + Windows + integration + lint
    dependabot.yml               # monthly pip + actions updates

  Dockerfile.linux-test          # Linux-codepath verification on macOS-Dev (no PipeWire/X11 — see docs/testing.md §10)
  docker-compose.linux-test.yml  #     ↳ wrapper for the above
  docker-compose.test.yml        # Standalone Mosquitto for ad-hoc 'pytest -m integration'

  pyproject.toml                 # project metadata + pytest/coverage config
  requirements.txt               # runtime deps (Linux + Windows markers)
  requirements-dev.txt           # adds pytest, coverage, pip-tools, cryptography
  requirements.lock.txt          # transitive lock via pip-compile
  userconfig.example.txt         # Sample PROBE_METHODS / PROBE_CAPABILITIES
  CHANGELOG.md                   # Keep-a-Changelog format
  LICENSE                        # ⚠ placeholder — Stiftung action required
```

## MQTT Topics

The probe uses two topic prefixes:

- **`probe/<fqdn>/...`** — Outbound: sensor data and command responses published by the probe.
- **`manager/<fqdn>/...`** — Inbound: commands sent to the probe from the manager.

### Outbound topics in detail

| Topic                                | Retained | QoS | Payload                                                                 |
| ------------------------------------ | -------- | --- | ----------------------------------------------------------------------- |
| `probe/<fqdn>/connected`             | yes      | 1   | `"1"` while online, `"0"` set as MQTT Last-Will on unclean disconnect.  |
| `probe/<fqdn>/capabilities`          | yes      | 1   | CSV string from `PROBE_CAPABILITIES` (e.g. `wake,shutdown,reboot`).     |
| `probe/<fqdn>/boot_time`             | yes      | 1   | JSON envelope with Unix-epoch float (`{"data":{"result":1714...}}`).    |
| `probe/<fqdn>/<sensor>`              | no       | 0   | JSON envelope with sensor result, published every 5s.                   |
| `probe/<fqdn>/errors`                | no       | 0   | JSON envelope with status dict (`{display:'ok', easire:'error', ...}`). |
| `probe/<fqdn>/<command>`             | no       | 1   | Response to a manager command (`{"data":{"status":"received"}}` then `{"data":{"status":"complete","result":...}}` or `{"error":{...}}`). |

### Inbound topics

| Topic                                | Payload                                                                              |
| ------------------------------------ | ------------------------------------------------------------------------------------ |
| `manager/<fqdn>/<command>`           | Optional JSON `{"args":[...], "kwargs":{...}}`. Empty payload means no-arg call.     |

The probe subscribes with `noLocal=True`, so its own outbound publishes
are not echoed back as commands.

### Response envelope

All JSON envelopes follow:

```json
{"data": {"status": "complete", "result": <sensor-or-command-result>}}
```

or, on failure:

```json
{"error": {"message": "<ExceptionName>", "errors": [...]}}
```

## System Requirements

### Linux

- Python 3.9+ (uses PEP 585 generic syntax `dict[str, Any]`)
- Network access to the MQTT broker
- wpctl (PipeWire/WirePlumber) for audio control
- xrandr for display info

### Windows

- Python 3.13+ (system-wide installation, not Windows Store)
- Network access to the MQTT broker
- LibreHardwareMonitorLib.dll + HidSharp.dll in `lib/win32/` (committed in repo)
- NSSM for running as a service

## Installation

### Linux

```bash
pip install -r requirements.txt
```

### Windows

1. Install Python system-wide: `winget install Python.Python.3.13 --scope machine`
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

### Development / Tests

```bash
pip install -r requirements-dev.txt
pytest                        # 72 Unit + 8 Integration (Auto-Broker)
pytest -m integration         # nur Integration gegen Auto-Broker
```

For Linux-codepath verification on a macOS dev machine, use Docker:

```bash
docker compose -f docker-compose.linux-test.yml run --rm probe-test
```

See [`docs/testing.md`](docs/testing.md) for the full testing
strategy, including hardware-smoke-tests, VM setup for Windows
verification, and CI matrix.

### Reproducible builds (`pip-compile`)

`requirements.lock.txt` is the resolved transitive lock of
`requirements.txt`, generated via `pip-compile` (from `pip-tools`).
For deterministic deploys, prefer it over `requirements.txt`:

```bash
pip install -r requirements.lock.txt
```

To re-generate (run on the **target platform** so `sys_platform` markers
resolve correctly — Linux for production probes):

```bash
pip install pip-tools
pip-compile --strip-extras --output-file=requirements.lock.txt requirements.txt
```

### Project layout / `pyproject.toml`

`pyproject.toml` carries project metadata (name, version,
`requires-python>=3.9`) and tool configuration (pytest paths, treat
`DeprecationWarning` as errors). It is **not** a build-system manifest
— the probe is deployed via `pip install -r requirements*.txt`, not
as a wheel. So:

- Source-of-truth for **install** is `requirements.txt` /
  `requirements.lock.txt`.
- Source-of-truth for **dev tooling** (pytest discovery, sys.path)
  is `pyproject.toml`.

Running `pytest` from the repo root works without arguments because
`pyproject.toml` already declares `pythonpath = ["src"]` and
`testpaths = ["tests"]`.

## Usage

```bash
python src/app.py \
    --config_file userconfig.txt \
    --mqtt_hostname mqtt.example.com \
    --mqtt_port 8883 \
    --ca_certificate /path/to/ca.pem \
    --certfile /path/to/client.pem \
    --keyfile /path/to/client_key.pem \
    --loglevel INFO
```

### CLI Options

| Option             | Description                                                    |
| ------------------ | -------------------------------------------------------------- |
| `--config_file`    | Path to the configuration file (required)                      |
| `--mqtt_hostname`  | Hostname of the MQTT broker (required)                         |
| `--mqtt_port`      | MQTT port (default: 8883, or 1883 with `--no_tls`)            |
| `--ca_certificate` | CA certificate for TLS (required unless `--no_tls`)            |
| `--certfile`       | Client certificate for TLS (required unless `--no_tls`)        |
| `--keyfile`        | Client key for TLS (required unless `--no_tls`)                |
| `--no_tls`         | Disable TLS (for local testing)                                |
| `--loglevel`       | CRITICAL, ERROR, WARNING, INFO, DEBUG                          |

## Configuration

The config file contains two comma-separated lists:

```bash
PROBE_METHODS="ping,temperatures,fans,uptime,display,is_muted,easire"
PROBE_CAPABILITIES="wake,shutdown,reboot,mute,unmute"
```

`PROBE_METHODS` — Sensors polled every 5 seconds, results published via MQTT.

`PROBE_CAPABILITIES` — Reported to the manager; defines which commands the probe accepts.

### Available PROBE_METHODS

| Method           | Description                         | Linux              | Windows                    |
| ---------------- | ----------------------------------- | ------------------ | -------------------------- |
| ping             | Signal that the device is alive     | -                  | -                          |
| uptime           | Seconds since boot                  | /proc/uptime       | psutil                     |
| temperatures     | CPU/GPU temperatures                | psutil             | LibreHardwareMonitor       |
| fans             | CPU/GPU fan speeds                  | psutil             | LibreHardwareMonitor       |
| display          | Display resolution and refresh rate | xrandr             | Win32 API (ctypes)         |
| is_muted         | Audio mute state                    | wpctl              | pycaw                      |
| easire           | easire-player process running       | psutil             | psutil                     |
| mpv_file_pos_sec | Playback position of mpv player     | mpv_control        | -                          |

#### `mpv_control`

`mpv_file_pos_sec` requires an external helper called `mpv_control` on
`$PATH` that talks to mpv's IPC socket. It is **not** installed by
this repo and not published as a pip package. If you don't run mpv as
the kiosk player, you can omit `mpv_file_pos_sec` from `PROBE_METHODS`.

A reference implementation is at
[`scripts/mpv_control.example.sh`](scripts/mpv_control.example.sh) —
copy to `/usr/local/bin/mpv_control` and make executable. It uses
`socat` to talk to mpv's IPC socket (`--input-ipc-server=/tmp/mpvsocket`)
and prints the integer seconds.

See also <https://github.com/mpv-player/mpv/blob/master/DOCS/man/ipc.rst>.

### Available PROBE_CAPABILITIES

| Capability | Description          | Linux              | Windows                    |
| ---------- | -------------------- | ------------------ | -------------------------- |
| wake       | Wake the device      | Wake-on-LAN (external) | Wake-on-LAN (external)     |
| shutdown   | Shut down the device | sudo shutdown now  | shutdown /s /t 0           |
| reboot     | Reboot the device    | sudo reboot now    | shutdown /r /t 0           |
| mute       | Mute audio           | wpctl              | pycaw                      |
| unmute     | Unmute audio         | wpctl              | pycaw                      |

## Running as a Service

### Linux (systemd)

A reference unit file is at [`systemd/humboldt-probe.service`](systemd/humboldt-probe.service).
Adjust paths and then:

```bash
sudo cp systemd/humboldt-probe.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now humboldt-probe
journalctl -u humboldt-probe -f
```

The unit is `Type=notify` with `WatchdogSec=30s`, so the probe's
heartbeat (see B6) is enforced — a stalled probe is auto-restarted.

### Windows (NSSM)

```powershell
# Install NSSM (one-time)
winget install NSSM.NSSM
```

Install / re-install the service via the helper script (run as
Administrator):

```powershell
.\scripts\install-windows.ps1 `
    -MqttHostname mqtt.example.com `
    -CaCertificate C:\humboldt-probe\ca.pem `
    -CertFile C:\humboldt-probe\client.pem `
    -KeyFile C:\humboldt-probe\client.key
```

For local testing against a localhost broker:

```powershell
.\scripts\install-windows.ps1 -MqttHostname 127.0.0.1 -NoTls
```

The script is idempotent — re-running it with different args
reconfigures the existing service in-place.

```powershell
# Manage the service
nssm start   HumboldtProbe
nssm stop    HumboldtProbe
nssm restart HumboldtProbe
nssm status  HumboldtProbe
```

NSSM automatically restarts the probe on crash and starts it on boot.

## Local Testing

The test suite handles its own MQTT broker — install
`mosquitto` once and `pytest` does the rest:

```bash
# macOS:        brew install mosquitto
# Debian/Ubu:   sudo apt install mosquitto mosquitto-clients
# Windows:      winget install EclipseFoundation.Mosquitto

pip install -r requirements-dev.txt
pytest                       # 102 tests, ~50s — Auto-Broker spawns Mosquitto if needed
pytest -m integration        # only the 9 integration tests (real Mosquitto roundtrips)
```

For ad-hoc manual exploration (Probe + your own MQTT-Explorer
session), see [`docs/testing.md` §3b](docs/testing.md).

### Real-hardware verification

Probe-Code uses platform-specific sensors — `methods/_stub.py` no-ops
on macOS-Dev. To verify the real Linux/Windows codepath you need
either Docker (Linux only, code-paths but no audio/display hardware)
or a VM. **Quick instructions in [`docs/quick-test-real-hardware.md`](docs/quick-test-real-hardware.md).**

## Remote Development (Mac to Linux/Windows)

With SSH (Linux + OpenSSH on Windows) you can iterate from your dev
machine:

```bash
# Sync the working tree
rsync -av --exclude='.venv' --exclude='__pycache__' . user@kiosk-01:/opt/humboldt-probe/

# Restart the service after a change
ssh user@kiosk-01 "sudo systemctl restart humboldt-probe"
ssh win-kiosk-01 "nssm restart HumboldtProbe"   # via Win-OpenSSH

# One-off sensor check on the real hardware
ssh user@kiosk-01 "cd /opt/humboldt-probe && bash scripts/hardware-test-linux.sh"
ssh win-kiosk-01 "cd C:\\humboldt-probe; powershell .\\scripts\\hardware-test-windows.ps1"
```
