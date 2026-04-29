import time
import socket
import logging

import click
import paho.mqtt.client as mqtt
try:
    import sd_notify
except ImportError:
    sd_notify = None


from probe import Probe
from misc import get_config, logger


class App:
    def __init__(self,
                 config,
                 mqtt_hostname,
                 mqtt_port,
                 ca_certificate,
                 certfile,
                 keyfile,
                 no_tls,
                 notify):
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
    def fqdn(self):
        fqdn = socket.getfqdn()
        if fqdn != self._fqdn:
            self.stop()
            self._fqdn = fqdn
            raise Exception('FQDN change detected. Retrying.')
        return fqdn

    def _setup(self):
        logger.info('FQDN: %s', self.fqdn)
        self.mqtt_client = mqtt.Client(client_id=self.fqdn)
        self.mqtt_client.enable_logger(logger)

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

    def run(self):
        while True:
            try:
                self._setup()
            except Exception as e:
                logger.exception(e)
                time.sleep(5)
                continue

            try:
                self.probe.start()

                if self.notify_enabled:
                    self.notify.status('Connecting...')

                logger.info('Connecting MQTT Host: %s:%s', self.mqtt_hostname, self.mqtt_port)
                self.mqtt_client.connect(self.mqtt_hostname, self.mqtt_port, 60)
                self.mqtt_client.loop_start()

                if self.notify_enabled:
                    self.notify.ready()
                    self.notify.status('Connected.')
                    self.notify.notify()
                time.sleep(3)

                while self.probe.is_connected:
                    logger.info('FQDN: %s', self.fqdn)
                    time.sleep(5)
                    if self.notify_enabled:
                        self.notify.notify()

                logger.debug('Probe is not connected')
            except Exception as e:
                logger.exception(e)
                if self.notify_enabled:
                    self.notify.status('Failed.')
                    self.notify.notify()

            self.stop()
            time.sleep(5)

    def stop(self):
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
@click.option('--loglevel', type=click.Choice(['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'], case_sensitive=False), default='CRITICAL')
def main(
        config_file,
        mqtt_hostname,
        mqtt_port,
        ca_certificate,
        certfile,
        keyfile,
        no_tls,
        loglevel):

    logging.basicConfig(level=getattr(logging, loglevel, 'WARNING'),
                        format='%(filename)s[line:%(lineno)d] %(levelname)s %(message)s')

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
