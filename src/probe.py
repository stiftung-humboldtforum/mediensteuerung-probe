import time
from threading import Thread

from paho.mqtt.client import Client, SubscribeOptions, MQTTMessage


from misc import error_response, logger, parse_payload, make_response
import methods
from methods import call_method, SENSORS, COMMANDS


class Probe(Thread):
    def __init__(self,
                 fqdn: str,
                 client: Client,
                 config: dict):
        super().__init__(daemon=True)
        self.fqdn = fqdn
        self.client = client
        self.config = config

        self._running = False

        self.is_connected = False
        self.playback_pos = None
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        try:
            periodic_methods = self.config['PROBE_METHODS'].split(',')
        except KeyError:
            logger.error('No PROBE_METHODS in userconfig.txt')
            periodic_methods = []

        try:
            self.capabilities = self.config['PROBE_CAPABILITIES']
        except KeyError:
            logger.error('No PROBE_CAPABILITIES in userconfig.txt — refusing all commands (fail-closed)')
            self.capabilities = ''

        self._allowed_methods = set(self.capabilities.split(','))
        self.methods = {name: SENSORS[name] for name in periodic_methods if name in SENSORS}
        unknown = [name for name in periodic_methods if name and name not in SENSORS]
        if unknown:
            logger.warning('Ignoring unknown PROBE_METHODS: %s', ','.join(unknown))
        self.errors = {}

    def check_playback_pos(self):
        new_playback_pos = methods.mpv_file_pos_sec()
        if new_playback_pos == self.playback_pos:
            self.errors['playback'] = 'error'
        else:
            self.errors['playback'] = 'ok'
        self.playback_pos = new_playback_pos
        self.client.publish(
            f'probe/{self.fqdn}/mpv_file_pos_sec',
            make_response(data=dict(status='complete', result=new_playback_pos)),
        )

    def check_display(self):
        result = methods.display()
        if result is not None:
            self.errors['display'] = 'ok'
        else:
            self.errors['display'] = 'error'
        self.client.publish(
            f'probe/{self.fqdn}/display',
            make_response(data=dict(status='complete', result=result)),
        )

    def check_easire(self):
        result = methods.easire()
        if result is not None:
            self.errors['easire'] = 'ok'
        else:
            self.errors['easire'] = 'error'
        self.client.publish(
            f'probe/{self.fqdn}/easire',
            make_response(data=dict(status='complete', result=result)),
        )

    def call_methods(self):
        self.client.publish(f'probe/{self.fqdn}/capabilities', self.capabilities)
        for name, method in self.methods.items():
            try:
                if name == 'mpv_file_pos_sec':
                    self.check_playback_pos()
                elif name == 'display':
                    self.check_display()
                elif name == 'easire':
                    self.check_easire()
                else:
                    self.client.publish(f'probe/{self.fqdn}/{name}', call_method(method))
            except Exception:
                logger.exception(name)
        self.client.publish(f'probe/{self.fqdn}/errors', error_response(self.errors))

    def run(self):
        self._running = True
        while self._running:
            if self.is_connected:
                self.call_methods()
            time.sleep(5)

    def stop(self):
        self._running = False

    def on_connect(self, client: Client, *args):
        logger.info('Connected %s', args)
        self.is_connected = True
        client.publish(f'probe/{self.fqdn}/connected')
        self.client.publish(f'probe/{self.fqdn}/capabilities', self.capabilities)
        self.client.publish(f'probe/{self.fqdn}/boot_time', call_method(methods.boot_time))
        client.subscribe(
            f'manager/{self.fqdn}/#',
            options=SubscribeOptions(noLocal=True)
        )

    def on_disconnect(self, _, *args):
        logger.info('Disconnected %s', args)
        self.is_connected = False

    def on_message(self, client: Client, userdata, msg: MQTTMessage):
        parts = msg.topic.split('/')
        if len(parts) < 3 or not parts[2]:
            logger.warning('Ignoring malformed topic: %s', msg.topic)
            return
        method_name = parts[2]
        logger.info('Received method %s', method_name)
        if method_name not in self._allowed_methods:
            response = make_response(error=dict(message='Method not allowed'))
            client.publish(f'probe/{self.fqdn}/{method_name}', response)
            return
        method = getattr(methods, method_name, None) if method_name in COMMANDS else None
        response = make_response(data=dict(status='received'))
        client.publish(f'probe/{self.fqdn}/{method_name}', response)
        if method is None:
            response = make_response(
                error=dict(message='Unknown method')
            )
        else:
            args, kwargs = parse_payload(msg.payload)
            response = call_method(method, *args, **kwargs)
        logger.debug(response)
        client.publish(f'probe/{self.fqdn}/{method_name}', response)
