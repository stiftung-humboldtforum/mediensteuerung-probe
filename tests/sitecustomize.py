"""Subprocess coverage hook.

The running_probe fixture (conftest.py) spawns `python src/app.py` as
a subprocess. Without help, that subprocess doesn't participate in
pytest-cov's coverage tracking — App.run, the reconnect loop, watchdog
gating etc. would all show as uncovered even though every integration
test exercises them.

Mechanism: Python loads any `sitecustomize.py` that's on sys.path at
startup. The conftest fixture puts `tests/` on PYTHONPATH for the
subprocess, so this file gets imported. If COVERAGE_PROCESS_START is
set (the standard coverage.py env-var pointing at a config file),
coverage.process_startup() starts tracking. The .coverage.<pid>
fragment gets combined by pytest-cov at the end of the session.

This file is harmless when not running under pytest — coverage.process_startup
is a no-op without COVERAGE_PROCESS_START.
"""
import os

if os.environ.get('COVERAGE_PROCESS_START'):
    try:
        import coverage
        coverage.process_startup()
    except ImportError:
        pass
