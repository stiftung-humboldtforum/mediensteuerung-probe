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
    app.py              # Main entry point (CLI, MQTT client, lifecycle)
    probe.py            # Probe thread (periodic sensor polling, command handling)
    methods/
      __init__.py       # Capabilities (shutdown, reboot, mute, unmute, ping)
      sensors.py        # Probe methods (temperatures, fans, uptime, display, ...)
    misc/
      __init__.py       # Config parser, logger, response helpers
  lib/
    win32/              # LibreHardwareMonitor DLLs (Windows only)
  tests/
    conftest.py         # Test configuration (sys.path setup)
    test_misc.py        # Tests for config parsing, response helpers
    test_methods.py     # Tests for sensor methods, call_method
    test_probe.py       # Tests for probe init, callbacks, security
  requirements.txt
  userconfig.example.txt
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
pytest
```

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

A typical implementation is a small shell script around `socat` or
`echo | nc -U /tmp/mpvsocket` that issues `{"command":["get_property","time-pos"]}`
and prints the integer seconds. See e.g.
<https://github.com/mpv-player/mpv/blob/master/DOCS/man/ipc.rst>.

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

Create a systemd unit file and enable it. The probe supports `sd_notify` for watchdog integration.

### Windows (NSSM)

```powershell
# Install NSSM
winget install NSSM.NSSM

# Install the service
nssm install HumboldtProbe "C:\Program Files\Python313\python.exe" "app.py --config_file ../userconfig.txt --mqtt_hostname <broker-ip> --mqtt_port 1883 --no_tls --loglevel INFO"
nssm set HumboldtProbe AppDirectory C:\path\to\humboldt-probe\src
nssm set HumboldtProbe AppStdout C:\path\to\humboldt-probe\probe.log
nssm set HumboldtProbe AppStderr C:\path\to\humboldt-probe\probe.log
nssm set HumboldtProbe AppRotateFiles 1
nssm set HumboldtProbe AppRotateBytes 1048576

# Manage the service
nssm start HumboldtProbe
nssm stop HumboldtProbe
nssm restart HumboldtProbe
nssm status HumboldtProbe
```

NSSM automatically restarts the probe on crash and starts it on boot.

## Local Testing

### 1. Install Mosquitto

**macOS:**
```bash
brew install mosquitto
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt install mosquitto mosquitto-clients
```

**Windows:**
```powershell
winget install EclipseFoundation.Mosquitto
```

### 2. Start the broker

```bash
mosquitto -p 1883 -v
```

### 3. Start the probe

```bash
python src/app.py \
    --config_file userconfig.txt \
    --mqtt_hostname <broker-ip> \
    --no_tls \
    --loglevel DEBUG
```

### 4. Monitor sensor data

With MQTT Explorer: connect to `<broker-ip>:1883`. All topics appear under `probe/<hostname>/`.

Or via command line:

```bash
mosquitto_sub -h <broker-ip> -p 1883 -t 'probe/#' -v
```

### 5. Send commands

```bash
mosquitto_pub -h <broker-ip> -p 1883 -t 'manager/<hostname>/mute' -m ''
mosquitto_pub -h <broker-ip> -p 1883 -t 'manager/<hostname>/unmute' -m ''
mosquitto_pub -h <broker-ip> -p 1883 -t 'manager/<hostname>/reboot' -m ''
```

## Remote Development (Mac to Windows)

With OpenSSH enabled on the Windows target, you can deploy changes directly:

```bash
# Deploy source files
scp -r src/* user@<windows-ip>:C:/path/to/humboldt-probe/src/

# Restart the service
ssh user@<windows-ip> "nssm restart HumboldtProbe"

# Test individual methods
ssh user@<windows-ip> "cd C:\path\to\humboldt-probe\src && python -c \"from methods.sensors import temperatures; print(temperatures())\""
```
