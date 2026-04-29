# Testing the Humboldt-Probe

Dieser Guide beschreibt **wie** Probe-Code getestet werden kann — von
schnellen Unit-Tests beim Coden bis zum Hardware-Smoke-Test direkt am
Kiosk-Rechner. Schichten von schnell + isoliert nach langsam + realistisch:

```
   schnell                                                       langsam
     ◄──────────────────────────────────────────────────────────────►
     Unit         Coverage        Integration      Hardware Smoke
     (pytest)    (pytest-cov)    (Mosquitto)       (am Geraet)
```

---

## 1. Unit-Tests (`pytest`)

Der Großteil des Codes ist mit `unittest.mock` testbar — kein MQTT-Broker,
kein PipeWire, kein Display nötig. Jede Test-Datei deckt einen Layer ab:

| Datei | Was wird getestet | Tests |
|-------|-------------------|-------|
| `tests/test_misc.py` | Config-Parser, Payload-Validation, JSON-Envelope | 16 |
| `tests/test_methods.py` | Sensor-Funktionen (Linux + Common), Subprocess-Timeouts | 16 |
| `tests/test_probe.py` | `Probe`-Klasse, MQTT-Callbacks, Thread-Lifecycle, on_message-Whitelist | 30 |
| `tests/test_app.py` | `App.fqdn`-Caching, `--no_tls`-Banner, CLI-Validation | 8 |

### Lokal ausführen

```bash
pip install -r requirements-dev.txt
pytest                                  # alle Tests
pytest tests/test_probe.py              # nur ein File
pytest tests/test_probe.py::test_on_message_blocked   # nur ein Test
pytest -k "timeout"                     # nach Substring im Test-Namen
pytest -x                               # bei erstem Fail abbrechen
pytest -x --pdb                         # bei Fail in PDB-Debugger droppen
```

`pyproject.toml` definiert `testpaths`, `pythonpath` und filtert
`DeprecationWarning` als Error — `pytest` ohne Args ist die kanonische
Form.

### Was die Unit-Tests **nicht** erkennen

- Fehler in echtem MQTT-Verhalten (Reconnect, QoS, Retained Messages)
- Hardware-spezifische Edge-Cases (kaputter wpctl-Output, gepatchte
  xrandr-Versionen, ungewöhnliche LHM-Sensor-Hardware-Namen auf Win)
- Race-Conditions die nur unter Last auftreten
- systemd / NSSM Lifecycle-Verhalten

Dafür sind die nächsten Schichten da.

---

## 2. Coverage-Reports (`pytest-cov`)

Identifiziert ungetestete Branches.

```bash
pytest --cov --cov-report=term-missing
pytest --cov --cov-report=html         # öffne htmlcov/index.html
```

Aktueller Coverage-Stand (Stand v0.2.0, gemessen auf macOS-Dev):

| Modul | Coverage | Lücken |
|-------|----------|--------|
| `misc/__init__.py`    | 100 % | — |
| `probe.py`            |  95 % | nur 5 Zeilen in on_disconnect/on_connect-Logging |
| `methods/__init__.py` |  73 % | mpv_file_pos_sec mit non-zero rc, easire-Exception-Branches |
| `methods/_stub.py`    |  61 % | NoReturn-Stubs (per Definition nie aufgerufen in Tests) |
| `methods/_linux.py`   |  44 % | display-Parsing mit echtem xrandr-Output |
| `app.py`              |  49 % | `App.run()`-Hauptloop (Integration-Test-Domäne) |

`_win32.py` ist via `[tool.coverage.run].omit` rausgenommen — auf
Linux/macOS-CI nie ausführbar, würde 0% drücken.

**Wann reicht 100% nicht:** Hardware-Funktionen können auf jedem System
unterschiedlich verhalten. 100% Branch-Coverage auf macOS sagt nichts
über `wpctl`-Verhalten auf der echten Probe-Hardware.

---

## 3. Integration-Tests (`pytest -m integration`)

`tests/test_integration.py` enthält 8 Tests die die **wirklich
getestet wird ob die Probe ueber MQTT korrekt agiert**:

| Test | Was wird verifiziert |
|------|----------------------|
| `test_probe_publishes_connected_retained` | retained `connected="1"` topic + retain-flag |
| `test_probe_publishes_capabilities_retained` | retained capabilities CSV |
| `test_probe_publishes_boot_time_retained` | retained boot_time mit Unix-Epoch |
| `test_probe_periodic_cycle_publishes_sensors` | Innerhalb 8s sind ping/uptime/errors/boot_time auf dem Bus |
| `test_command_ping_roundtrip` | `manager/<fqdn>/ping` → 'received' + 'complete' Response |
| `test_command_blocked_returns_method_not_allowed` | Capability-Gate live: shutdown wird abgewiesen |
| `test_module_attribute_attack_blocked` | COMMANDS-Whitelist live: 'os' wird nicht aufgerufen |
| `test_last_will_published_on_unclean_disconnect` | SIGKILL → connected="0" via Broker-Will (~25-80s) |

Diese Tests starten den **echten** Probe-Subprocess (`src/app.py`)
gegen einen **echten** Mosquitto und sprechen mit ihm ueber **echtes**
paho-mqtt — kein Mocking. Alles was Unit-Tests nicht abdecken
(Retained, Last-Will, QoS-ACKs, Subscribe-Patterns) wird hier verifiziert.

Tests skippen automatisch wenn kein Broker erreichbar ist (siehe
`tests/conftest.py::pytest_collection_modifyitems`).

### 3a. Lokal: Broker via Docker oder Mosquitto

**Variante A — Docker:**
```bash
docker compose -f docker-compose.test.yml up -d
pytest -m integration
docker compose -f docker-compose.test.yml down
```

**Variante B — Mosquitto direkt:**
```bash
# macOS:  brew install mosquitto
# Debian: sudo apt install mosquitto
mosquitto -p 11883 -v &
pytest -m integration
kill %1
```

**Anderer Broker (z.B. Staging):**
```bash
PROBE_TEST_BROKER=staging.mqtt.example.com PROBE_TEST_PORT=1883 \
  pytest -m integration
```

Der `PROBE_TEST_BROKER:PROBE_TEST_PORT` Env-Var-Override macht die
Tests broker-agnostisch — gleicher Test-Code laeuft gegen lokales
mosquitto, gegen einen Docker-Container oder gegen einen echten
Staging-Broker.

### 3b. Manuelle Beobachtung — was die Tests automatisieren

### 3a. Mosquitto starten

```bash
# macOS
brew install mosquitto
brew services run mosquitto    # oder: mosquitto -p 1883 -v

# Linux
sudo apt install mosquitto mosquitto-clients
mosquitto -p 1883 -v
```

### 3b. Probe lokal starten (gegen lokalen Broker)

```bash
cd src
python app.py \
    --config_file ../userconfig.example.txt \
    --mqtt_hostname 127.0.0.1 \
    --no_tls \
    --loglevel DEBUG
```

Der `--no_tls` Banner sollte als "localhost broker / for local testing
only" erscheinen — wenn er stattdessen die laute Production-Warnung
schreibt, ist im hostname-Check was schief.

### 3c. Sensor-Topics beobachten

In zweitem Terminal:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 -t 'probe/#' -v
```

Erwartet (in dieser Reihenfolge bei Connect):
```
probe/<fqdn>/connected         "1"           ← retained, qos=1
probe/<fqdn>/capabilities      "wake,..."    ← retained, qos=1
probe/<fqdn>/boot_time         {"data":...}  ← retained, qos=1
probe/<fqdn>/temperatures      {...}         ← alle 5s
probe/<fqdn>/fans              {...}
probe/<fqdn>/uptime            {...}
probe/<fqdn>/display           {...}
probe/<fqdn>/is_muted          {...}
probe/<fqdn>/easire            {...}
probe/<fqdn>/mpv_file_pos_sec  {...}
probe/<fqdn>/errors            {...}         ← Status-Aggregation, einmal pro Cycle
```

### 3d. Manager-Commands testen

In drittem Terminal:

```bash
# Erlaubte Commands (in PROBE_CAPABILITIES)
mosquitto_pub -h 127.0.0.1 -p 1883 -t 'manager/<fqdn>/mute' -m ''
mosquitto_pub -h 127.0.0.1 -p 1883 -t 'manager/<fqdn>/unmute' -m ''

# Nicht-erlaubte Commands müssen "Method not allowed" zurückgeben
mosquitto_pub -h 127.0.0.1 -p 1883 -t 'manager/<fqdn>/eval' -m ''

# Modul-Attributzugriff muss "Unknown method" zurückgeben (Whitelist-Check)
mosquitto_pub -h 127.0.0.1 -p 1883 -t 'manager/<fqdn>/os' -m ''
mosquitto_pub -h 127.0.0.1 -p 1883 -t 'manager/<fqdn>/subprocess' -m ''
```

Alles via `scripts/smoke-test.sh` automatisierbar — siehe das Skript.

### 3e. Last-Will testen

Probe brutal killen (`kill -9 <pid>`) während ein zweiter
`mosquitto_sub` läuft. Nach ~60s sollte:

```
probe/<fqdn>/connected   "0"   ← vom Broker gepusht, retained
```

Das ist der MQTT-Last-Will (B5). Bei sauberem `Ctrl-C` wird der
Will *nicht* getriggert (saubere Disconnect-Sequenz) — `connected`
bleibt auf `"1"`.

### 3f. Reconnect-Backoff testen

Probe gegen toten Broker starten:

```bash
python app.py --config_file ../userconfig.example.txt \
    --mqtt_hostname 127.0.0.1 --mqtt_port 9999 --no_tls --loglevel INFO
```

Logs sollten zeigen:
```
... Setup failed, retrying in 5s
... Setup failed, retrying in 10s
... Setup failed, retrying in 20s
... Setup failed, retrying in 40s
... Setup failed, retrying in 60s
... Setup failed, retrying in 60s    ← cap bei BACKOFF_MAX
```

Sobald Mosquitto auf 9999 startet: nächster Retry verbindet, Backoff-Counter
reset auf 5s.

---

## 4. Hardware-Smoke-Tests

Auf der Ziel-Hardware (Kiosk-PC). Diese Tests verifizieren das was nur
mit echter Hardware reagiert.

### Linux (Debian/Ubuntu, PipeWire)

```bash
# Audio
python -c "from src.methods._linux import is_muted, mute, unmute; \
           print('muted before:', is_muted()); \
           mute(); print('muted after mute:', is_muted()); \
           unmute(); print('muted after unmute:', is_muted())"

# Display (mit aktivem X-Server)
DISPLAY=:0 python -c "from src.methods._linux import display; print(display())"
# Erwarte: '1920x1080, 60.00 Hz' (oder ähnlich)

# Temperaturen / Fans (psutil)
python -c "from src.methods._linux import temperatures, fans; \
           print('temps:', temperatures()); \
           print('fans:', fans())"

# Uptime
python -c "from src.methods._linux import uptime; print(uptime())"

# easire (passt nur wenn easire-player läuft)
python -c "from src.methods import easire; print(easire())"
```

### Windows (per OpenSSH oder lokal in PowerShell)

```powershell
# Audio (pycaw)
python -c "from src.methods._win32 import is_muted, mute, unmute; print(is_muted())"

# LibreHardwareMonitor (braucht Admin-Rechte für vollständige Sensoren!)
python -c "from src.methods._win32 import temperatures, fans; print(temperatures())"

# Display (Win32 API)
python -c "from src.methods._win32 import display; print(display())"
```

**Wichtig:** LHM braucht **Administrator-Rechte** für niedrig-Level-
Hardware-Sensoren. Wenn die Probe als Service läuft, muss der NSSM-
Service-User entsprechende Rechte haben — sonst kommen leere
`temperatures()`/`fans()` zurück.

---

## 5. Smoke-Test-Skript

`scripts/smoke-test.sh` automatisiert die Mosquitto-Integration-Schritte
(3c-3d). Vor jedem Deploy auf einem Kiosk-PC mindestens einmal laufen
lassen — fängt config-Fehler und Broker-Connect-Probleme ab bevor man
den Service installiert.

```bash
./scripts/smoke-test.sh 127.0.0.1
```

Erwarte: alle Schritte mit `[OK]` markiert.

---

## 6. CI-Pipeline

`.github/workflows/test.yml` läuft auf jedem Push/PR gegen `main`:

| Job | Was | Wann |
|-----|-----|------|
| pytest (Ubuntu, Python 3.9-3.13) | Unit-Tests + Coverage | jeder Push/PR |
| pytest (macOS, Python 3.12-3.13) | `_stub.py`-Pfad + Import-Sanity | jeder Push/PR |
| Coverage-XML upload | Artifact für Codecov-Integration | nur Ubuntu/3.13 |
| Verify clean import | `python -W error::DeprecationWarning` | jede Matrix-Zeile |

`pyproject.toml` zwingt `DeprecationWarning` zu Errors während `pytest` —
verhindert dass z.B. ein paho-mqtt-Update einen neuen Deprecation-Path
einschleicht ohne dass jemand es merkt.

CI-Failures sind nicht-skippbar (kein `--no-verify` Bypass) — der einzige
Weg an grünen CI vorbei ist Probleme tatsächlich zu fixen.

---

## 7. Debug-Helfer

### Log-Level live anpassen

```bash
sudo systemctl edit humboldt-probe   # für override
# Im override:
[Service]
Environment=PROBE_LOGLEVEL=DEBUG
ExecStart=
ExecStart=/usr/bin/python3 src/app.py ... --loglevel=DEBUG

sudo systemctl restart humboldt-probe
journalctl -u humboldt-probe -f
```

### MQTT-Traffic mitschneiden

`mosquitto_sub` ist alles-oder-nichts. Für gefilterte Sicht:

```bash
mosquitto_sub -h <broker> -p 8883 \
    --cafile ca.pem --cert client.pem --key client.key \
    -t 'probe/<fqdn>/#' -v | grep --line-buffered errors
```

### Probe ohne MQTT-Loop testen

Methoden direkt importieren und ausführen — siehe Hardware-Smoke-Tests
oben. Hilft beim Debuggen wenn die Verbindung selbst funktioniert aber
ein Sensor leere Daten liefert.

---

## 8. Wenn Tests fehlschlagen

1. **`DeprecationWarning: ...`** — Library hat einen Pfad deprecated.
   Wenn nicht von uns: pinning anpassen, sonst Code aktualisieren.
2. **`subprocess.TimeoutExpired`** — Tool unresponsive. Bei lokalen Tests
   fast immer X-Server / PipeWire / mpv-IPC nicht erreichbar.
3. **`AttributeError: ... has no attribute 'sensors_temperatures'`** —
   psutil-Funktion nicht auf der Plattform verfügbar (z.B. macOS).
   Tests schon entsprechend gemockt; in echtem Code nicht relevant
   weil `_stub` greift.
4. **`paho.mqtt.client.Client` Callback-Signatur-Fehler** — paho-mqtt
   1.x vs 2.x Mismatch. Wir sind auf 2.x mit VERSION2 (siehe
   `app.py:_setup`).

---

## 9. Schreiben neuer Tests

### Konvention

- Test-Datei pro src-Modul: `test_<modul>.py`
- Eine Test-Funktion pro Verhalten, nicht pro Methode
- Test-Namen beschreiben Verhalten + Erwartung:
  `test_<funktion>_<bedingung>_<erwartung>`

### Patches richtig setzen

- **Sensor-Funktionen** auf Plattform-Modul-Ebene patchen (greift nur
  auf der jeweiligen Plattform):
  ```python
  @patch('methods._linux.subprocess.check_output')
  ```
- **Probe-Methoden** auf `methods.<name>`-Ebene patchen (das ist die
  Late-Binding-Stelle siehe R5):
  ```python
  @patch('methods.display', return_value='1920x1080, 60 Hz')
  ```
- **App-Methoden** brauchen `from app import App, ...`; die Module-
  Attribute können direkt gepatcht werden.

### Thread-Tests

Probe.start() / Probe.join(timeout=2) — immer Timeout setzen, sonst
hängt die Test-Suite wenn ein Thread nicht terminiert. Verwende
`probe.client = Mock()` für die MQTT-Seite.

---

## TL;DR Cheat Sheet

```bash
# Alles + Coverage
pytest --cov --cov-report=term-missing

# Nur einen Test
pytest -k "test_on_message_blocked" -v

# Vor jedem Deploy
./scripts/smoke-test.sh <broker-host>

# Live-Tail
journalctl -u humboldt-probe -f
mosquitto_sub -h <broker> -t 'probe/#' -v
```
