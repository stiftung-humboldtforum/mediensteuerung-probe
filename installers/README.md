# installers/ — offline install bundle

The Humboldt-Probe must be installable on machines with **no internet** and
without `winget`. Everything the Windows install needs is fetched here once by
[`scripts/prepare-offline.ps1`](../scripts/prepare-offline.ps1) and then consumed
fully offline by [`scripts/install-windows.ps1`](../scripts/install-windows.ps1).

The binaries are **gitignored** (large, frequently re-versioned); only this
README is tracked. Resolved versions + SHA256 land in `bundle.manifest.json`.

## Contents (after `prepare-offline.ps1`)

| File | Source | Version | Consumer |
|---|---|---|---|
| `shawl.exe` | github.com/mtkennerly/shawl | v1.9.0, **SHA256-pinned** | service wrapper |
| `python-3.13.x-amd64.exe` | python.org/ftp/python | newest 3.13 patch | Python runtime |
| `wheels/` | `pip download -r requirements.lock.txt` (win_amd64, cp313) | matches the lock | probe deps |
| `mosquitto-*-install-windows-x64.exe` | mosquitto.org | newest | test broker (dev) |
| `Git-*-64-bit.exe` | github.com/git-for-windows/git | latest, **SHA256-verified** | git (dev) |

## Build the bundle (once, online)

```powershell
.\scripts\prepare-offline.ps1            # fetch/refresh everything to newest
.\scripts\prepare-offline.ps1 -Offline   # only validate what is already present
.\scripts\prepare-offline.ps1 -Force      # re-download everything
```

Network-tolerant: if a source is unreachable but the file is already present it
is kept (an offline re-run still validates). `shawl` is version-pinned (bump in
`prepare-offline.ps1`); the wheels are pinned to `requirements.lock.txt`; the
rest resolve to the newest upstream release.

## Install offline (no internet)

```powershell
.\scripts\install-windows.ps1   # installs shawl + Python + deps from here, then the service
```

`mosquitto` and `Git` are **dev/test** tools — install them from the bundled
installers only on a developer machine when needed; the probe service itself
does not require them.
