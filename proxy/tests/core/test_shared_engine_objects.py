"""The suite's guard on the objects every test of a worker shares (the
``pytest_runtest_setup`` and ``pytest_runtest_teardown`` hooks of the
conftest): each engine, its remote adapter, the remote layer and their
classes.

A test that replaces a method on one of them by a plain assignment leaves
its stand-in for every test after it in the same worker. The guard fails
that test by name and puts the object back. The probes below run as a
suite of their own, in a process of their own, with the guard wired as the
suite wires it: a leak's test errors at its end, the test after it finds
the object as made, and ``monkeypatch.setattr`` passes. The probes' suite
has an autouse fixture that takes ``monkeypatch``, as the proxy's has: the
guard reads the objects after the test's own patches are undone, whatever
the order of the fixtures.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_GUARD = '''
import pytest

from tests.conftest import pytest_runtest_setup, pytest_runtest_teardown  # noqa: F401


@pytest.fixture(autouse=True)
def _a_fixture_of_every_test_that_patches(monkeypatch):
    monkeypatch.setenv("PROBE", "1")
    yield
'''

_PROBES = '''
from unittest.mock import MagicMock

from core.engine import Engine
from core.remote.remote_execution import RemoteExecutionLayer
from core.session import session_manager as sm


async def _stand_in(*_a, **_k):
    return None


def _engine():
    return sm._ENGINES["claude-code-cli"]


def _adapter():
    return _engine().remote_adapter()


def test_00_the_probes_have_a_remote_layer():
    # The probes' own process: the layer stays for the tests below.
    sm._remote_layer = RemoteExecutionLayer(MagicMock())
    _adapter()


def test_10_leak_on_the_adapter():
    _adapter().begin_turn = _stand_in


def test_11_the_adapter_is_as_made():
    assert "begin_turn" not in vars(_adapter())
    assert _adapter().begin_turn.__func__ is type(_adapter()).begin_turn


def test_20_leak_on_the_engine():
    _engine().prepare_spawn = _stand_in


def test_21_the_engine_is_as_made():
    assert "prepare_spawn" not in vars(_engine())


def test_30_leak_on_the_adapters_class():
    type(_adapter()).close_state = _stand_in


def test_31_the_adapters_class_is_as_made():
    assert type(_adapter()).close_state is not _stand_in
    assert type(_adapter()).close_state.__qualname__.endswith("Adapter.close_state")


def test_40_leak_on_the_remote_layer():
    sm._remote_layer.close_session = _stand_in


def test_41_the_remote_layer_is_as_made():
    assert "close_session" not in vars(sm._remote_layer)


def test_50_leak_on_the_remote_layers_class():
    RemoteExecutionLayer.abort = _stand_in


def test_51_the_remote_layers_class_is_as_made():
    assert "abort" not in vars(RemoteExecutionLayer)
    assert RemoteExecutionLayer.abort is not _stand_in


def test_60_leak_on_a_base_class():
    Engine.prepare_spawn = _stand_in


def test_61_the_base_class_is_as_made():
    assert Engine.prepare_spawn is not _stand_in


def test_70_leak_by_taking_a_method_off_a_class():
    del type(_adapter()).steer


def test_71_the_method_is_back():
    assert callable(type(_adapter()).steer)


def test_80_monkeypatch_is_the_way(monkeypatch):
    monkeypatch.setattr(_adapter(), "begin_turn", _stand_in)
    monkeypatch.setattr(_engine(), "prepare_spawn", _stand_in)
    monkeypatch.setattr(type(_adapter()), "close_state", _stand_in)
    monkeypatch.setattr(sm._remote_layer, "close_session", _stand_in)
    monkeypatch.setattr(RemoteExecutionLayer, "abort", _stand_in)
    monkeypatch.setattr(Engine, "prepare_spawn", _stand_in)


def test_81_and_it_leaves_them_as_made():
    for shared in (_adapter(), _engine(), sm._remote_layer):
        assert not [name for name, value in vars(shared).items() if value is _stand_in]
    assert "begin_turn" not in vars(_adapter()) and "prepare_spawn" not in vars(_engine())
    assert "abort" not in vars(RemoteExecutionLayer) and Engine.prepare_spawn is not _stand_in


def test_82_leak_by_a_plain_replacement_of_the_remote_layer():
    sm._remote_layer = MagicMock()


def test_83_the_remote_layer_is_the_one_made():
    assert type(sm._remote_layer) is RemoteExecutionLayer


def test_90_state_of_its_own_is_no_leak():
    # An object's own state, a new session and a lazily made adapter are no method of a class.
    sm._ENGINES["codex-cli"].remote_adapter()
    sm._remote_layer._sessions["s-1"] = object()
    sm._remote_layer._sessions.pop("s-1")
'''

#: Each leak's test, and what the guard's sentence names for it.
_LEAKS = {
    "test_10_leak_on_the_adapter": "the claude-code-cli engine's remote adapter: begin_turn",
    "test_20_leak_on_the_engine": "the claude-code-cli engine: prepare_spawn",
    "test_30_leak_on_the_adapters_class": "the class ClaudeRemoteAdapter: close_state",
    "test_40_leak_on_the_remote_layer": "the remote layer: close_session",
    "test_50_leak_on_the_remote_layers_class": "the class RemoteExecutionLayer: abort",
    "test_60_leak_on_a_base_class": "the class Engine: prepare_spawn",
    "test_70_leak_by_taking_a_method_off_a_class": "the class ClaudeRemoteAdapter: steer",
    "test_82_leak_by_a_plain_replacement_of_the_remote_layer": "the remote layer: replaced by a MagicMock",
}
_ALL = 20

#: A class first imported inside a test: the guard reads it before the test
#: all the same.
_FIRST_IMPORT = '''
async def _stand_in(*_a, **_k):
    return None


def test_10_leak_on_a_class_first_imported_here():
    from core.remote.remote_execution import RemoteExecutionLayer
    RemoteExecutionLayer.abort = _stand_in


def test_11_the_class_is_as_made():
    from core.remote.remote_execution import RemoteExecutionLayer
    assert RemoteExecutionLayer.abort is not _stand_in
'''


def _run_probes(tmp_path: Path, probes: str) -> tuple[dict[str, str], str]:
    """The probes run as a suite of their own with the guard: each errored
    test with the guard's sentence, and what the run said."""
    (tmp_path / "conftest.py").write_text(_GUARD)
    (tmp_path / "test_probes.py").write_text(probes)
    # From the proxy's folder, so the probes import the suite's own conftest
    # and the proxy as every test does. A wide terminal: the summary's lines
    # keep each failure's whole sentence.
    done = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-rE", "-p", "no:cacheprovider", "-p", "no:xdist"],
        cwd=Path(REPO_ROOT) / "proxy", env={**os.environ, "COLUMNS": "2000"},
        capture_output=True, text=True, timeout=110, check=False)
    said = done.stdout + done.stderr
    return dict(re.findall(r"^ERROR \S*test_probes\.py::(\w+) - Failed: (.*)$", said, re.M)), said


def test_a_test_that_leaves_a_stand_in_on_a_shared_object_fails_and_the_next_finds_it_as_made(tmp_path: Path):
    errors, said = _run_probes(tmp_path, _PROBES)
    assert sorted(errors) == sorted(_LEAKS), said[-3000:]
    for name, what in _LEAKS.items():
        assert what in errors[name], errors[name]
        assert "replace it with monkeypatch.setattr" in errors[name]
    # Every test passed its own body: a leak fails at its end, and the tests after it found the object as made.
    assert re.search(rf"\b{_ALL} passed, {len(_LEAKS)} errors? in ", said), said[-1500:]


def test_a_class_first_imported_inside_a_test_is_read_before_it(tmp_path: Path):
    """The remote layer's module is imported by no test before its first
    use: a leak on its class in that same test is caught all the same."""
    errors, said = _run_probes(tmp_path, _FIRST_IMPORT)
    assert sorted(errors) == ["test_10_leak_on_a_class_first_imported_here"], said[-3000:]
    assert "the class RemoteExecutionLayer: abort" in errors["test_10_leak_on_a_class_first_imported_here"]
    assert re.search(r"\b2 passed, 1 error in ", said), said[-1500:]
