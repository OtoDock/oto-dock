"""The computer tool's own tests (``mcps/custom/computer-mcp/tests``), run in a
child interpreter with the tool's folder as its root.

The tool imports its siblings by bare name (``screen``, ``server``, ``steps``),
names other tools use too, so its tests cannot be collected beside the proxy's.
No other gate runs them, and they hold every stop of a call of steps, the keys
released after a failure and the system reads with their libraries faked. The
child sees no display: its own fixtures fail a test that would reach one.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests._paths import CUSTOM_MCPS

_HIDDEN = {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "PYTHONPATH", "PYTEST_ADDOPTS"}


@pytest.mark.timeout(300)
def test_the_computer_tools_own_tests_pass(tmp_path):
    folder = CUSTOM_MCPS / "computer-mcp"
    if not (folder / "tests").is_dir():
        pytest.skip("computer-mcp is not in this checkout")
    env = {k: v for k, v in os.environ.items() if k not in _HIDDEN and not k.startswith("PYTEST_XDIST")}
    env["HOME"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", os.devnull,
         "--rootdir", str(folder), "tests"],
        cwd=folder, capture_output=True, text=True, timeout=280, env=env, check=False,
    )
    assert r.returncode == 0, (r.stdout[-6000:] + r.stderr[-2000:])
    assert " passed" in r.stdout
