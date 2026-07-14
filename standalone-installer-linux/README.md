# standalone-installer-linux/ — offline installer package (Linux)

Builds a **self-contained, fully offline** installer for the Humboldt-Probe on
Debian 12 (bookworm) x86_64 — the Linux counterpart to
[`standalone-installer/`](../standalone-installer/README.md) (Windows).

Copy one folder to a target kiosk, run `sudo ./install.sh`, and the probe is a
running, boot-enabled systemd service — with **no internet and no apt repo** on
the target.

## Build the package (once, on a bookworm/amd64 box with internet)

```bash
# 1. Fetch the offline bundle (standalone Python + wheels + .deb closure).
bash scripts/prepare-offline-linux.sh

# 2. Assemble the package (certs come from your vault, NOT the repo).
bash standalone-installer-linux/build-standalone-installer-linux.sh \
    --tls-dir /path/to/tls --tar
```

Output: `standalone-installer-linux/dist/HumboldtProbe-Setup-linux/` (+ `.tar.gz`
with `--tar`). Both are gitignored — the package contains the fleet mTLS key.

> **Build host must match the target release/arch** (bookworm/amd64). The bundled
> `.deb`s are bookworm packages and the wheels are cp311/manylinux x86_64; a
> package built elsewhere will not `dpkg`/`pip --no-index` cleanly on the kiosk.
> A bookworm container works fine as the build host.

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
