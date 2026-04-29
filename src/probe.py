from threading import Event, Thread

from paho.mqtt.client import Client, SubscribeOptions, MQTTMessage


from misc import status_response, logger, parse_payload, make_response
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

        # Event statt running-bool: stop() setzt das Event und der
        # run()-Loop wacht aus der Wait-Phase sofort auf, statt bis
        # zu 5s im time.sleep zu haengen.
        self._stop_event = Event()

        self.is_connected = False
        # Set in on_connect, cleared in on_disconnect. Used by App.run()
        # to wait for the initial broker handshake instead of a blind
        # time.sleep(3) — schuetzt vor Reconnect-Flap auf langsamen
        # Brokern.
        self.connected_event = Event()
        self.playback_pos = None
        # Heartbeat counter: bumped after each successful call_methods()
        # cycle. The App-Thread uses this to gate sd_notify watchdog
        # pings — if the Probe-Thread hangs, the counter stalls and the
        # watchdog times out, allowing systemd to restart the unit.
        self.heartbeat = 0
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
        try:
            new_playback_pos = methods.mpv_file_pos_sec()
        except Exception:
            logger.exception('mpv_file_pos_sec')
            self.errors['playback'] = 'error'
            self.client.publish(
                f'probe/{self.fqdn}/mpv_file_pos_sec',
                make_response(error=dict(message='check failed')),
            )
            return
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
        try:
            result = methods.display()
        except Exception:
            logger.exception('display')
            self.errors['display'] = 'error'
            self.client.publish(
                f'probe/{self.fqdn}/display',
                make_response(error=dict(message='check failed')),
            )
            return
        if result is not None:
            self.errors['display'] = 'ok'
        else:
            self.errors['display'] = 'error'
        self.client.publish(
            f'probe/{self.fqdn}/display',
            make_response(data=dict(status='complete', result=result)),
        )

    def check_easire(self):
        try:
            result = methods.easire()
        except Exception:
            logger.exception('easire')
            self.errors['easire'] = 'error'
            self.client.publish(
                f'probe/{self.fqdn}/easire',
                make_response(error=dict(message='check failed')),
            )
            return
        if result is not None:
            self.errors['easire'] = 'ok'
        else:
            self.errors['easire'] = 'error'
        self.client.publish(
            f'probe/{self.fqdn}/easire',
            make_response(data=dict(status='complete', result=result)),
        )

    def call_methods(self):
        # capabilities werden in on_connect() retained publisht — kein
        # Bedarf, sie alle 5s erneut zu schicken (waere QoS 0 ohne retain
        # und reiner Traffic-Muell).
        for name, stored_method in self.methods.items():
            try:
                if name == 'mpv_file_pos_sec':
                    self.check_playback_pos()
                elif name == 'display':
                    self.check_display()
                elif name == 'easire':
                    self.check_easire()
                else:
                    # Late-bind via methods-Modul, damit Test-Patches
                    # (patch('methods.<name>')) auch in der periodischen
                    # Schleife greifen. stored_method ist Fallback.
                    method = getattr(methods, name, stored_method)
                    self.client.publish(f'probe/{self.fqdn}/{name}', call_method(method))
            except Exception:
                logger.exception(name)
        self.client.publish(f'probe/{self.fqdn}/errors', status_response(self.errors))

    def run(self):
        while not self._stop_event.is_set():
            if self.is_connected:
                self.call_methods()
                self.heartbeat += 1
            # Interruptible sleep — stop() returns instantly statt
            # bis zu 5s zu warten.
            self._stop_event.wait(5)

    def stop(self):
        self._stop_event.set()

    def on_connect(self, client: Client, userdata, flags, reason_code, properties=None):
        logger.info('Connected reason_code=%s flags=%s', reason_code, flags)
        self.is_connected = True
        # retain=True so newly subscribing managers see "alive" without
        # waiting for the next sensor cycle.
        self.client.publish(f'probe/{self.fqdn}/connected', payload='1', qos=1, retain=True)
        self.client.publish(f'probe/{self.fqdn}/capabilities', self.capabilities, qos=1, retain=True)
        self.client.publish(f'probe/{self.fqdn}/boot_time', call_method(methods.boot_time), qos=1, retain=True)
        self.client.subscribe(
            f'manager/{self.fqdn}/#',
            options=SubscribeOptions(noLocal=True)
        )
        # Initialer Heartbeat-Bump — der Connect-Handshake selbst ist
        # ein Lebenszeichen, das App.run als Watchdog-Signal nehmen kann
        # statt bis zum ersten call_methods()-Cycle (bis zu 5s) zu warten.
        self.heartbeat += 1
        self.connected_event.set()

    def on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        logger.info('Disconnected reason_code=%s', reason_code)
        self.is_connected = False
        self.connected_event.clear()

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
        client.publish(f'probe/{self.fqdn}/{method_name}', response, qos=1)
