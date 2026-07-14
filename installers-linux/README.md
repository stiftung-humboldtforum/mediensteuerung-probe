# installers-linux/ — offline install bundle (Linux)

The Humboldt-Probe must be installable on Debian 12 (bookworm) x86_64 machines
with **no internet and no apt repo**. Everything the Linux install needs is
fetched here once by
[`scripts/prepare-offline-linux.sh`](../scripts/prepare-offline-linux.sh) and
then consumed fully offline by
[`scripts/install-linux.sh`](../scripts/install-linux.sh) — the Linux
counterpart to [`installers/`](../installers/README.md) (Windows).

The binaries are **gitignored** (large, frequently re-versioned); only this
README is tracked. Resolved versions + SHA256 land in
`bundle.manifest.linux.json`.

## Contents (after `prepare-offline-linux.sh`)

| Path | Source | Notes | Consumer |
|---|---|---|---|
| `python/cpython-3.11.*-x86_64-…-install_only.tar.gz` | github.com/astral-sh/python-build-standalone | newest 3.11, **SHA256-verified** | Python runtime |
| `wheels/*.whl` | `pip wheel -r requirements.lock.txt` (built with the bundled Python) | cp311 / manylinux x86_64, matches the lock | probe deps |
| `debs/*.deb` | `apt-get download` of the recursive closure | pipewire, wireplumber, x11-xserver-utils, mosquitto-clients + all deps (bookworm) | system packages |
| `bundle.manifest.linux.json` | generated | versions + sha256 + lockHash + target codename/arch | build/install gates |

## Build the bundle (once, on a bookworm/amd64 box with internet)

```bash
bash scripts/prepare-offline-linux.sh            # fetch/refresh to newest
bash scripts/prepare-offline-linux.sh --offline  # only validate what is present
bash scripts/prepare-offline-linux.sh --force    # re-fetch everything
```

Build host must be **bookworm/amd64** (the `.deb`s are release-specific, the
wheels are cp311/manylinux x86_64). The script refuses a mismatched host unless
`ALLOW_HOST_MISMATCH=1`. Network-tolerant: if a source is unreachable but the
file is already present, it is kept.

## Install offline (no internet)

```bash
sudo bash scripts/install-linux.sh   # installs .debs + Python + deps + service
```

`mosquitto-clients` is bundled for the smoke-test; the probe runtime itself
needs only `wpctl` (pipewire/wireplumber) and `xrandr` (x11-xserver-utils).
A full test **broker** (`mosquitto`) is dev-only — `apt install mosquitto`.
