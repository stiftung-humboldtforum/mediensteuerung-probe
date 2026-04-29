import json
from misc import parse_payload, make_response, error_response, get_config


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


def test_make_response_data():
    result = json.loads(make_response(data={'status': 'ok'}))
    assert result['data']['status'] == 'ok'
    assert 'error' not in result


def test_make_response_error():
    result = json.loads(make_response(error={'message': 'fail'}))
    assert result['error']['message'] == 'fail'
    assert 'data' not in result


def test_make_response_both():
    result = json.loads(make_response(data={'x': 1}, error={'y': 2}))
    assert 'data' in result
    assert 'error' in result


def test_error_response():
    result = json.loads(error_response({'display': 'ok', 'easire': 'error'}))
    assert result['data']['status'] == 'complete'
    assert result['data']['result']['display'] == 'ok'
    assert result['data']['result']['easire'] == 'error'


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
