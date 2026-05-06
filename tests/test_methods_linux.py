"""Mock-based tests for methods/_linux.py — complement to
tests/test_methods.py (which already covers is_muted / uptime /
temperatures / fans / display).

This file fills in:
- shutdown/reboot (sudo invocations + RuntimeError handling)
- mute/unmute (wpctl invocations)
- display env + display timeout details

Together they cover _linux.py without real PipeWire/X11/sudo hardware.
"""
import subprocess
from unittest.mock import patch

import pytest

from methods import _linux


# --- Power (sudo shutdown / sudo reboot) ----------------------------------

@patch('methods._linux.subprocess.run')
def test_linux_shutdown_calls_sudo_shutdown_now(mock_run):
    mock_run.return_value.returncode = 0
    _linux.shutdown()
    args = mock_run.call_args[0][0]
    assert args == ['sudo', '/sbin/shutdown', 'now']


@patch('methods._linux.subprocess.run')
def test_linux_reboot_calls_sudo_reboot_now(mock_run):
    mock_run.return_value.returncode = 0
    _linux.reboot()
    args = mock_run.call_args[0][0]
    assert args == ['sudo', '/sbin/reboot', 'now']


@patch('methods._linux.subprocess.run')
def test_linux_shutdown_raises_RuntimeError_on_nonzero_rc(mock_run):
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = 'a password is required'
    with pytest.raises(RuntimeError, match='shutdown failed.*password'):
        _linux.shutdown()


@patch('methods._linux.subprocess.run')
def test_linux_reboot_raises_RuntimeError_on_nonzero_rc(mock_run):
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = 'sudo: missing NOPASSWD entry'
    with pytest.raises(RuntimeError, match='reboot failed.*NOPASSWD'):
        _linux.reboot()


# --- Audio (wpctl + PipeWire) ---------------------------------------------

@patch('methods._linux.subprocess.run')
def test_linux_mute_calls_wpctl(mock_run):
    mock_run.return_value.returncode = 0
    _linux.mute()
    args = mock_run.call_args[0][0]
    assert args == ['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '1']
    # Timeout must be set so a PipeWire hang doesn't block the polling loop.
    assert mock_run.call_args[1].get('timeout') == 3


@patch('methods._linux.subprocess.run')
def test_linux_unmute_calls_wpctl(mock_run):
    mock_run.return_value.returncode = 0
    _linux.unmute()
    args = mock_run.call_args[0][0]
    assert args == ['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0']


@patch('methods._linux.subprocess.run')
def test_linux_mute_raises_on_nonzero_rc(mock_run):
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = 'PipeWire daemon not running'
    with pytest.raises(RuntimeError, match='mute failed.*PipeWire'):
        _linux.mute()


@patch('methods._linux.subprocess.run')
def test_linux_unmute_raises_on_nonzero_rc(mock_run):
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = 'no default sink'
    with pytest.raises(RuntimeError, match='unmute failed.*sink'):
        _linux.unmute()


# --- Display (xrandr env + timeout) ---------------------------------------

_XRANDR_OUTPUT = b"""\
Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767
HDMI-0 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   1920x1080     60.00*+  74.97
"""


@patch('methods._linux.subprocess.run')
def test_linux_display_passes_DISPLAY_env(mock_run):
    """display() must pass DISPLAY=:0 in env — otherwise xrandr sees
    nothing when the probe runs as a systemd user without $DISPLAY."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=['xrandr', '--current'], returncode=0,
        stdout=_XRANDR_OUTPUT, stderr=b'',
    )
    _linux.display()
    env = mock_run.call_args[1]['env']
    assert env.get('DISPLAY') == ':0'


@patch('methods._linux.subprocess.run')
def test_linux_display_has_timeout(mock_run):
    """An xrandr hang must not block the polling loop."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=['xrandr', '--current'], returncode=0,
        stdout=_XRANDR_OUTPUT, stderr=b'',
    )
    _linux.display()
    assert mock_run.call_args[1].get('timeout') == 5
