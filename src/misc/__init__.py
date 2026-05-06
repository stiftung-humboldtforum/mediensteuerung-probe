from typing import Any, Optional
import shlex
import json
import logging

# Hierarchical logger so test fixtures can attach handlers to a
# specific scope and `logging.getLogger('humboldt_probe')` still
# captures everything below it. Re-exported for the rest of the
# package via `from misc import logger`.
logger = logging.getLogger('humboldt_probe')

# Probe version. Published as a retained MQTT topic on connect so the
# manager dashboard can detect fleet-version drift. Sync with
# pyproject.toml when bumping.
VERSION = '0.2.0'


def get_config(config_file: str) -> dict[str, str]:
    """Parse a userconfig.txt with shell-style KEY="value" lines into
    a dict. Lines starting with '#' (after optional whitespace) are
    treated as comments and skipped. Errors (file missing, parse
    error) yield an empty dict — Probe.__init__ falls back to defaults
    / fail-closed values."""
    config: dict = {}
    try:
        with open(config_file) as f:
            content = f.read()
    except FileNotFoundError:
        logger.error('Config file not found: %s', config_file)
        return config
    except OSError as e:
        logger.error('Cannot read config %s: %s', config_file, e)
        return config

    # Strip comment lines BEFORE shlex.split — otherwise shlex would
    # tokenise '# Periodische Sensor-Polls' into 6 useless tokens that
    # end up as garbage keys in the config dict.
    cleaned = '\n'.join(
        line for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    )
    try:
        tokens = shlex.split(cleaned)
    except ValueError as e:
        # ValueError = unterminated quote / mismatched escape.
        logger.error('Config %s parse error: %s', config_file, e)
        return config

    for token in tokens:
        var, sep, value = token.partition('=')
        if not sep:
            logger.warning('Ignoring config token without "=": %r', token)
            continue
        config[var] = value
    return config


def parse_payload(payload: bytes) -> tuple[list[Any], dict[str, Any]]:
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


def status_response(status: dict[str, str]) -> str:
    """Wrap a status dict (e.g. {'display': 'ok', 'easire': 'error'})
    in the standard probe-response envelope. Despite the per-key 'error'
    values it carries, the envelope is data-keyed (not error-keyed) —
    the dict is informational status, not a transport-layer error.
    """
    return make_response(data={'status': 'complete', 'result': status})
