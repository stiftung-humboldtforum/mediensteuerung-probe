Humboldt-Probe -- offline installer (Linux)
===========================================

Install the Humboldt-Probe (MQTT monitoring agent) on an already-running
Debian machine (the release this package was built for -- see below).
No internet, no apt repo required --
Python, all pip dependencies, the system packages (pipewire/wireplumber/
xrandr/mosquitto-clients + their dependency closure), the probe itself and
the TLS material are all bundled in this folder.

USAGE
-----
1. Copy this whole folder (or the .tar.gz) to the target machine and extract.
2. (Optional) edit config.env to point at a different MQTT broker.
   Default is srv-control-avm:8883.
3. Run as root:   sudo ./install.sh
   (install.sh re-execs itself with sudo if you forget.)
4. Wait for "<service> is ACTIVE". The humboldt-probe systemd service is then
   installed, enabled at boot, and running.

WHAT IT DOES
------------
- installs the bundled .deb closure with dpkg (pipewire, wireplumber, xrandr,
  mosquitto-clients + all dependencies) -- offline
- deploys the probe to /opt/humboldt-probe (src, scripts) and the config +
  certs to /etc/humboldt-probe (client_key.pem hardened to 0600)
- unpacks the bundled standalone Python to /opt/humboldt-probe/python and
  installs the probe's pip dependencies into it, offline (from the wheels)
- creates the system user 'probe', a NOPASSWD sudoers drop-in for
  shutdown/reboot, and registers the 'humboldt-probe' systemd service
  (Type=notify, WatchdogSec, auto-restart, enabled at boot)

REQUIREMENTS ON THE TARGET
--------------------------
- The Debian release the bundle was built for (default Debian 13 "trixie";
  shown in installers-linux/bundle.manifest.linux.json), matching amd64/arm64.
  The installer refuses a different release (the bundled .debs are release-
  specific). Override: IGNORE_CODENAME=1.
- If the box's logged-in kiosk user is NOT uid 1000, re-run the service
  installer with --kiosk-uid <uid> (display/audio sensors read that session).
- The display + audio sensors read the kiosk user's session (/run/user/<uid>,
  mode 0700). The service runs as 'probe' by default, which cannot enter that
  dir -- so is_muted/display may stay empty while MQTT/temperatures/fans/uptime
  work. If you need audio/display, run the service AS the kiosk login user:
  add PROBE_USER=<kiosk-user> before ./install.sh, or re-run
  scripts/install-linux.sh --probe-user <kiosk-user>.

AFTERWARDS
----------
  systemctl status humboldt-probe
  journalctl -u humboldt-probe -f
  bash /opt/humboldt-probe/scripts/smoke-test.sh <broker-host>

SECURITY
--------
This folder contains the fleet-wide MQTT client private key. Treat it like a
credential: keep it access-controlled, and delete it after use on shared
machines.
