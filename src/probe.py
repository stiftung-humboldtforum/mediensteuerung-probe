from threading import Event

from paho.mqtt.client import Client, SubscribeOptions, MQTTMessage


from misc import status_response, logger, parse_payload, make_response, VERSION
import methods
from methods import call_method, SENSORS, COMMANDS


class Probe:
    """Sensor poller and manager-command dispatcher.

    Probe is NOT a Thread — paho-mqtt already runs its own network
    thread that fires on_connect / on_disconnect / on_message. The
    periodic sensor poll is driven by the App.run() main thread, which
    calls Probe.poll() every 5s while connected.

    Net thread count: paho's network thread + main thread = 2.
    """

    def __init__(self,
                 fqdn: str,
                 client: Client,
                 config: dict[str, str]):
        self.fqdn = fqdn
        self.client = client
        self.config = config

        # Set in on_connect, cleared in on_disconnect. App.run() waits
        # on it for the initial broker handshake before entering the
        # polling loop.
        self.connected_event = Event()
        self.playback_pos = None
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        # Strip whitespace per token so 'mute, unmute' parses the same as
        # 'mute,unmute' — operators may add spaces for readability.
        try:
            periodic_methods = [s.strip() for s in self.config['PROBE_METHODS'].split(',')]
        except KeyError:
            logger.error('No PROBE_METHODS in userconfig.txt')
            periodic_methods = []

        try:
            self.capabilities = self.config['PROBE_CAPABILITIES']
        except KeyError:
            logger.error('No PROBE_CAPABILITIES in userconfig.txt — refusing all commands (fail-closed)')
            self.capabilities = ''

        self._allowed_methods = {s.strip() for s in self.capabilities.split(',')}
        self.methods = {name: SENSORS[name] for name in periodic_methods if name in SENSORS}
        unknown = [name for name in periodic_methods if name and name not in SENSORS]
        if unknown:
            logger.warning('Ignoring unknown PROBE_METHODS: %s', ','.join(unknown))
        # 'wake' is declarative-only on the manager side (WoL is external)
        # but still defined as a no-op COMMAND, so it's not 'unknown'.
        unknown_caps = [
            name for name in self._allowed_methods
            if name and name not in COMMANDS
        ]
        if unknown_caps:
            logger.warning('PROBE_CAPABILITIES contains unknown commands: %s', ','.join(unknown_caps))
        self.errors: dict[str, str] = {}

    @property
    def is_connected(self) -> bool:
        """Convenience flag — same source of truth as connected_event."""
        return self.connected_event.is_set()

    def check_playback_pos(self) -> None:
        """Probe whether mpv playback advances. Identical position
        across two cycles flags 'playback': 'error' (mpv stuck)."""
        try:
            new_playback_pos = methods.mpv_file_pos_sec()
        except Exception:
            logger.exception('mpv_file_pos_sec')
            self.errors['playback'] = 'error'
            self.client.publish(
                f'probe/{self.fqdn}/v1/mpv_file_pos_sec',
                make_response(error=dict(message='check failed')),
            )
            return
        if new_playback_pos == self.playback_pos:
            self.errors['playback'] = 'error'
        else:
            self.errors['playback'] = 'ok'
        self.playback_pos = new_playback_pos
        self.client.publish(
            f'probe/{self.fqdn}/v1/mpv_file_pos_sec',
            make_response(data=dict(status='complete', result=new_playback_pos)),
        )

    def check_display(self) -> None:
        """Probe display resolution+refresh. None on errors (no DISPLAY,
        xrandr fails, no active mode line)."""
        try:
            result = methods.display()
        except Exception:
            logger.exception('display')
            self.errors['display'] = 'error'
            self.client.publish(
                f'probe/{self.fqdn}/v1/display',
                make_response(error=dict(message='check failed')),
            )
            return
        if result is not None:
            self.errors['display'] = 'ok'
        else:
            self.errors['display'] = 'error'
        self.client.publish(
            f'probe/{self.fqdn}/v1/display',
            make_response(data=dict(status='complete', result=result)),
        )

    def check_easire(self) -> None:
        """Probe whether the easire-player process is alive."""
        try:
            result = methods.easire()
        except Exception:
            logger.exception('easire')
            self.errors['easire'] = 'error'
            self.client.publish(
                f'probe/{self.fqdn}/v1/easire',
                make_response(error=dict(message='check failed')),
            )
            return
        if result is not None:
            self.errors['easire'] = 'ok'
        else:
            self.errors['easire'] = 'error'
        self.client.publish(
            f'probe/{self.fqdn}/v1/easire',
            make_response(data=dict(status='complete', result=result)),
        )

    def poll(self) -> None:
        """Run one polling cycle: invoke every sensor in self.methods
        and publish results. errors-dict is published once at the end.
        Per-method exceptions are logged but don't break the cycle.

        Called from App.run() every 5s. Capabilities + boot_time are
        retained-published in on_connect so we don't repeat them here.
        """
        for name, stored_method in self.methods.items():
            try:
                if name == 'mpv_file_pos_sec':
                    self.check_playback_pos()
                elif name == 'display':
                    self.check_display()
                elif name == 'easire':
                    self.check_easire()
                else:
                    # Late-bind via the methods module so test patches
                    # on `methods.<name>` are honoured even from the
                    # polling loop. stored_method is the fallback.
                    method = getattr(methods, name, stored_method)
                    self.client.publish(f'probe/{self.fqdn}/v1/{name}', call_method(method))
            except Exception:
                logger.exception(name)
        self.client.publish(f'probe/{self.fqdn}/v1/errors', status_response(self.errors))

    def on_connect(self, client: Client, userdata, flags, reason_code, properties=None):
        """paho-mqtt v2 on_connect callback. Publishes initial state
        (connected, capabilities, version, boot_time — all retained),
        subscribes to manager-topics, sets connected_event for App.run."""
        logger.info('Connected reason_code=%s flags=%s', reason_code, flags)
        # retain=True so newly subscribing managers see "alive" without
        # waiting for the next sensor cycle.
        self.client.publish(f'probe/{self.fqdn}/v1/connected', payload='1', qos=1, retain=True)
        self.client.publish(f'probe/{self.fqdn}/v1/capabilities', self.capabilities, qos=1, retain=True)
        self.client.publish(f'probe/{self.fqdn}/v1/version', payload=VERSION, qos=1, retain=True)
        self.client.publish(f'probe/{self.fqdn}/v1/boot_time', call_method(methods.boot_time), qos=1, retain=True)
        # `+` matches exactly one topic level — manager commands are of
        # the form 'manager/<fqdn>/v1/<command>'. Tighter than '#' (which
        # matches any depth) — saves broker routing work and prevents
        # accidental wildcard matches.
        self.client.subscribe(
            f'manager/{self.fqdn}/v1/+',
            options=SubscribeOptions(noLocal=True)
        )
        self.connected_event.set()

    def on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        """paho-mqtt v2 on_disconnect callback. Clears connected_event so
        App.run notices and goes into reconnect-Backoff.

        Also resets playback_pos: after reconnect the first cycle compares
        new vs. previous position. Without reset, a stale value lingering
        from before the disconnect would falsely flag 'playback error'
        on the very first post-reconnect cycle if mpv happened to be at
        the same integer second.
        """
        logger.info('Disconnected reason_code=%s', reason_code)
        self.connected_event.clear()
        self.playback_pos = None

    def on_message(self, client: Client, userdata, msg: MQTTMessage):
        """Manager-command dispatcher. Topic format:
        manager/<fqdn>/v1/<command>. Two gates apply:
            1. command must be in self._allowed_methods
               (PROBE_CAPABILITIES whitelist)
            2. command must resolve to a function via the COMMANDS dict
               — prevents reflection on imported module attributes
        """
        parts = msg.topic.split('/')
        # Expect manager/<fqdn>/v1/<cmd>; reject anything else early.
        if len(parts) < 4 or parts[2] != 'v1' or not parts[3]:
            logger.warning('Ignoring malformed topic: %s', msg.topic)
            return
        method_name = parts[3]
        if method_name not in self._allowed_methods:
            # Log at DEBUG only — disallowed commands are expected on a
            # multi-probe broker (each probe sees managers querying others).
            # INFO-logging here would flood logs and amplify a brute-force
            # attempt against the capability gate.
            logger.debug('Rejecting disallowed method: %s', method_name)
            response = make_response(error=dict(message='Method not allowed'))
            client.publish(f'probe/{self.fqdn}/v1/{method_name}', response)
            return
        logger.info('Received method %s', method_name)
        method = getattr(methods, method_name, None) if method_name in COMMANDS else None
        response = make_response(data=dict(status='received'))
        client.publish(f'probe/{self.fqdn}/v1/{method_name}', response)
        if method is None:
            response = make_response(
                error=dict(message='Unknown method')
            )
        else:
            args, kwargs = parse_payload(msg.payload)
            response = call_method(method, *args, **kwargs)
        logger.debug(response)
        client.publish(f'probe/{self.fqdn}/v1/{method_name}', response, qos=1)
