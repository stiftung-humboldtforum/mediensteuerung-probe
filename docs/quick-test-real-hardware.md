# Quick-Test gegen reale Hardware

Pro Plattform am jeweiligen Rechner laufen lassen. Vor jedem Deploy
auf einem Kiosk-PC.

---

## Linux-PC

```bash
git clone https://github.com/stiftung-humboldtforum/mediensteuerung-probe.git
cd mediensteuerung-probe

# einmalig
sudo apt install -y python3-pip mosquitto pipewire wireplumber wpctl x11-utils
pip install -r requirements-dev.txt

# Tests
mosquitto -p 11883 -v &
pytest
bash scripts/hardware-test-linux.sh
```

`hardware-test-linux.sh` togglet Audio. Bei Live-Betrieb:
`SKIP_AUDIO=1 bash scripts/hardware-test-linux.sh`.

---

## Windows-PC

```powershell
git clone https://github.com/stiftung-humboldtforum/mediensteuerung-probe.git
cd mediensteuerung-probe

# einmalig (als Admin)
winget install Python.Python.3.13 --scope machine
winget install NSSM.NSSM EclipseFoundation.Mosquitto Git.Git
pip install -r requirements-dev.txt

# Tests (PowerShell als Administrator — LHM braucht's fuer Sensoren)
mosquitto -p 11883 -v
pytest
.\scripts\hardware-test-windows.ps1
.\scripts\install-windows.ps1 -MqttHostname 127.0.0.1 -NoTls   # Service-Install testen
```

`-SkipAudio` an `hardware-test-windows.ps1` falls Live-Betrieb laeuft.

---

## TL;DR

| Wo            | Was wird getestet                                   | Setup       |
| ------------- | --------------------------------------------------- | ----------- |
| Linux-PC      | Probe-Logik + MQTT + wpctl + xrandr + psutil-HW     | apt once    |
| Windows-PC    | Probe-Logik + MQTT + pycaw + LHM + Win32 + NSSM     | winget once |

Vor jedem Deploy auf den Kiosk: beide PCs einmal durchlaufen lassen.
