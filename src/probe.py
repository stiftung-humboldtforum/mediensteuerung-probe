from threading import Event, Thread

from paho.mqtt.client import Client, SubscribeOptions, MQTTMessage


from misc import status_response, logger, parse_payload, make_response, VERSION
import methods
from methods import call_method, SENSORS, COMMANDS


class Probe(Thread):
    """Periodic sensor poller + manager-command handler.

    Runs in its own daemon thread. Each cycle (5s):
        - polls sensors named in PROBE_METHODS via SENSORS-Whitelist,
          publishes results on probe/<fqdn>/<sensor>
        - aggregates ok/error per check_* sensor into errors-dict,
          publishes once on probe/<fqdn>/errors
        - bumps self.heartbeat (App.run uses it as watchdog signal)

    MQTT callbacks (on_connect/on_disconnect/on_message) are wired in
    __init__; the actual mqtt-loop runs in paho-mqtt's own thread.
    """

    def __init__(self,
                 fqdn: str,
                 client: Client,
                 config: dict[str, str]):
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
        self.errors = {}

    def check_playback_pos(self) -> None:
        """Probe whether mpv playback advances. If the position is
        identical to the previous cycle, mpv is considered stuck —
        errors['playback'] = 'error'."""
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

    def check_display(self) -> None:
        """Probe display resolution+refresh. Returns None on errors
        (no DISPLAY, xrandr fails, no active mode line)."""
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

    def check_easire(self) -> None:
        """Probe whether the easire-player process is alive."""
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

    def call_methods(self) -> None:
        """Run one polling cycle: invoke every sensor in self.methods
        and publish results. errors-dict is published once at the end.
        Per-method exceptions are logged but don't break the cycle."""
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

    def run(self) -> None:
        """Thread main-loop. Runs call_methods every 5s while connected;
        sleeps via Event.wait so stop() returns instantly."""
        while not self._stop_event.is_set():
            if self.is_connected:
                self.call_methods()
                self.heartbeat += 1
            # Interruptible sleep — stop() returns instantly statt
            # bis zu 5s zu warten.
            self._stop_event.wait(5)

    def stop(self) -> None:
        """Signal the run-loop to terminate. The thread wakes from
        wait(5) immediately. Caller should join() afterwards."""
        self._stop_event.set()

    def on_connect(self, client: Client, userdata, flags, reason_code, properties=None):
        """paho-mqtt v2 on_connect callback. Publishes initial state
        (connected=1, capabilities, boot_time — all retained), subscribes
        to manager-topics, sets connected_event for App.run."""
        logger.info('Connected reason_code=%s flags=%s', reason_code, flags)
        self.is_connected = True
        # retain=True so newly subscribing managers see "alive" without
        # waiting for the next sensor cycle.
        self.client.publish(f'probe/{self.fqdn}/connected', payload='1', qos=1, retain=True)
        self.client.publish(f'probe/{self.fqdn}/capabilities', self.capabilities, qos=1, retain=True)
        self.client.publish(f'probe/{self.fqdn}/version', payload=VERSION, qos=1, retain=True)
        self.client.publish(f'probe/{self.fqdn}/boot_time', call_method(methods.boot_time), qos=1, retain=True)
        # `+` matches exactly one topic level — manager commands are of
        # the form 'manager/<fqdn>/<command>'. Tighter than '#' (which
        # matches any depth) — extra path segments below would be ignored
        # by parts[2] anyway, but a tighter subscription saves broker
        # routing work and prevents accidental wildcard matches.
        self.client.subscribe(
            f'manager/{self.fqdn}/+',
            options=SubscribeOptions(noLocal=True)
        )
        # Initialer Heartbeat-Bump — der Connect-Handshake selbst ist
        # ein Lebenszeichen, das App.run als Watchdog-Signal nehmen kann
        # statt bis zum ersten call_methods()-Cycle (bis zu 5s) zu warten.
        self.heartbeat += 1
        self.connected_event.set()

    def on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        """paho-mqtt v2 on_disconnect callback. Clears the connected
        flags so App.run notices and goes into reconnect-Backoff.

        Also resets playback_pos: after reconnect the first cycle compares
        new vs. previous position. Without reset a stale value lingering
        from before the disconnect would falsely flag 'playback error'
        on the very first post-reconnect cycle if mpv happened to be at
        the same integer second.
        """
        logger.info('Disconnected reason_code=%s', reason_code)
        self.is_connected = False
        self.connected_event.clear()
        self.playback_pos = None

    def on_message(self, client: Client, userdata, msg: MQTTMessage):
        """Manager-command dispatcher. Topic format:
        manager/<fqdn>/<command>. Two gates apply:
            1. command must be in self._allowed_methods
               (PROBE_CAPABILITIES whitelist)
            2. command must resolve to a function via the COMMANDS dict
               — prevents reflection on imported module attributes
        """
        parts = msg.topic.split('/')
        if len(parts) < 3 or not parts[2]:
            logger.warning('Ignoring malformed topic: %s', msg.topic)
            return
        method_name = parts[2]
        if method_name not in self._allowed_methods:
            # Log at DEBUG only — disallowed commands are expected on a
            # multi-probe broker (each probe sees managers querying others).
            # INFO-logging here would flood logs and amplify a brute-force
            # attempt against the capability gate.
            logger.debug('Rejecting disallowed method: %s', method_name)
            response = make_response(error=dict(message='Method not allowed'))
            client.publish(f'probe/{self.fqdn}/{method_name}', response)
            return
        logger.info('Received method %s', method_name)
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
