# standalone-installer/

Builds a **self-contained offline installer** for the Humboldt-Probe that runs on
an already-running Windows machine (no re-image, no internet) — "a normal
software installer": copy a folder, run `install.cmd`, done.

## Build

```powershell
# 1. offline bundle must exist (once, online): python + shawl + wheels
.\scripts\prepare-offline.ps1

# 2. build the package (TLS certs are a build input -- they are not in this repo)
.\standalone-installer\build-standalone-installer.ps1 -TlsDir <dir-with-certs>
.\standalone-installer\build-standalone-installer.ps1 -TlsDir <dir-with-certs> -Zip
```

`-TlsDir` must contain `ca_certificate.pem`, `client_certificate.pem`,
`client_key.pem` (from the MQTT-broker admin / vault). The build **fails closed**
if any input is missing or the bundled wheels are stale vs `requirements.lock.txt`.

## Use (on the target machine)

1. Copy `HumboldtProbe-Setup\` (or unzip) onto the machine — offline is fine.
2. Optional: edit `config.txt` for a different MQTT broker (default `srv-control-avm:8883`).
3. Run `install.cmd` as administrator (self-elevates on double-click).

Deploys the probe to `C:\humboldt-probe`, then installs Python + deps + the shawl
`HumboldtProbe` service (auto-start, LocalSystem) — all offline.

## How it works (no duplicated logic)

`install.cmd` → `install.ps1` deploys the payload to `C:\humboldt-probe`, hardens
the mTLS key, then hands off to the probe's own
[`scripts/install-windows.ps1`](../scripts/install-windows.ps1) (reused verbatim)
for Python + deps + the shawl service. Deploy is done in the wrapper so
`install-windows.ps1` keeps its "install from a checkout in place" semantics for
developers.

## Files

| File | Role |
|---|---|
| `build-standalone-installer.ps1` | builder (run in the repo) |
| `install.cmd` | package launcher; self-elevates (UAC) |
| `install.ps1` | package wrapper; deploy + key-harden + call `install-windows.ps1` |
| `config.txt.example` | copied into the package as `config.txt` |
| `PACKAGE-README.txt` | copied into the package as `README.txt` |

The built package under `dist/` is **gitignored** (it holds the bundled binaries
and the fleet mTLS key). Treat the package as a secret.
