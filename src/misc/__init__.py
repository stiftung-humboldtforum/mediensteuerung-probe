from typing import Any, Optional
import shlex
import json
import logging

logger = logging.getLogger()


def get_config(config_file):
    """Parse a userconfig.txt with shell-style KEY="value" lines into
    a dict. Errors (file missing, parse error) yield an empty dict —
    Probe.__init__ falls back to defaults / fail-closed values."""
    config = {}
    try:
        with open(config_file) as f:
            for line in shlex.split(f.read()):
                var, _, value = line.partition('=')
                config[var] = value
    except Exception as e:
        logger.error('Loading config %s', e)
    return config


def parse_payload(payload: bytes) -> tuple[list, dict]:
    """Decode a manager-command MQTT-Payload into (args, kwargs).
    Expected format: JSON object {"args": [...], "kwargs": {...}}.
    Anything malformed (non-JSON, wrong types, non-string kwargs-keys)
    yields safe empty defaults + a logged warning — never raises."""
    args: list = []
    kwargs: dict = {}
    try:
        arguments = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return args, kwargs
    if not isinstance(arguments, dict):
        logger.warning('Payload is not a JSON object: %r', type(arguments).__name__)
        return args, kwargs
    raw_args = arguments.get('args', [])
    if isinstance(raw_args, list):
        args = raw_args
    else:
        logger.warning('Payload args is not a list: %r', type(raw_args).__name__)
    raw_kwargs = arguments.get('kwargs', {})
    if isinstance(raw_kwargs, dict) and all(isinstance(k, str) for k in raw_kwargs):
        kwargs = raw_kwargs
    else:
        logger.warning('Payload kwargs is not a string-keyed dict')
    return args, kwargs


def make_response(
    data: Optional[dict] = None,
    error: Optional[dict] = None,
) -> str:
    """Build the standard probe-response JSON envelope. Either or both
    of data/error may be set (manager-side decides which key wins)."""
    response: dict[str, Any] = {}
    if data is not None:
        response['data'] = data
    if error is not None:
        response['error'] = error
    return json.dumps(response)


def status_response(status):
    """Wrap a status dict (e.g. {'display': 'ok', 'easire': 'error'})
    in the standard probe-response envelope. Despite the per-key 'error'
    values it carries, the envelope is data-keyed (not error-keyed) —
    the dict is informational status, not a transport-layer error.
    """
    return make_response(data={'status': 'complete', 'result': status})
