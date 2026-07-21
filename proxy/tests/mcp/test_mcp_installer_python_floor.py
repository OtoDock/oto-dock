"""Python-floor retry for source-bundled (requirements.txt) MCP installs.

The pypi branch has always recreated the venv with ``uv venv --python <spec>``
when the resolver names a Python floor; bundled MCPs (server.py +
requirements.txt — every custom/core MCP) silently lacked the retry, so a
satellite whose default python is too old failed them forever (live-hit
2026-07-19: music-gen-mcp needs >=3.11 on a 3.10 satellite venv). These tests
script the subprocess sequence to pin the retry behavior.
"""

import asyncio
import json

import pytest

from services.mcp import mcp_installer

_UNSAT_LOG = (
    b"Using Python 3.10.12 environment at: venv\n"
    b"  x No solution found when resolving dependencies:\n"
    b"  Because the current Python version (3.10.12) does not satisfy "
    b"Python>=3.11 and rpds-py==2026.6.3 depends on Python>=3.11, we can "
    b"conclude that rpds-py==2026.6.3 cannot be used.\n"
)


class _FakeProc:
    def __init__(self, rc: int, out: bytes):
        self.returncode = rc
        self._out = out

    async def communicate(self):
        return self._out, None


def _mcp_dir(tmp_path):
    d = tmp_path / "music-gen-mcp"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"name": "music-gen-mcp"}))
    (d / "requirements.txt").write_text("rpds-py==2026.6.3\n")
    (d / "server.py").write_text("print('hi')\n")
    return d


def _fake_uv(tmp_path):
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    return str(uv)


@pytest.mark.asyncio
async def test_requirements_install_retries_with_python_floor(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    # Scripted sequence: venv-create ok → pip -r fails with the floor log →
    # venv recreate (must carry --python >=3.11) ok → pip -r ok.
    script = [
        _FakeProc(0, b"created"),
        _FakeProc(1, _UNSAT_LOG),
        _FakeProc(0, b"created 3.11"),
        _FakeProc(0, b"installed"),
    ]

    def _spawn(*cmd, **kw):
        calls.append(list(cmd))
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(script[len(calls) - 1])
        return fut

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    r = await mcp_installer.install_mcp(
        _mcp_dir(tmp_path), "python", "local",
        uv_bin=_fake_uv(tmp_path), python_bin="python3",
    )
    assert r.ok is True
    assert len(calls) == 4
    # The recreate names the reported floor so uv can fetch a matching CPython.
    assert calls[2][:4] == [calls[2][0], "venv", "--python", ">=3.11"]


@pytest.mark.asyncio
async def test_requirements_install_no_retry_without_floor_in_log(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    script = [
        _FakeProc(0, b"created"),
        _FakeProc(1, b"error: connection reset while downloading wheel"),
    ]

    def _spawn(*cmd, **kw):
        calls.append(list(cmd))
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(script[len(calls) - 1])
        return fut

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    r = await mcp_installer.install_mcp(
        _mcp_dir(tmp_path), "python", "local",
        uv_bin=_fake_uv(tmp_path), python_bin="python3",
    )
    assert r.ok is False
    assert len(calls) == 2  # transient failure — no doomed venv rebuild


@pytest.mark.asyncio
async def test_requirements_retry_failure_surfaces_original_style_error(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    script = [
        _FakeProc(0, b"created"),
        _FakeProc(1, _UNSAT_LOG),
        _FakeProc(1, b"no interpreter found for Python>=3.11"),
    ]

    def _spawn(*cmd, **kw):
        calls.append(list(cmd))
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(script[len(calls) - 1])
        return fut

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    r = await mcp_installer.install_mcp(
        _mcp_dir(tmp_path), "python", "local",
        uv_bin=_fake_uv(tmp_path), python_bin="python3",
    )
    assert r.ok is False
    assert "Python>=3.11" in r.log
