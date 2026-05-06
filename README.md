# Humboldt-Probe

Software-Komponente fuer AV/Digital-Media-Installations-PCs. Verbindet
sich per MQTT mit dem Manager-Service und liefert Telemetrie / nimmt
Kommandos entgegen.

## Features

- MQTT-Client (TLS / mTLS optional, Last-Will, retained Status-Topics)
- Cross-Platform: Linux + Windows mit plattform-spezifischen Sensoren
- Hardware-Monitoring: CPU/GPU-Temperaturen + Lueftern via psutil (Linux)
  bzw. LibreHardwareMonitor (Windows)
- Audio-Steuerung: wpctl/PipeWire (Linux) bzw. pycaw/COM (Windows)
- systemd-Watchdog (`sd_notify`, Linux)
- Reconnect-Backoff bei Verbindungsabbruch

## Projektstruktur

```
src/
  app.py                       Lifecycle: CLI, MQTT, Reconnect-Loop, sd_notify
  probe.py                     Probe-Thread (Sensor-Polls + Command-Dispatch)
  methods/
    __init__.py                Plattform-Dispatch + SENSORS/COMMANDS-Whitelists
    _linux.py                  Linux (wpctl, xrandr, psutil)
    _win32.py                  Windows (pycaw, LibreHardwareMonitor, Win32 API)
  misc/
    __init__.py                Config-Parser, JSON-Envelope, Validation

tests/                         pytest unit + integration (Mosquitto extern)
scripts/
  smoke-test.sh                MQTT-Verifikation pre-/post-deploy
  hardware-test-{linux,windows}.* Hardware-Sensor-Checks am Geraet
  install-windows.ps1          Idempotenter NSSM-Service-Setup
  mpv_control.example.sh       Reference-Impl fuer mpv_file_pos_sec
systemd/humboldt-probe.service Reference-Unit (Type=notify, WatchdogSec=30s)
lib/win32/                     LibreHardwareMonitorLib.dll + HidSharp.dll
```

## MQTT-Topics

Zwei Praefixe:

- `probe/<fqdn>/...`   — outbound (Sensor-Daten + Command-Antworten)
- `manager/<fqdn>/...` — inbound (Kommandos vom Manager)

### Outbound

| Topic                       | Retained | QoS | Payload                                                              |
| --------------------------- | -------- | --- | -------------------------------------------------------------------- |
| `probe/<fqdn>/connected`    | yes      | 1   | `"1"` online, `"0"` als Last-Will bei unsauberem Disconnect          |
| `probe/<fqdn>/capabilities` | yes      | 1   | CSV aus `PROBE_CAPABILITIES`                                         |
| `probe/<fqdn>/boot_time`    | yes      | 1   | JSON-Envelope mit Unix-Epoch                                         |
| `probe/<fqdn>/<sensor>`     | no       | 0   | JSON-Envelope mit Sensor-Result, alle 5s                             |
| `probe/<fqdn>/errors`       | no       | 0   | JSON-Envelope mit Status-Dict pro Sensor                             |
| `probe/<fqdn>/<command>`    | no       | 1   | Command-Response (`status: received` → `complete` oder `error`)      |

### Inbound

| Topic                      | Payload                                                            |
| -------------------------- | ------------------------------------------------------------------ |
| `manager/<fqdn>/<command>` | optional JSON `{"args":[...], "kwargs":{...}}` — leer = arg-los    |

`noLocal=True` beim Subscribe — eigene Outbound-Publishes werden nicht
als Kommando zurueckgespiegelt.

### Response-Envelope

```json
{"data": {"status": "complete", "result": <sensor-or-command-result>}}
```

oder bei Fehler:

```json
{"error": {"message": "<ExceptionName>", "errors": [...]}}
```

## System-Requirements

### Linux

- Python 3.9-3.13 (3.14 nicht unterstuetzt — pythonnet-Cap)
- Netzwerk-Zugang zum MQTT-Broker
- wpctl (PipeWire/WirePlumber) fuer Audio
- xrandr fuer Display-Info

### Windows

- Python 3.13 system-wide (`winget install Python.Python.3.13 --scope machine`)
- Netzwerk-Zugang zum MQTT-Broker
- LibreHardwareMonitorLib.dll + HidSharp.dll in `lib/win32/` (im Repo)
- NSSM fuer Service-Betrieb

## Installation

```bash
# Linux
pip install -r requirements.lock.txt

# Windows
pip install -r requirements.lock.windows.txt

# Dev / Tests
pip install -r requirements-dev.txt
```

## Usage

Defaults: `--mqtt_hostname srv-control-avm`, Cert-Dateien
`ca_certificate.pem` / `client_certificate.pem` / `client_key.pem`
neben `app.py`. Damit reicht im einfachsten Fall:

```bash
python src/app.py --config_file userconfig.txt
```

Vollstaendig:

```bash
python src/app.py \
    --config_file userconfig.txt \
    --mqtt_hostname srv-control-avm \
    --mqtt_port 8883 \
    --ca_certificate /path/to/ca_certificate.pem \
    --certfile /path/to/client_certificate.pem \
    --keyfile /path/to/client_key.pem \
    --loglevel INFO
```

### CLI-Optionen

| Option             | Default                  | Beschreibung                            |
| ------------------ | ------------------------ | --------------------------------------- |
| `--config_file`    | (required)               | Pfad zur userconfig.txt                 |
| `--mqtt_hostname`  | `srv-control-avm`        | MQTT-Broker-Host                        |
| `--mqtt_port`      | 8883 (1883 mit `--no_tls`) | MQTT-Port                             |
| `--ca_certificate` | `ca_certificate.pem`     | CA fuer TLS                             |
| `--certfile`       | `client_certificate.pem` | Client-Cert fuer mTLS                   |
| `--keyfile`        | `client_key.pem`         | Client-Key fuer mTLS                    |
| `--no_tls`         | false                    | TLS aus (nur lokales Testen)            |
| `--loglevel`       | INFO                     | CRITICAL/ERROR/WARNING/INFO/DEBUG       |

### Env-Variablen

| Variable               | Default | Beschreibung                                            |
| ---------------------- | ------- | ------------------------------------------------------- |
| `PROBE_MQTT_KEEPALIVE` | 60      | MQTT-Keepalive-Sek. Niedriger = schnellerer Last-Will.  |

## Configuration

`userconfig.txt`:

```bash
PROBE_METHODS="ping,temperatures,fans,uptime,display,is_muted"
PROBE_CAPABILITIES="wake,shutdown,reboot,mute,unmute"
```

`PROBE_METHODS` — Sensoren die alle 5s gepollt werden.
`PROBE_CAPABILITIES` — Kommandos die der Manager senden darf.

### PROBE_METHODS

| Methode          | Beschreibung                | Linux         | Windows               |
| ---------------- | --------------------------- | ------------- | --------------------- |
| ping             | Lebenszeichen               | -             | -                     |
| uptime           | Sekunden seit Boot          | /proc/uptime  | psutil                |
| temperatures     | CPU/GPU-Temperaturen        | psutil        | LibreHardwareMonitor  |
| fans             | Lueftergeschwindigkeiten    | psutil        | LibreHardwareMonitor  |
| display          | Aufloesung + Refresh-Rate   | xrandr        | Win32 API (ctypes)    |
| is_muted         | Audio-Mute-State            | wpctl         | pycaw                 |
| easire           | easire-Player laeuft?       | psutil        | psutil                |
| mpv_file_pos_sec | mpv-Playback-Position       | mpv_control   | -                     |

`easire` und `mpv_file_pos_sec` sind optional und nur sinnvoll wenn
die jeweilige Anwendung am Kiosk laeuft. `mpv_file_pos_sec` braucht
zusaetzlich das externe `mpv_control`-Tool auf `$PATH` — Reference-Impl
in [`scripts/mpv_control.example.sh`](scripts/mpv_control.example.sh).

### PROBE_CAPABILITIES

| Capability | Beschreibung    | Linux                  | Windows                |
| ---------- | --------------- | ---------------------- | ---------------------- |
| wake       | Wake-on-LAN     | extern via Manager     | extern via Manager     |
| shutdown   | Geraet aus      | sudo shutdown now      | shutdown /s /t 0       |
| reboot     | Geraet reboot   | sudo reboot now        | shutdown /r /t 0       |
| mute       | Audio mute      | wpctl                  | pycaw                  |
| unmute     | Audio unmute    | wpctl                  | pycaw                  |

## Service-Betrieb

### Linux (systemd)

```bash
sudo cp systemd/humboldt-probe.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now humboldt-probe
journalctl -u humboldt-probe -f
```

`Type=notify` mit `WatchdogSec=30s` — gestallter Probe wird automatisch
neu gestartet.

### Windows (NSSM)

```powershell
winget install NSSM.NSSM
.\scripts\install-windows.ps1   # Default: srv-control-avm + Certs aus C:\humboldt-probe\
nssm {start|stop|restart|status} HumboldtProbe
```

NSSM startet die Probe bei Boot und nach Crash automatisch neu.

## Operations

### MQTT-Topics fuer Operator

| Topic                    | Zweck                                                |
| ------------------------ | ---------------------------------------------------- |
| `probe/<fqdn>/connected` | `"1"` online, `"0"` Last-Will. retained.             |
| `probe/<fqdn>/version`   | Probe-Software-Version (z.B. `0.2.0`). retained.     |
| `probe/<fqdn>/capabilities` | CSV der unterstuetzten Kommandos. retained.       |
| `probe/<fqdn>/boot_time` | Unix-epoch Boot. retained.                            |
| `probe/<fqdn>/errors`    | Status-Aggregation pro Sensor (alle 5s).             |

### Cert-Renewal

mTLS-Zertifikate haben begrenzte Lifetime. Vor Ablauf:

1. Neue Cert+Key auf Kiosk legen (gleiche Dateinamen ueberschreiben).
2. Service neu starten:
   - Linux: `sudo systemctl restart humboldt-probe`
   - Windows: `nssm restart HumboldtProbe`
3. Verify: `journalctl -u humboldt-probe -n 50` (Linux) bzw. NSSM-Logfile.

### Update / Rollback

```bash
# Linux
git -C /opt/humboldt-probe fetch && git -C /opt/humboldt-probe checkout <tag>
sudo systemctl restart humboldt-probe

# Rollback
git -C /opt/humboldt-probe checkout <previous-tag>
sudo systemctl restart humboldt-probe
```

`probe/<fqdn>/version` zeigt nach Restart die neue Version — Manager-
Dashboard kann Fleet-Drift erkennen.

### Troubleshooting

| Symptom | Wahrscheinliche Ursache | Diagnose |
|---|---|---|
| `Setup error: [Errno 2] No such file or directory` | Cert-Datei fehlt oder Pfad falsch | `ls *.pem` neben `app.py` pruefen, oder `--ca_certificate=...` setzen |
| `Setup error: [Errno -2] Name or service not known` (Linux) / `getaddrinfo failed` (Windows) | Hostname unbekannt | `nslookup <broker>` / `ping <broker>` — VPN aktiv? |
| `TimeoutError: timed out` | Broker erreicht aber nicht antwortend (Firewall, falscher Port) | `nc -zv <broker> 8883` |
| `Setup failed, retrying in 5s` Loop | Persistent error in `_setup` (cert / hostname / config_file) | `journalctl -u humboldt-probe -n 50` zeigt Stacktrace; ersten Fehler beheben |
| Manager sieht `connected="1"` aber keine Sensor-Daten | `PROBE_METHODS` leer oder nur unbekannte Sensoren | `cat userconfig.txt` und Log nach `Ignoring unknown PROBE_METHODS` durchsuchen |
| Manager-Command `mute` antwortet `Method not allowed` | `mute` fehlt in `PROBE_CAPABILITIES` | `userconfig.txt` ergaenzen, Service neu starten |
| `probe/<fqdn>/temperatures` ist `{}` (leer, Windows) | LHM braucht Admin-Rechte; NSSM-Service als LocalSystem oder Admin-User starten | `nssm set HumboldtProbe ObjectName <Admin-User> ...` |
| `wpctl: command not found` (Linux) | PipeWire/WirePlumber nicht installiert | `sudo apt install pipewire wireplumber` |
| `Cannot read config ...` | userconfig.txt-Pfad falsch oder Permissions | `ls -la` + ggf. owner-Anpassung an Probe-User |

## Testing

Mosquitto fuer Integration-Tests installieren (`apt install mosquitto`
auf Linux / `winget install EclipseFoundation.Mosquitto` auf Windows),
dann:

```bash
mosquitto -p 11883 -v &
pytest                           # alle Tests
pytest -m integration            # nur Integration-Tests
```

Details: [`docs/testing.md`](docs/testing.md) und
[`docs/quick-test-real-hardware.md`](docs/quick-test-real-hardware.md).
