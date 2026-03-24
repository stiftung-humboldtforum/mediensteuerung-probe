# Changelog: avorus-probe (Original) -> humboldt_probe (Linux)

Dieses Dokument beschreibt alle Unterschiede zwischen dem Original (`avorus-probe`) und der
Linux-Deployment-Version (`humboldt_probe`). Nur Code-Dateien werden verglichen — Dateien die
ausschliesslich im Deployment existieren (TLS-Zertifikate, `__pycache__`) sind nicht aufgefuehrt.

---

## Unveraenderte Dateien

- `src/app.py` — identisch, keine Aenderungen
- `src/misc/__init__.py` — identisch, keine Aenderungen

---

## src/probe.py

### 1. Log-Level fuer fehlende Konfiguration: `critical` -> `error`

**Zeilen 37 + 43** — Wenn `PROBE_METHODS` oder `PROBE_CAPABILITIES` nicht in der Konfiguration
gefunden werden, wird statt `logger.critical()` nun `logger.error()` verwendet.

```python
# Original:
logger.critical('No PROBE_METHODS in userconfig.txt')
logger.critical('No PROBE_CAPABILITIES in userconfig.txt')

# Linux:
logger.error('No PROBE_METHODS in userconfig.txt')
logger.error('No PROBE_CAPABILITIES in userconfig.txt')
```

**Grund:** Eine fehlende Konfigurationsvariable ist ein Fehler, aber kein kritischer Systemausfall.
Die Probe laeuft mit Standardwerten weiter.

### 2. Formatierung: Mehrzeilige Ausdruecke auf eine Zeile zusammengefasst

Mehrere `self.client.publish()`-Aufrufe und die `self.methods`-Dictionary-Comprehension wurden
von mehrzeiligen Ausdruecken auf einzelne Zeilen reduziert. Dies betrifft:

- **Zeile 46** — `self.methods` Dict-Comprehension (2 Zeilen -> 1 Zeile)
- **Zeile 72** — `self.client.publish(capabilities)` in `call_methods()`
- **Zeile 82** — `self.client.publish(method result)` in `call_methods()`
- **Zeile 83** — `self.client.publish(errors)` in `call_methods()`
- **Zeile 101** — `self.client.publish(capabilities)` in `on_connect()`
- **Zeile 102** — `self.client.publish(boot_time)` in `on_connect()`

Rein kosmetische Aenderungen ohne funktionale Auswirkung.

---

## src/methods/__init__.py

### 3. Audio-System komplett umgestellt: ALSA/pyalsaaudio -> wpctl (PipeWire/WirePlumber)

Dies ist die groesste funktionale Aenderung. Die gesamte Audio-Steuerung unter Linux wurde
von der ALSA-Abstraktion (pyalsaaudio) auf direkte WirePlumber-Kommandozeilen-Aufrufe (`wpctl`)
umgestellt.

#### 3a. Funktion `get_audio_device()` entfernt

Die komplette Funktion wurde geloescht. Sie erkannte per `alsaaudio.pcms()`, ob PipeWire
oder PulseAudio als Backend lief, und gab den jeweiligen Device-Namen zurueck.

```python
# Entfernt:
def get_audio_device():
    try:
        from alsaaudio import pcms
        pcms = pcms()
        if 'pipewire' in pcms or 'pulseaudio' in pcms:
            return 'pipewire' if 'pipewire' in pcms else 'pulseaudio'
        else:
            return False
    except:
        False
```

#### 3b. `is_muted()` — Linux-Pfad komplett neu implementiert

```python
# Original (ALSA mit mpv_control-Fallback):
def is_muted():
    if platform.system() == 'Linux':
        device = get_audio_device()
        if device:
            from alsaaudio import Mixer
            mixer = Mixer(device=device)
            return mixer.getmute()[0]
        else:
            p = subprocess.run(
                'mpv_control get_mute', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
            if p.returncode == 0:
                return int(p.stdout.strip().decode())
    else:
        result = subprocess.run(
            ['sc', 'query', 'audiosrv'], capture_output=True, text=True)
        return 'RUNNING' not in result.stdout

# Linux (wpctl):
def is_muted():
    if platform.system() == 'Linux':
        return "MUTED" in subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]).decode()
    else:
        result = subprocess.run(['sc', 'query', 'audiosrv'], capture_output=True, text=True)
        return 'RUNNING' not in result.stdout
```

#### 3c. `mute()` — Linux-Pfad komplett neu implementiert

```python
# Original (ALSA mit mpv_control-Fallback):
def mute():
    if platform.system() == 'Linux':
        device = get_audio_device()
        if device:
            from alsaaudio import Mixer
            mixer = Mixer(device=device)
            mixer.setmute(True, 0)
        else:
            subprocess.run('mpv_control set_mute 1',
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    else:
        subprocess.run('net stop audiosrv')

# Linux (wpctl):
def mute():
    if platform.system() == 'Linux':
        subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
    else:
        subprocess.run('net stop audiosrv')
```

#### 3d. `unmute()` — Linux-Pfad komplett neu implementiert

```python
# Original (ALSA mit mpv_control-Fallback):
def unmute():
    if platform.system() == 'Linux':
        device = get_audio_device()
        if device:
            from alsaaudio import Mixer
            mixer = Mixer(device=device)
            mixer.setmute(False, 0)
        else:
            subprocess.run('mpv_control set_mute 0',
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    else:
        subprocess.run('net start audiosrv')

# Linux (wpctl):
def unmute():
    if platform.system() == 'Linux':
        subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
    else:
        subprocess.run('net start audiosrv')
```

### 4. Shutdown/Reboot: `sudo` hinzugefuegt

```python
# Original:
result = os.system('shutdown now')
result = os.system('reboot now')

# Linux:
result = os.system('sudo shutdown now')
result = os.system('sudo reboot now')
```

**Grund:** Die Probe laeuft nicht als Root-Benutzer und benoetigt `sudo` fuer Systembefehle.

### 5. Formatierung: Leerzeilen zwischen Funktionen reduziert

Doppelte Leerzeilen (`\n\n`) zwischen Funktionsdefinitionen wurden auf einfache Leerzeilen
reduziert. Rein kosmetisch.

---

## src/methods/sensors.py

### 6. `display()` — xrandr-Aufruf geaendert

```python
# Original:
p = subprocess.run(
    'xrandr', env={'DISPLAY': ':0'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

# Linux:
p = subprocess.run('xrandr --current', env={'DISPLAY': ':0'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
```

Aenderungen:
- `'xrandr'` -> `'xrandr --current'` (Flag `--current` hinzugefuegt — vermeidet vollstaendiges
  Probing aller Ausgaenge, liefert nur aktuelle Konfiguration)
- `shell=True` hinzugefuegt (noetig, da der Befehl jetzt als String mit Argumenten uebergeben wird)

### 7. `mpv_file_pos_sec()` — Fehlerbehandlung geaendert

```python
# Original:
def mpv_file_pos_sec():
    p = subprocess.run('mpv_control file_pos_sec',
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    if p.returncode == 0:
        try:
            return int(p.stdout.strip().decode())
        except:
            return 0

# Linux:
def mpv_file_pos_sec():
    p = subprocess.run('mpv_control file_pos_sec', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    if p.returncode == 0:
        return int(p.stdout.strip().decode())
```

Aenderungen:
- `try/except` Block mit `return 0` Fallback entfernt
- Bei fehlgeschlagenem Parsen wird jetzt eine Exception geworfen statt `0` zurueckzugeben
- Bei `returncode != 0` wird implizit `None` zurueckgegeben (statt `0`)
- Subprocess-Argumente auf eine Zeile zusammengefasst

### 8. `easire()` — Formatierung

Subprocess-Argumente auf eine Zeile zusammengefasst. Keine funktionale Aenderung.

### 9. Formatierung: Leerzeilen zwischen Funktionen reduziert

Doppelte Leerzeilen zwischen Funktionsdefinitionen auf einfache Leerzeilen reduziert.
Rein kosmetisch.

---

## requirements.txt

Nicht vorhanden in der Linux-Version (`humboldt_probe`). Die Abhaengigkeit `pyalsaaudio`
wird nicht mehr benoetigt, da die Audio-Steuerung ueber `wpctl` (System-Tool) laeuft.

---

## Zusammenfassung

| Nr. | Datei | Aenderung | Typ |
|-----|-------|-----------|-----|
| 1 | probe.py | `logger.critical` -> `logger.error` | Funktional |
| 2 | probe.py | Mehrzeilige Ausdruecke zusammengefasst | Kosmetisch |
| 3a | methods/__init__.py | `get_audio_device()` entfernt | Funktional |
| 3b | methods/__init__.py | `is_muted()` Linux: ALSA -> wpctl | Funktional |
| 3c | methods/__init__.py | `mute()` Linux: ALSA -> wpctl | Funktional |
| 3d | methods/__init__.py | `unmute()` Linux: ALSA -> wpctl | Funktional |
| 4 | methods/__init__.py | `sudo` bei shutdown/reboot | Funktional |
| 5 | methods/__init__.py | Leerzeilen reduziert | Kosmetisch |
| 6 | sensors.py | `xrandr --current` + `shell=True` | Funktional |
| 7 | sensors.py | `mpv_file_pos_sec()` try/except entfernt | Funktional |
| 8 | sensors.py | `easire()` Formatierung | Kosmetisch |
| 9 | sensors.py | Leerzeilen reduziert | Kosmetisch |
