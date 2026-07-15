# standalone-installer-linux/ — offline installer package (Linux)

Builds a **self-contained, fully offline** installer for the Humboldt-Probe on
a Debian target (default **Debian 13 “trixie”**), amd64 or arm64 — the Linux
counterpart to [`standalone-installer/`](../standalone-installer/README.md)
(Windows).

Copy one folder to a target kiosk, run `sudo ./install.sh`, and the probe is a
running, boot-enabled systemd service — with **no internet and no apt repo** on
the target.

## Build the package (once, on any Debian-based host with internet)

```bash
# 1. Build the offline bundle (standalone Python + wheels + .deb closure).
bash scripts/prepare-offline-linux.sh                     # target Debian 13 (trixie)
#   or e.g. --target-release bookworm for Debian 12.

# 2. Assemble the package (certs come from your vault, NOT the repo).
bash standalone-installer-linux/build-standalone-installer-linux.sh \
    --tls-dir /path/to/tls --tar
```

Output: `standalone-installer-linux/dist/HumboldtProbe-Setup-linux/` (+ `.tar.gz`
with `--tar`). Both are gitignored — the package contains the fleet mTLS key.

> The **build host can be any Debian-based distro** (Debian, Ubuntu, Pop!_OS,
> Mint …): `prepare-offline-linux.sh` resolves the `.deb` closure against the
> **target** Debian release via an isolated apt-root, not the host's repos — no
> container, no chroot. On a non-Debian host, install the Debian keyring once:
> `sudo apt-get install debian-archive-keyring`. The Python + wheels are built
> for the build host's **architecture**, so build on amd64 for amd64 kiosks
> (arm64 for arm64) — cross-arch is out of scope.

## Install on the target (offline)

```bash
# copy + extract the package, then:
sudo ./install.sh
```

`install.sh` self-elevates, deploys the payload to `/opt/humboldt-probe` +
`/etc/humboldt-probe`, hardens the key, then calls
[`scripts/install-linux.sh`](../scripts/install-linux.sh) (reused **verbatim** —
single source of truth for the offline runtime + service setup).

## What ends up on the target

| Path | Content |
|---|---|
| `/opt/humboldt-probe/src` | probe source |
| `/opt/humboldt-probe/python` | bundled standalone Python + installed deps |
| `/etc/humboldt-probe/` | `userconfig.txt` + `ca/client cert` + `client_key.pem` (0600) |
| `/etc/systemd/system/humboldt-probe.service` | rendered unit (Type=notify) |
| `/etc/sudoers.d/humboldt-probe` | NOPASSWD shutdown/reboot for the `probe` user |

Manage it with `systemctl status humboldt-probe` /
`journalctl -u humboldt-probe -f`.
