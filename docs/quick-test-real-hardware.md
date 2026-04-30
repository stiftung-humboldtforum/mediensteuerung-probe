# Quick-Test gegen reale Hardware

Drei Stufen, jeweils so nah an Production wie sinnvoll. macOS-Dev kann
alle drei vom selben Mac aus auslösen.

---

## Stufe 1 — Linux-Codepath (Docker, ~30s Setup)

**Was es testet:** Probe-Code läuft auf echtem Linux-Python 3.13
(nicht macOS-Stub), paho-mqtt v2 + Auto-Broker funktionieren auf
Linux, alle 102 Tests grün.

**Was es nicht testet:** kein PipeWire / kein X-Server / keine echten
Hardware-Sensoren — `wpctl`, `xrandr` und psutil-Sensors fallen.

```bash
# einmalig (~30s, baut Image)
docker compose -f docker-compose.linux-test.yml build

# Tests laufen lassen
docker compose -f docker-compose.linux-test.yml run --rm probe-test pytest
```

---

## Stufe 2 — Linux mit Hardware (UTM/Multipass-VM, ~10min Setup, dann reusable)

**Was es zusätzlich testet:** echte PipeWire-Audio (mute/unmute), echtes
xrandr (Display-Mode), psutil-Hardware-Sensoren (Temps/Fans).

### Setup (einmalig)

**Option A: UTM** (https://mac.getutm.app, gratis, Apple-Hypervisor):
1. UTM laden → "Create new VM" → Linux → Ubuntu 24.04 Desktop ARM ISO
2. 2 GB RAM, 20 GB Disk, Ubuntu Standard-Setup durchklicken
3. In der VM: `sudo apt install pipewire wireplumber wpctl x11-utils python3-pip mosquitto git`

**Option B: Multipass** (`brew install multipass`, schneller, aber kein Desktop):
```bash
multipass launch --name kiosk-vm --cpus 2 --memory 2G --disk 20G 24.04
multipass exec kiosk-vm -- sudo apt install -y pipewire wireplumber wpctl python3-pip mosquitto git
# Audio-Tests gehen hier nicht (kein PipeWire-User-Session) — nur Display via xvfb
```

### Tests laufen

```bash
# Repo in die VM bringen
rsync -av --exclude='.venv' --exclude='__pycache__' . kiosk-vm:/home/ubuntu/humboldt-probe/

# Tests + Hardware-Smoke
ssh kiosk-vm 'cd ~/humboldt-probe && pip install -r requirements-dev.txt && pytest && bash scripts/hardware-test-linux.sh'
```

`scripts/hardware-test-linux.sh` verifiziert die echten wpctl/xrandr/
psutil-Sensoren — auf macOS würde das alles fail. Hier muss es grün sein.

---

## Stufe 3 — Windows mit Hardware (UTM/Parallels-VM)

**Was es zusätzlich testet:** pycaw (Audio-Mute via COM), LibreHardwareMonitor
(CPU/GPU-Temps + -Fans), Win32 EnumDisplaySettingsW, NSSM-Service-Install.

### Setup (einmalig)

**Option A: UTM mit Windows 11 ARM** (free, Apple Silicon):
1. Windows 11 ARM ISO über https://mac.getutm.app/gallery laden
2. UTM → "Create new VM" → Windows → ISO auswählen → 4 GB RAM, 64 GB Disk
3. Windows-Standard-Setup durchklicken (Local Account ohne MS-Login geht)
4. In der VM (PowerShell als Admin):
   ```powershell
   winget install Python.Python.3.13 --scope machine
   winget install NSSM.NSSM
   winget install EclipseFoundation.Mosquitto
   winget install Git.Git
   ```

**Option B: Parallels Desktop** (kommerziell, beste Hardware-Pass-Through):
- Audio-Pass-Through funktioniert robuster als UTM → pycaw kann echte
  Mute-Toggle machen

### Tests laufen

```powershell
# Repo in die VM (oder via Shared Folder)
git clone https://github.com/stiftung-humboldtforum/mediensteuerung-probe.git
cd mediensteuerung-probe
pip install -r requirements-dev.txt

# Pytest läuft die Unit-Tests + die _win32-Mock-Tests gegen das echte pycaw/LHM
pytest

# Hardware-Smoke gegen die echte Hardware (als Admin starten — LHM braucht es)
.\scripts\hardware-test-windows.ps1

# Service-Install testen (idempotent, kann gestoppt werden)
.\scripts\install-windows.ps1 -MqttHostname 127.0.0.1 -NoTls
nssm status HumboldtProbe
nssm stop HumboldtProbe
```

---

## TL;DR Cheat Sheet

| Was du wissen willst                          | Stufe | Setup-Zeit | Run-Zeit |
|-----------------------------------------------|-------|------------|----------|
| "Läuft mein Code auf Linux?"                  | 1     | 30 s       | 50 s     |
| "Funktionieren wpctl/xrandr/psutil auf Linux?" | 2     | 10 min einmalig | 1 min |
| "Funktionieren pycaw + LHM auf Windows?"       | 3     | 30 min einmalig | 1 min |

Stufe 1 → einfach `docker compose ... run`. Stufe 2 + 3 brauchen einmal
VM-Setup (`mac.getutm.app`), danach reusable.

**Volltest** (alle drei Stufen) ist ~3 Minuten Run-Zeit nach Setup.
Empfohlen vor jedem signifikanten Push.
