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

# einmalig, ONLINE: gesamtes Offline-Bundle laden (Python, shawl, Wheels, mosquitto, Git)
.\scripts\prepare-offline.ps1
pip install -r requirements-dev.txt   # Dev-/Test-Deps (PyPI)

# Test-Tools aus dem Bundle installieren (offline, bei Bedarf):
#   installers\mosquitto-*-install-windows-x64.exe   (Broker)
#   installers\Git-*-64-bit.exe                      (Git)

# Tests (PowerShell als Administrator — LHM braucht's fuer Sensoren)
mosquitto -p 11883 -v
pytest
.\scripts\hardware-test-windows.ps1
.\scripts\install-windows.ps1 -MqttHostname 127.0.0.1 -NoTls   # Offline-Service-Install testen
```

`-SkipAudio` an `hardware-test-windows.ps1` falls Live-Betrieb laeuft.

---

## TL;DR

| Wo            | Was wird getestet                                   | Setup       |
| ------------- | --------------------------------------------------- | ----------- |
| Linux-PC      | Probe-Logik + MQTT + wpctl + xrandr + psutil-HW     | apt once    |
| Windows-PC    | Probe-Logik + MQTT + pycaw + LHM + Win32 + shawl    | prepare-offline once |

Vor jedem Deploy auf den Kiosk: beide PCs einmal durchlaufen lassen.
