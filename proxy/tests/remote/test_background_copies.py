"""The three ``background.spawn`` helpers (the proxy's, the satellite's, the
phone daemon's) share no code: each tree runs alone. Their common part is
held here, the parts that differ on purpose are left alone: the satellite
and the phone hand a task's death to the loop's exception handler and let
``SystemExit`` end a task as an exit, the proxy logs it and drains at
shutdown.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT

COPIES = {
    "proxy": REPO_ROOT / "proxy" / "core" / "background.py",
    "satellite": REPO_ROOT / "satellite" / "background.py",
    "phone": REPO_ROOT / "phone" / "background.py",
}


def _definitions(path: Path) -> dict[str, str]:
    """Each top-level function and assignment as an AST dump, docstrings dropped."""
    out: dict[str, str] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = ast.dump(node)
    return out


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"_background_copy_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_spawn_and_the_registry_are_one_shape_in_the_three_trees():
    defs = {name: _definitions(path) for name, path in COPIES.items()}
    for key in ("spawn", "_tasks"):
        shapes = {name: d.get(key) for name, d in defs.items()}
        assert None not in shapes.values(), f"{key} missing: {shapes}"
        assert len(set(shapes.values())) == 1, f"{key} differs between {sorted(shapes)}"
    # The satellite's and the phone's done callback are the same one.
    assert defs["satellite"]["_finished"] == defs["phone"]["_finished"]


@pytest.mark.parametrize("name", sorted(COPIES))
def test_each_copy_names_tracks_and_drops_its_tasks(name):
    mod = _load(name, COPIES[name])

    async def scenario():
        gate = asyncio.Event()

        async def work():
            await gate.wait()

        task = mod.spawn(work(), name="job", owner="test")
        assert task.get_name() == "test:job"
        assert task in mod._tasks
        gate.set()
        await task
        await asyncio.sleep(0)                    # the done callback runs
        return task

    task = asyncio.run(scenario())
    assert task not in mod._tasks


@pytest.mark.parametrize("name", sorted(COPIES))
def test_each_copy_refuses_off_the_loop(name):
    mod = _load(name, COPIES[name])

    async def work():
        return None

    coro = work()
    with pytest.raises(RuntimeError):
        mod.spawn(coro, name="job", owner="test")
    coro.close()
