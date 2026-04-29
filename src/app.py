import sys
import platform
# COM apartment mode must be set before any module touches COM. Because
# the methods package imports pythonnet/LHM (Windows) and pycaw, this
# has to happen at the very top of app.py, before any other imports
# that might transitively pull in win32com / comtypes / pythonnet.
if platform.system() == 'Windows':
    sys.coinit_flags = 0  # COINIT_MULTITHREADED — match pythonnet/LHM

import time
import socket
import logging
from typing import Optional

import click
import paho.mqtt.client as mqtt
try:
    import sd_notify
except ImportError:
    sd_notify = None


from probe import Probe
from misc import get_config, logger


class FqdnChanged(RuntimeError):
    """Raised when socket.getfqdn() returns a different value than at
    init. The run-Loop catches it and rebuilds the MQTT client with
    the new identity."""


class App:
    """Outer lifecycle: setup MQTT-Client + Probe-Thread, connect, then
    sit in a watchdog-pinging loop while the Probe runs. On disconnect
    or any failure: stop, exponential-backoff, retry.
    """

    def __init__(
        self,
        config: dict,
        mqtt_hostname: str,
        mqtt_port: int,
        ca_certificate: Optional[str],
        certfile: Optional[str],
        keyfile: Optional[str],
        no_tls: bool,
        notify,  # sd_notify.Notifier | None
    ) -> None:
        self.mqtt_hostname = mqtt_hostname
        self.mqtt_port = mqtt_port
        self.config = config
        self.ca_certificate = ca_certificate
        self.certfile = certfile
        self.keyfile = keyfile
        self.no_tls = no_tls
        self.notify = notify
        self.notify_enabled = notify.enabled() if notify is not None else False
        self._fqdn = socket.getfqdn()

    @property
    def fqdn(self) -> str:
        """Cached FQDN. Use _refresh_fqdn() to re-resolve."""
        return self._fqdn

    def _refresh_fqdn(self) -> None:
        """Re-resolve FQDN via DNS. Raises FqdnChanged if it changed —
        the run-Loop fängt das, baut einen neuen Client (mit der neuen
        Identity) und reconnected.

        Side-effect-frei (kein stop, kein logger.exception). Wird nur
        in _setup() aufgerufen, nicht in der inner-while-Loop —
        DNS-Lookup pro 5s-Cycle war Verschwendung.
        """
        current = socket.getfqdn()
        if current != self._fqdn:
            old = self._fqdn
            self._fqdn = current
            raise FqdnChanged(f'FQDN changed: {old} → {current}')

    def _setup(self) -> None:
        """Build a fresh mqtt.Client + Probe pair. Runs once per
        reconnect cycle. May raise FqdnChanged or any TLS / config-
        related exception — caller (run) catches and retries with
        backoff."""
        self._refresh_fqdn()
        logger.info('FQDN: %s', self.fqdn)
        self.mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.fqdn,
        )
        self.mqtt_client.enable_logger(logger)

        # Last Will: when the probe drops unexpectedly the broker
        # publishes this to inform the manager. retained so newly
        # subscribing managers see the current state.
        self.mqtt_client.will_set(
            f'probe/{self.fqdn}/connected',
            payload='0',
            qos=1,
            retain=True,
        )

        if not self.no_tls:
            logger.info('MQTT TLS: %s %s %s', self.ca_certificate, self.certfile, self.keyfile)
            self.mqtt_client.tls_set(
                self.ca_certificate,
                self.certfile,
                self.keyfile
            )

        self.probe = Probe(self.fqdn,
                           client=self.mqtt_client,
                           config=self.config)

    BACKOFF_INITIAL = 5
    BACKOFF_MAX = 60

    def run(self) -> None:
        """Main lifecycle loop. Exits only via SIGTERM/SIGKILL — the
        outer service manager (systemd / NSSM) is responsible for
        eventual stops."""
        backoff = self.BACKOFF_INITIAL
        while True:
            try:
                self._setup()
            except Exception as e:
                logger.exception(e)
                logger.warning('Setup failed, retrying in %ds', backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, self.BACKOFF_MAX)
                continue

            try:
                self.probe.start()

                if self.notify_enabled:
                    self.notify.status('Connecting...')

                logger.info('Connecting MQTT Host: %s:%s', self.mqtt_hostname, self.mqtt_port)
                self.mqtt_client.connect(self.mqtt_hostname, self.mqtt_port, 60)
                self.mqtt_client.loop_start()

                # Warte auf das tatsaechliche on_connect-Callback statt
                # blindem time.sleep(3). Auf langsamen Brokern wuerde
                # sleep(3) zu frueh enden und die while-Loop sofort
                # rauswerfen → Reconnect-Flap.
                if not self.probe.connected_event.wait(timeout=15):
                    logger.error('MQTT connect handshake timed out after 15s')
                    raise TimeoutError('MQTT connect timeout')

                # Erfolgreicher Handshake → backoff-Counter zuruecksetzen
                backoff = self.BACKOFF_INITIAL

                if self.notify_enabled:
                    self.notify.ready()
                    self.notify.status('Connected.')
                    self.notify.notify()

                last_heartbeat = self.probe.heartbeat
                stalled_cycles = 0
                while self.probe.is_connected:
                    time.sleep(5)
                    current_heartbeat = self.probe.heartbeat
                    if current_heartbeat != last_heartbeat:
                        last_heartbeat = current_heartbeat
                        stalled_cycles = 0
                        if self.notify_enabled:
                            self.notify.notify()
                    else:
                        stalled_cycles += 1
                        logger.warning('Probe heartbeat stalled (%d cycles); withholding sd_notify watchdog ping', stalled_cycles)

                logger.debug('Probe is not connected')
            except Exception as e:
                logger.exception(e)
                if self.notify_enabled:
                    # Exception-Typ und Kurzbeschreibung statt generischem
                    # 'Failed.' — der Manager sieht ueber sd_notify den
                    # Statustext und kann unterschiedliche Failure-Modi
                    # auseinanderhalten (TimeoutError vs ConnectionRefused
                    # vs CertificateError).
                    detail = f'{type(e).__name__}: {e}'[:200]
                    self.notify.status(f'Failed: {detail}')
                    self.notify.notify()

            self.stop()
            logger.info('Reconnect in %ds', backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, self.BACKOFF_MAX)

    def stop(self) -> None:
        """Tear down current cycle: disconnect MQTT, signal Probe-
        Thread to stop, join it. Idempotent — safe to call when
        attributes don't yet exist (early Setup-failure)."""
        if hasattr(self, 'mqtt_client') and self.mqtt_client.is_connected():
            self.mqtt_client.disconnect()
        if hasattr(self, 'probe') and self.probe.is_alive():
            self.probe.stop()
            self.probe.join()


@click.command()
@click.option('--config_file', type=str, required=True)
@click.option('--mqtt_hostname', type=str, required=True)
@click.option('--mqtt_port', type=int, default=None)
@click.option('--ca_certificate', type=str, required=False)
@click.option('--certfile', type=str, required=False)
@click.option('--keyfile', type=str, required=False)
@click.option('--no_tls', is_flag=True, default=False)
@click.option('--loglevel', type=click.Choice(['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'], case_sensitive=False), default='INFO')
def main(
        config_file,
        mqtt_hostname,
        mqtt_port,
        ca_certificate,
        certfile,
        keyfile,
        no_tls,
        loglevel):

    logging.basicConfig(
        level=getattr(logging, loglevel.upper(), logging.WARNING),
        format='%(asctime)s %(levelname)s %(filename)s:%(lineno)d %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z',
    )

    if not no_tls and not all([ca_certificate, certfile, keyfile]):
        raise click.UsageError('--ca_certificate, --certfile, and --keyfile are required unless --no_tls is set.')

    if mqtt_port is None:
        mqtt_port = 1883 if no_tls else 8883

    if no_tls:
        is_local = mqtt_hostname in ('localhost', '127.0.0.1', '::1')
        banner = '*' * 70
        if is_local:
            logger.warning('%s', banner)
            logger.warning('--no_tls active (localhost broker). For local testing only.')
            logger.warning('%s', banner)
        else:
            logger.warning('%s', banner)
            logger.warning('--no_tls active against non-local broker %s — NO AUTH, NO ENCRYPTION.', mqtt_hostname)
            logger.warning('Anyone on the network can issue probe commands. Production deployments MUST use TLS.')
            logger.warning('%s', banner)

    if sd_notify is not None:
        notify = sd_notify.Notifier()
    else:
        notify = None

    if notify is not None and notify.enabled():
        notify.status('Startup...')

    config = get_config(config_file)
    logger.info('Config: %s', config)

    app = App(config,
              mqtt_hostname,
              mqtt_port,
              ca_certificate,
              certfile,
              keyfile,
              no_tls,
              notify)
    app.run()


if __name__ == '__main__':
    main()
