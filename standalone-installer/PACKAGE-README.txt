Humboldt-Probe -- offline installer
===================================

Install the Humboldt-Probe (MQTT monitoring agent) on an already-running
Windows machine. No internet required -- Python, all dependencies, shawl,
the probe itself and the TLS material are bundled in this folder.

USAGE
-----
1. Copy this whole folder to the target machine (USB, share, whatever).
2. (Optional) edit config.txt to point at a different MQTT broker.
   Default is srv-control-avm:8883.
3. Right-click install.cmd  ->  "Run as administrator".
   (Or double-click it and confirm the UAC prompt.)
4. Wait for "Installation finished OK". The HumboldtProbe service is then
   installed, set to start automatically, and running.

WHAT IT DOES
------------
- deploys the probe to C:\HumboldtProbe (src, lib, certs; key ACL-hardened)
- installs Python 3.13 (from the bundled installer, if not already present)
- installs the probe's pip dependencies offline (from the bundled wheels)
- registers "HumboldtProbe" as a Windows service via shawl (auto-start,
  restart-on-crash, runs as LocalSystem)

AFTERWARDS
----------
  Get-Service HumboldtProbe
  Get-Content C:\HumboldtProbe\probe_rCURRENT.log -Tail 50

SECURITY
--------
This folder contains the fleet-wide MQTT client private key. Treat it like a
credential: keep it access-controlled, and delete it after use on shared
machines.
