"""The host Python floor (``PYTHON_MIN_VERSION`` in VERSIONS.md) and every
site that enforces or states it move together, or this suite goes red.

Nothing at runtime reads the pin: the satellite venv runs on whatever
``python3`` the host has, the two installers gate on the floor, the baseline
installer's probe accepts it, the README states it. Those sites disagreed for
months (3.13 declared, 3.10 enforced, 3.12 installed on Windows) — this is the
drift detector. The runbook's "host Python floor" section lists the sites.

The tarball never ships ``tests/``; the public cut keeps VERSIONS.md,
``scripts/`` and the README, so the paths below exist wherever the suite runs.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _floor() -> tuple[int, int]:
    versions = ROOT / "VERSIONS.md"
    if not versions.is_file():
        pytest.skip("VERSIONS.md is not in this tree")
    m = re.search(r"^PYTHON_MIN_VERSION=(\d+)\.(\d+)\s*$", versions.read_text(encoding="utf-8"), re.M)
    assert m, "VERSIONS.md has no PYTHON_MIN_VERSION pin"
    return int(m.group(1)), int(m.group(2))


def _read(rel: str) -> str:
    path = ROOT / rel
    assert path.is_file(), f"{rel} is missing"
    return path.read_text(encoding="utf-8")


def test_running_interpreter_meets_the_floor():
    assert sys.version_info[:2] >= _floor()


def test_the_installers_gate_on_the_floor():
    major, minor = _floor()
    sh = _read("satellite/install.sh")
    assert f'[ "$minor" -ge {minor} ]' in sh
    assert f"Python {major}.{minor}+ required" in sh

    ps1 = _read("satellite/install.ps1")
    assert f"$v.Minor -ge {minor}" in ps1
    assert f"Python {major}.{minor}+ required" in ps1


def test_the_baseline_probe_accepts_the_floor():
    major, minor = _floor()
    baseline = _read("scripts/install-baseline-tools.ps1")
    assert f"[int]$matches[2] -ge {minor}" in baseline
    # The probe has no user-facing message; its comment names the floor.
    assert f">={major}.{minor}" in baseline


def test_the_readme_names_the_floor():
    major, minor = _floor()
    assert f"Python {major}.{minor} or newer" in _read("satellite/README.md")
