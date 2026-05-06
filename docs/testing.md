# Testing der Humboldt-Probe

Vier Schichten von schnell + isoliert nach langsam + realistisch:

```
   schnell                                                       langsam
     ◄──────────────────────────────────────────────────────────────►
     Unit         Coverage        Integration      Hardware Smoke
     (pytest)    (pytest-cov)    (Mosquitto)       (am Geraet)
```

---

## 1. Unit-Tests

Mit `unittest.mock`. Kein MQTT-Broker, kein PipeWire, kein Display
noetig.

| Datei                   | Was                                                       |
| ----------------------- | --------------------------------------------------------- |
| `tests/test_misc.py`    | Config-Parser, Payload-Validation, JSON-Envelope          |
| `tests/test_methods.py` | Sensor-Funktionen (Linux + Common), Subprocess-Timeouts   |
| `tests/test_probe.py`   | `Probe`-Klasse, MQTT-Callbacks, Thread-Lifecycle          |
| `tests/test_app.py`     | `App.fqdn`-Caching, `--no_tls`-Banner, CLI-Validation     |

```bash
pip install -r requirements-dev.txt
pytest                                  # alle Tests
pytest tests/test_probe.py              # ein File
pytest -k "timeout"                     # Substring-Filter
pytest -x --pdb                         # bei Fail in PDB-Debugger
```

`pyproject.toml` definiert `testpaths`, `pythonpath` und
`DeprecationWarning` als Error.

### Was Unit-Tests nicht abdecken

- Echtes MQTT-Verhalten (Reconnect, QoS, Retained Messages)
- Hardware-Edge-Cases (kaputter wpctl-Output, ungewoehnliche LHM-Sensoren)
- systemd / NSSM Lifecycle

---

## 2. Coverage

```bash
pytest --cov --cov-report=term-missing
pytest --cov --cov-report=html         # htmlcov/index.html
```

`_win32.py` ist via `[tool.coverage.run].omit` rausgenommen — auf
Linux/macOS-CI nie ausfuehrbar.

---

## 3. Integration-Tests

`tests/test_integration.py`: 11 Tests die echte MQTT-Roundtrips
verifizieren — Probe-Subprocess gegen externen Mosquitto.

| Test                                                        | Verifiziert                                                   |
| ----------------------------------------------------------- | ------------------------------------------------------------- |
| `test_probe_publishes_connected_retained`                   | retained `connected="1"` + retain-flag                        |
| `test_probe_publishes_capabilities_retained`                | retained capabilities CSV                                     |
| `test_probe_publishes_boot_time_retained`                   | retained boot_time                                            |
| `test_probe_periodic_cycle_publishes_sensors`               | 8s-Window: ping/uptime/errors/boot_time auf Bus               |
| `test_command_ping_roundtrip`                               | manager-command → received + complete                         |
| `test_command_blocked_returns_method_not_allowed`           | Capability-Gate: shutdown abgewiesen                          |
| `test_capability_gate_blocks_module_attribute`              | `os`-Attack-Pfad abgewiesen                                   |
| `test_whitelist_gate_blocks_module_attribute`               | `os` in capabilities → "Unknown method"                       |
| `test_last_will_published_on_unclean_disconnect`            | SIGKILL → connected="0" via Will                              |
| `test_probe_exponential_backoff_on_dead_broker`             | Reconnect-Backoff 5s→10s→20s aus App-Logs                     |
| `test_probe_connects_via_tls`                               | mTLS-Handshake mit ephemerer self-signed CA                   |

Tests skippen automatisch wenn kein Broker erreichbar ist.

### Broker starten

```bash
# macOS:        brew install mosquitto && mosquitto -p 11883 -v
# Debian/Ubu:   sudo apt install mosquitto && mosquitto -p 11883 -v
# Windows:      winget install EclipseFoundation.Mosquitto
#               mosquitto -p 11883 -v

pytest -m integration

# Externer Broker (Staging):
PROBE_TEST_BROKER=staging.mqtt PROBE_TEST_PORT=1883 pytest -m integration
```

### Manuelle Debug-Session

Wenn ein Integration-Test failed, Schritte hand-by-hand ausfuehren:

```bash
# Terminal 1: Broker
mosquitto -p 1883 -v

# Terminal 2: Probe gegen lokalen Broker
cd src
python app.py --config_file ../userconfig.example.txt \
    --mqtt_hostname 127.0.0.1 --no_tls --loglevel DEBUG

# Terminal 3: Topics beobachten
mosquitto_sub -h 127.0.0.1 -p 1883 -t 'probe/#' -v

# Terminal 4: Commands schicken
mosquitto_pub -h 127.0.0.1 -p 1883 -t 'manager/<fqdn>/mute' -m ''
mosquitto_pub -h 127.0.0.1 -p 1883 -t 'manager/<fqdn>/eval' -m ''   # erwarte "Method not allowed"
```

Erwartete Topic-Sequenz nach Connect:

```
probe/<fqdn>/connected         "1"           retained, qos=1
probe/<fqdn>/capabilities      "wake,..."    retained, qos=1
probe/<fqdn>/boot_time         {"data":...}  retained, qos=1
probe/<fqdn>/temperatures      {...}         alle 5s
probe/<fqdn>/fans              {...}
probe/<fqdn>/uptime            {...}
probe/<fqdn>/display           {...}
probe/<fqdn>/is_muted          {...}
probe/<fqdn>/errors            {...}         Status-Aggregation einmal pro Cycle
```

`scripts/smoke-test.sh` automatisiert die Schritte.

### Last-Will testen

Probe brutal killen (`kill -9 <pid>`) waehrend ein zweiter
`mosquitto_sub` laeuft. Nach ~60s sollte:

```
probe/<fqdn>/connected   "0"   ← vom Broker gepusht, retained
```

Bei sauberem `Ctrl-C` wird der Will *nicht* getriggert — `connected`
bleibt `"1"`.

### Reconnect-Backoff testen

```bash
python app.py --config_file ../userconfig.example.txt \
    --mqtt_hostname 127.0.0.1 --mqtt_port 9999 --no_tls --loglevel INFO
```

Erwartete Logs: `Setup failed, retrying in 5s` → `10s` → `20s` → `40s`
→ `60s` (cap bei `BACKOFF_MAX`).

---

## 4. Hardware-Smoke-Tests

Auf der Ziel-Hardware. Verifiziert was nur mit echter Hardware reagiert.

### Linux

```bash
ssh kiosk-01 "cd /opt/humboldt-probe && bash scripts/hardware-test-linux.sh"
```

8 Checks (display, uptime, temperatures, fans, audio mute-toggle,
easire, mpv_control wenn vorhanden, sudo-NOPASSWD). Audio-Toggle ist
invasiv — bei Live-Betrieb mit `SKIP_AUDIO=1`.

### Windows

```powershell
.\scripts\hardware-test-windows.ps1
.\scripts\hardware-test-windows.ps1 -SkipAudio   # bei Live-Betrieb
```

7 Checks (Win32-Display, psutil-Uptime, LHM-Temps, LHM-Fans, pycaw
mute-toggle, easire, shutdown.exe).

**Wichtig:** LHM braucht **Administrator-Rechte** fuer Hardware-Sensoren.
Wenn die Probe als Service laeuft, muss der NSSM-Service-User
entsprechende Rechte haben — sonst kommen leere `temperatures()`/`fans()`.

### Manuelle Direkt-Aufrufe

#### Linux

```bash
python -c "from src.methods._linux import is_muted, mute, unmute; \
           print('before:', is_muted()); mute(); print('after mute:', is_muted())"

DISPLAY=:0 python -c "from src.methods._linux import display; print(display())"
python -c "from src.methods._linux import temperatures, fans; print(temperatures(), fans())"
```

#### Windows

```powershell
python -c "from src.methods._win32 import is_muted; print(is_muted())"
python -c "from src.methods._win32 import temperatures, fans; print(temperatures())"
python -c "from src.methods._win32 import display; print(display())"
```

---

## 5. Smoke-Test-Skript

`scripts/smoke-test.sh` automatisiert die Mosquitto-Integration-Schritte
(Sektion 3). Vor jedem Deploy auf einem Kiosk-PC laufen lassen.

```bash
./scripts/smoke-test.sh <broker-host>
```

---

## 6. Debug-Helfer

### Log-Level live anpassen (Linux)

```bash
sudo systemctl edit humboldt-probe
# Im override:
[Service]
ExecStart=
ExecStart=/usr/bin/python3 src/app.py ... --loglevel=DEBUG

sudo systemctl restart humboldt-probe
journalctl -u humboldt-probe -f
```

### MQTT-Traffic mitschneiden

```bash
mosquitto_sub -h <broker> -p 8883 \
    --cafile ca.pem --cert client.pem --key client.key \
    -t 'probe/<fqdn>/#' -v | grep --line-buffered errors
```

---

## 7. Wenn Tests fehlschlagen

1. **`DeprecationWarning: ...`** — Library hat Pfad deprecated. Code
   aktualisieren oder Pin anpassen.
2. **`subprocess.TimeoutExpired`** — Tool unresponsive. Lokal fast
   immer X-Server / PipeWire / mpv-IPC nicht erreichbar.
3. **`AttributeError: ... has no attribute 'sensors_temperatures'`** —
   psutil-Funktion nicht auf der Plattform verfuegbar (z.B. macOS).
   Tests sind gemockt, in Production greift `_stub`.
4. **`paho.mqtt.client.Client` Callback-Signatur-Fehler** — paho-mqtt
   1.x vs 2.x Mismatch. Wir sind auf 2.x mit `VERSION2`.

---

## 8. Konvention fuer neue Tests

- Test-Datei pro src-Modul: `test_<modul>.py`
- Eine Test-Funktion pro Verhalten, nicht pro Methode
- Test-Namen: `test_<funktion>_<bedingung>_<erwartung>`
- Sensor-Funktionen auf Plattform-Modul-Ebene patchen:
  ```python
  @patch('methods._linux.subprocess.check_output')
  ```
- Probe-Methoden auf `methods.<name>`-Ebene (Late-Binding):
  ```python
  @patch('methods.display', return_value='1920x1080, 60 Hz')
  ```
- Thread-Tests: immer Timeout fuer `Probe.join(timeout=2)` — sonst
  haengt die Suite wenn ein Thread nicht terminiert.

---

## TL;DR

```bash
pytest --cov --cov-report=term-missing      # alles + Coverage
pytest -k "test_on_message_blocked" -v      # einzelner Test
./scripts/smoke-test.sh <broker-host>       # vor Deploy
journalctl -u humboldt-probe -f             # Live-Tail
mosquitto_sub -h <broker> -t 'probe/#' -v   # MQTT-Traffic
```
