import sys
import platform
# COM apartment mode must be set before any module touches COM. Because
# the methods package imports pythonnet/LHM (Windows) and pycaw, this
# has to happen at the very top of app.py, before any other imports
# that might transitively pull in win32com / comtypes / pythonnet.
if platform.system() == 'Windows':
    sys.coinit_flags = 0  # COINIT_MULTITHREADED — match pythonnet/LHM

import os
import ssl
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
    init. The run-Loop catches it, tears down the old probe, and
    rebuilds the MQTT client with the new identity. The new FQDN is
    carried as the second positional arg so the caller can install it
    after a clean teardown of the old probe."""

    def __init__(self, message: str, new_fqdn: str) -> None:
        super().__init__(message)
        self.new_fqdn = new_fqdn


class App:
    """Outer lifecycle: setup MQTT-Client + Probe-Thread, connect, then
    sit in a watchdog-pinging loop while the Probe runs. On disconnect
    or any failure: stop, exponential-backoff, retry.
    """

    # Reconnect backoff: each failed setup/connect doubles the wait,
    # capped at BACKOFF_MAX.
    BACKOFF_INITIAL = 5
    BACKOFF_MAX = 60

    # Maximum chunk of sleep during reconnect-backoff before we ping
    # sd_notify. Must be < systemd's WatchdogSec (30s in the reference
    # unit) — otherwise systemd would mark the service stalled and
    # restart it mid-backoff, defeating the point of exponential backoff.
    BACKOFF_NOTIFY_INTERVAL = 15

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

        Wird nur in _setup() aufgerufen, nicht in der inner-while-Loop —
        DNS-Lookup pro 5s-Cycle war Verschwendung.

        Atomar: self._fqdn wird ERST nach erfolgreichem Setup gewechselt.
        So bleibt die alte FQDN gueltig falls der naechste _setup() eben-
        falls fehlschlaegt — kein Verlust der Identity-Konsistenz fuer
        das (noch laufende) alte Probe-Objekt.
        """
        current = socket.getfqdn()
        if current != self._fqdn:
            raise FqdnChanged(f'FQDN changed: {self._fqdn} → {current}', current)

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
        # Bound the in-flight + queued message counts so a long broker
        # outage does not let the paho client buffer grow without limit
        # (Probe.call_methods publishes ~10 topics every 5s; over an
        # hour-long disconnect that is ~7000 buffered messages).
        self.mqtt_client.max_inflight_messages_set(20)
        self.mqtt_client.max_queued_messages_set(200)

        # Last Will: when the probe drops unexpectedly the broker
        # publishes this to inform the manager. retained so newly
        # subscribing managers see the current state.
        self.mqtt_client.will_set(
            f'probe/{self.fqdn}/v1/connected',
            payload='0',
            qos=1,
            retain=True,
        )

        if not self.no_tls:
            logger.debug('MQTT TLS material: ca=%s cert=%s key=%s', self.ca_certificate, self.certfile, self.keyfile)
            self.mqtt_client.tls_set(
                self.ca_certificate,
                self.certfile,
                self.keyfile,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        self.probe = Probe(self.fqdn,
                           client=self.mqtt_client,
                           config=self.config)

    def _backoff_sleep(self, seconds: int) -> None:
        """Sleep `seconds` total, but ping sd_notify every
        BACKOFF_NOTIFY_INTERVAL so the systemd Watchdog (WatchdogSec=30s)
        does not trip during long retry-windows. Without this, a 60s
        backoff would force systemd to restart the unit mid-sleep and
        nullify exponential backoff."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            chunk = min(self.BACKOFF_NOTIFY_INTERVAL, remaining)
            time.sleep(chunk)
            if self.notify_enabled:
                self.notify.notify()

    def run(self) -> None:
        """Main lifecycle loop. Exits via SIGTERM/SIGKILL under a
        service-manager (systemd / NSSM), or via KeyboardInterrupt /
        SystemExit during interactive use. The outer try/finally
        guarantees a clean stop() in every exit path so no thread or
        socket leaks behind."""
        backoff = self.BACKOFF_INITIAL
        try:
            self._run_loop(backoff)
        finally:
            self.stop()

    def _run_loop(self, backoff: int) -> None:
        """Inner loop — split out so the outer run() can wrap it in a
        try/finally without growing the indentation of every except-
        branch by one level."""
        while True:
            try:
                self._setup()
            except FqdnChanged as e:
                # Old probe (if any) is still bound to the previous FQDN.
                # Tear it down cleanly before installing the new identity,
                # so the next _setup() builds with the correct value.
                logger.warning('%s — rebuilding identity', e)
                self.stop()
                self._fqdn = e.new_fqdn
                continue  # immediate retry, no backoff for identity churn
            except Exception as e:
                logger.exception('Setup error: %s', e)
                logger.warning('Setup failed, retrying in %ds', backoff)
                if self.notify_enabled:
                    detail = f'{type(e).__name__}: {e}'[:200]
                    self.notify.status(f'Setup failed: {detail}')
                    self.notify.notify()
                self._backoff_sleep(backoff)
                backoff = min(backoff * 2, self.BACKOFF_MAX)
                continue

            try:
                if self.notify_enabled:
                    self.notify.status('Connecting...')

                logger.info('Connecting MQTT Host: %s:%s', self.mqtt_hostname, self.mqtt_port)
                # MQTT keepalive default 60s. Last-Will fires after
                # ~1.5x keepalive (= 90s broker default). Tests set
                # PROBE_MQTT_KEEPALIVE=5 for a faster Last-Will trigger.
                # Garbage env-values fall back to the default instead
                # of killing _setup() in a ValueError loop.
                keepalive_raw = os.environ.get('PROBE_MQTT_KEEPALIVE', '60')
                try:
                    keepalive = int(keepalive_raw)
                except ValueError:
                    logger.warning('Invalid PROBE_MQTT_KEEPALIVE=%r — falling back to 60s', keepalive_raw)
                    keepalive = 60
                self.mqtt_client.connect(self.mqtt_hostname, self.mqtt_port, keepalive)
                self.mqtt_client.loop_start()

                # Wait for the actual on_connect callback rather than a
                # blind time.sleep(3). On slow brokers a fixed sleep
                # would end too early and the outer loop would flap.
                if not self.probe.connected_event.wait(timeout=15):
                    logger.error('MQTT connect handshake timed out after 15s')
                    raise TimeoutError('MQTT connect timeout')

                # Successful handshake → reset backoff counter.
                backoff = self.BACKOFF_INITIAL

                if self.notify_enabled:
                    self.notify.ready()
                    self.notify.status('Connected.')
                    self.notify.notify()

                # Drive sensor polling from the main thread. paho-mqtt's
                # network thread handles connect/messages; we just call
                # probe.poll() every 5s and ping sd_notify so systemd's
                # watchdog stays satisfied. The is_connected check after
                # each sleep exits the loop within 5s of any disconnect.
                while self.probe.is_connected:
                    try:
                        self.probe.poll()
                    except Exception:
                        # Probe.poll() catches per-sensor errors, so
                        # reaching here means a structural bug.
                        logger.exception('Probe.poll crashed')
                    if self.notify_enabled:
                        self.notify.notify()
                    time.sleep(5)

                logger.debug('Probe is not connected')
            except Exception as e:
                logger.exception('Connect cycle error: %s', e)
                if self.notify_enabled:
                    # Carry exception type + message in the sd_notify
                    # status so the operator can tell failure modes apart
                    # (TimeoutError vs ConnectionRefused vs CertificateError)
                    # straight from `systemctl status`.
                    detail = f'{type(e).__name__}: {e}'[:200]
                    self.notify.status(f'Failed: {detail}')
                    self.notify.notify()

            self.stop()
            logger.info('Reconnect in %ds', backoff)
            self._backoff_sleep(backoff)
            backoff = min(backoff * 2, self.BACKOFF_MAX)

    def stop(self) -> None:
        """Tear down current cycle: publish offline-state, disconnect
        MQTT, stop the network loop thread. Idempotent — safe to call
        when attributes don't yet exist (early Setup-failure).

        On a clean (graceful) disconnect the Last-Will does NOT fire, so
        we explicitly publish `connected="0"` retained before disconnecting
        — otherwise the manager would keep seeing the previous retained
        `connected="1"` until a new probe takes over the topic.
        """
        if hasattr(self, 'mqtt_client'):
            if self.mqtt_client.is_connected():
                # Mirror the Last-Will payload so dashboards see "offline"
                # immediately rather than waiting for the next probe-up.
                try:
                    self.mqtt_client.publish(
                        f'probe/{self.fqdn}/v1/connected',
                        payload='0', qos=1, retain=True,
                    ).wait_for_publish(timeout=2)
                except Exception as e:
                    # Publish/wait can fail if the broker is mid-disconnect;
                    # log at debug — Last-Will will still flip the topic on
                    # the next unclean-disconnect cycle.
                    logger.debug('Graceful offline-publish failed: %s', e)
                self.mqtt_client.disconnect()
            # loop_stop() must be called for every loop_start() — without
            # it the paho-mqtt network thread leaks per reconnect cycle.
            self.mqtt_client.loop_stop()


@click.command()
@click.option('--config_file', type=str, required=True,
              help='Path to userconfig.txt with PROBE_METHODS / PROBE_CAPABILITIES.')
@click.option('--mqtt_hostname', type=str, default='srv-control-avm', show_default=True,
              help='MQTT broker hostname or IP.')
@click.option('--mqtt_port', type=int, default=None,
              help='MQTT broker port. Default: 8883 (1883 with --no_tls).')
@click.option('--ca_certificate', type=str, default='ca_certificate.pem', show_default=True,
              help='Path to CA certificate (PEM) for mTLS.')
@click.option('--certfile', type=str, default='client_certificate.pem', show_default=True,
              help='Path to client certificate (PEM) for mTLS.')
@click.option('--keyfile', type=str, default='client_key.pem', show_default=True,
              help='Path to client private key (PEM) for mTLS.')
@click.option('--no_tls', is_flag=True, default=False,
              help='Disable TLS — for local testing only. Refuses to run against non-localhost without prominent warning.')
@click.option('--loglevel', type=click.Choice(['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'], case_sensitive=False), default='INFO',
              help='Python logging level.')
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
        if no_tls:
            # Operator-visible status: TLS off. `systemctl status` shows
            # this prominently next to the active-state marker.
            notify.status('UNSAFE: --no_tls active (no encryption, no auth)')
        else:
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
