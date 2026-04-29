import os
import platform
import subprocess

# Note: sys.coinit_flags is set in app.py before any imports happen, so
# that COM is initialized in MTA mode for both pythonnet/LHM and pycaw.
# Setting it here would be too late if app.py imports were re-ordered.

from .sensors import temperatures, fans, boot_time, uptime, mpv_file_pos_sec, display, easire
from misc import logger, make_response


def call_method(method, *args, **kwargs):
    try:
        result = method(*args, **kwargs)
        response = make_response(
            data=dict(status='complete', result=result)
        )
    except Exception as e:
        response = make_response(
            error=dict(
                message=type(e).__name__,
                errors=e.args
            )
        )
    return response

def shutdown():
    if platform.system() == 'Linux':
        result = os.system('sudo shutdown now')
    elif platform.system() == 'Windows':
        result = os.system('shutdown /s /t 0')
    else:
        raise NotImplementedError(f'shutdown not supported on {platform.system()}')
    if result != 0:
        raise Exception(result)

def reboot():
    if platform.system() == 'Linux':
        result = os.system('sudo reboot now')
    elif platform.system() == 'Windows':
        result = os.system('shutdown /r /t 0')
    else:
        raise NotImplementedError(f'reboot not supported on {platform.system()}')
    if result != 0:
        raise Exception(result)

def ping():
    return

def _get_windows_volume():
    from pycaw.pycaw import AudioUtilities
    speakers = AudioUtilities.GetSpeakers()
    return speakers.EndpointVolume

def is_muted():
    if platform.system() == 'Linux':
        return "MUTED" in subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]).decode()
    elif platform.system() == 'Windows':
        volume = _get_windows_volume()
        return bool(volume.GetMute())
    else:
        raise NotImplementedError(f'is_muted not supported on {platform.system()}')

def mute():
    if platform.system() == 'Linux':
        subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
    elif platform.system() == 'Windows':
        volume = _get_windows_volume()
        volume.SetMute(1, None)
    else:
        raise NotImplementedError(f'mute not supported on {platform.system()}')

def unmute():
    if platform.system() == 'Linux':
        subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
    elif platform.system() == 'Windows':
        volume = _get_windows_volume()
        volume.SetMute(0, None)
    else:
        raise NotImplementedError(f'unmute not supported on {platform.system()}')


SENSORS = {
    'ping': ping,
    'temperatures': temperatures,
    'fans': fans,
    'uptime': uptime,
    'boot_time': boot_time,
    'mpv_file_pos_sec': mpv_file_pos_sec,
    'display': display,
    'easire': easire,
    'is_muted': is_muted,
}

COMMANDS = {
    'shutdown': shutdown,
    'reboot': reboot,
    'mute': mute,
    'unmute': unmute,
    'ping': ping,
}
