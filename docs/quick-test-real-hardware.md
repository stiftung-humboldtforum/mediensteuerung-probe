# Quick-Test gegen reale Hardware

Workflow: Dev auf macOS, dann pro Plattform am jeweiligen Rechner
laufen lassen. Optional Docker als CI-äquivalentes Zwischending.

---

## Auf deinem Linux-PC

```bash
git clone https://github.com/stiftung-humboldtforum/mediensteuerung-probe.git
cd mediensteuerung-probe

# einmalig
sudo apt install -y python3-pip mosquitto pipewire wireplumber wpctl x11-utils
pip install -r requirements-dev.txt

# Tests
pytest                           # 102 Tests — Auto-Broker, alle Code-Pfade
bash scripts/hardware-test-linux.sh    # echte wpctl/xrandr/psutil-Sensoren
```

`hardware-test-linux.sh` ist der invasive Teil — er togglet Audio.
Mit `SKIP_AUDIO=1 bash scripts/hardware-test-linux.sh` ohne Audio-
Toggle laufen.

---

## Auf deinem Windows-PC

```powershell
git clone https://github.com/stiftung-humboldtforum/mediensteuerung-probe.git
cd mediensteuerung-probe

# einmalig (als Admin)
winget install Python.Python.3.13 --scope machine
winget install NSSM.NSSM EclipseFoundation.Mosquitto Git.Git
pip install -r requirements-dev.txt

# Tests (PowerShell als Administrator — LHM braucht's für Sensoren)
pytest                                           # 102 Tests
.\scripts\hardware-test-windows.ps1              # echtes pycaw + LHM + Win32-Display
.\scripts\install-windows.ps1 -MqttHostname 127.0.0.1 -NoTls   # Service-Install testen
```

`-SkipAudio` an `hardware-test-windows.ps1` falls Live-Betrieb läuft.

---

## Auf macOS-Dev (kein Wechsel zum Linux/Windows-PC)

Schneller Sanity-Check vor dem Übertragen — Linux-Code-Pfad in Docker:

```bash
docker compose -f docker-compose.linux-test.yml run --rm probe-test pytest
```

Verifiziert: Code läuft auf echtem Linux-Python, paho-mqtt v2 + Auto-
Broker funktionieren. **Nicht** verifiziert: PipeWire/X11/Hardware-
Sensoren — dafür der Linux-PC oben.

Pure Unit + Integration auf macOS direkt geht auch (`brew install
mosquitto && pytest`), aber `methods/_stub.py` greift dort statt
`_linux.py`/`_win32.py`.

---

## TL;DR

| Wo                | Was wird getestet                                 | Setup        | Run-Zeit |
|-------------------|---------------------------------------------------|--------------|----------|
| macOS-Dev (direkt)| Probe-Logik, MQTT-Roundtrips, _stub-Pfad          | brew once    | ~50s     |
| macOS via Docker  | Probe-Logik, MQTT, _linux.py-Imports              | docker once  | ~50s     |
| **Linux-PC**      | + wpctl, xrandr, psutil-Hardware-Sensoren         | apt once     | ~50s + HW-Tests |
| **Windows-PC**    | + pycaw, LibreHardwareMonitor, Win32, NSSM        | winget once  | ~50s + HW-Tests |

Vor jedem signifikanten Push: macOS oder Docker reichen. Vor jedem
Deploy auf den Kiosk: Linux-PC + Windows-PC einmal durchlaufen lassen.
