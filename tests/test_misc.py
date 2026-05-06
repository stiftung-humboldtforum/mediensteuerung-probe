import json
from misc import parse_payload, get_config


def test_parse_payload_valid():
    payload = json.dumps({'args': [1, 2], 'kwargs': {'x': 3}}).encode()
    args, kwargs = parse_payload(payload)
    assert args == [1, 2]
    assert kwargs == {'x': 3}


def test_parse_payload_args_only():
    payload = json.dumps({'args': ['a', 'b']}).encode()
    args, kwargs = parse_payload(payload)
    assert args == ['a', 'b']
    assert kwargs == {}


def test_parse_payload_kwargs_only():
    payload = json.dumps({'kwargs': {'key': 'value'}}).encode()
    args, kwargs = parse_payload(payload)
    assert args == []
    assert kwargs == {'key': 'value'}


def test_parse_payload_empty():
    args, kwargs = parse_payload(b'')
    assert args == []
    assert kwargs == {}


def test_parse_payload_invalid_json():
    args, kwargs = parse_payload(b'not json')
    assert args == []
    assert kwargs == {}


def test_parse_payload_args_not_list():
    payload = json.dumps({'args': 'evil', 'kwargs': {}}).encode()
    args, kwargs = parse_payload(payload)
    assert args == []
    assert kwargs == {}


def test_parse_payload_kwargs_not_dict():
    payload = json.dumps({'args': [], 'kwargs': 'evil'}).encode()
    args, kwargs = parse_payload(payload)
    assert args == []
    assert kwargs == {}


def test_parse_payload_kwargs_non_string_keys():
    payload = b'{"args": [], "kwargs": {"1": "x"}}'
    args, kwargs = parse_payload(payload)
    assert args == []
    assert kwargs == {'1': 'x'}


def test_parse_payload_top_level_array():
    payload = json.dumps([1, 2, 3]).encode()
    args, kwargs = parse_payload(payload)
    assert args == []
    assert kwargs == {}


def test_get_config(tmp_path):
    config_file = tmp_path / 'userconfig.txt'
    config_file.write_text('PROBE_METHODS="ping,temperatures"\nPROBE_CAPABILITIES="shutdown,reboot"')
    config = get_config(str(config_file))
    assert config['PROBE_METHODS'] == 'ping,temperatures'
    assert config['PROBE_CAPABILITIES'] == 'shutdown,reboot'


def test_get_config_missing_file():
    config = get_config('/nonexistent/path/config.txt')
    assert config == {}


def test_get_config_empty_file(tmp_path):
    config_file = tmp_path / 'empty.txt'
    config_file.write_text('')
    config = get_config(str(config_file))
    assert config == {}


def test_get_config_skips_comments(tmp_path):
    """userconfig.example.txt commit (R4) hatte Kommentar-Zeilen
    eingefuehrt — vorher zerlegte shlex die in nutzlose Tokens."""
    config_file = tmp_path / 'with_comments.txt'
    config_file.write_text(
        '# Header comment\n'
        '#   Indented comment\n'
        'PROBE_METHODS="ping,uptime"\n'
        '\n'
        '# Another comment\n'
        'PROBE_CAPABILITIES="reboot"\n'
    )
    config = get_config(str(config_file))
    assert config == {'PROBE_METHODS': 'ping,uptime', 'PROBE_CAPABILITIES': 'reboot'}


def test_get_config_token_without_equals_ignored(tmp_path):
    config_file = tmp_path / 'malformed.txt'
    config_file.write_text('VALID="ok"\nstray_token_no_equals\n')
    config = get_config(str(config_file))
    assert config == {'VALID': 'ok'}


def test_get_config_unterminated_quote_returns_empty(tmp_path):
    """shlex raises ValueError on mismatched quotes — get_config must
    return {} instead of crashing the probe at startup."""
    config_file = tmp_path / 'broken.txt'
    config_file.write_text('PROBE_METHODS="ping,uptime\n')  # unterminated quote
    config = get_config(str(config_file))
    assert config == {}


def test_get_config_unreadable_file_returns_empty(tmp_path):
    """OS-level read errors yield an empty dict (fail-closed)."""
    config = get_config(str(tmp_path))  # directory, not a file
    assert config == {}
