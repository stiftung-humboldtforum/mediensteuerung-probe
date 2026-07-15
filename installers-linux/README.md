# installers-linux/ — offline install bundle (Linux)

The Humboldt-Probe must be installable on Debian-based machines with **no
internet and no apt repo**. Everything the Linux install needs is fetched here
once by [`scripts/prepare-offline-linux.sh`](../scripts/prepare-offline-linux.sh)
and then consumed fully offline by
[`scripts/install-linux.sh`](../scripts/install-linux.sh) — the Linux
counterpart to [`installers/`](../installers/README.md) (Windows).

The binaries are **gitignored** (large, frequently re-versioned); only this
README is tracked. Resolved versions + SHA256 land in
`bundle.manifest.linux.json`.

## Contents (after `prepare-offline-linux.sh`)

| Path | Source | Notes | Consumer |
|---|---|---|---|
| `python/cpython-3.11.*-…-install_only.tar.gz` | github.com/astral-sh/python-build-standalone | newest 3.11, **SHA256-verified** (build-host arch) | Python runtime |
| `wheels/*.whl` | `pip wheel -r requirements.lock.txt` (built with the bundled Python) | cp311 / manylinux, matches the lock | probe deps |
| `debs/*.deb` | apt's solver, isolated apt-root against the **target** Debian repos | pipewire, wireplumber, x11-xserver-utils, mosquitto-clients + full closure | system packages |
| `bundle.manifest.linux.json` | generated | versions + sha256 + lockHash + target codename/arch | build/install gates |

## Build the bundle (once, on any Debian-based host with internet)

```bash
bash scripts/prepare-offline-linux.sh                       # target Debian 13 (trixie), default
bash scripts/prepare-offline-linux.sh --target-release bookworm   # Debian 12 instead
bash scripts/prepare-offline-linux.sh --offline             # only validate what is present
bash scripts/prepare-offline-linux.sh --force               # re-fetch everything
```

The **build host can be any Debian-based distro** (Debian, Ubuntu, Pop!_OS,
Mint …) — the `.deb` closure is resolved against the **target** Debian release
via an isolated apt-root (empty dpkg status ⇒ apt's own solver picks the full,
correct closure), not against the host's own repos. No container, no chroot.

- Default target: **Debian 13 “trixie”**. Override: `--target-release <codename>`,
  `--mirror <url>`, `--keyring <path>` (env `PROBE_TARGET_RELEASE`).
- On a **non-Debian** host (Ubuntu/Pop!_OS) the Debian archive keyring is needed
  once: `sudo apt-get install debian-archive-keyring`.
- The Python + wheels are built for the **build host's architecture** (amd64 or
  arm64); cross-architecture bundling is out of scope.

## Install offline (no internet)

```bash
sudo bash scripts/install-linux.sh   # installs .debs + Python + deps + service
```

`mosquitto-clients` is bundled for the smoke-test; the probe runtime itself
needs only `wpctl` (pipewire/wireplumber) and `xrandr` (x11-xserver-utils).
A full test **broker** (`mosquitto`) is dev-only — `apt install mosquitto`.
