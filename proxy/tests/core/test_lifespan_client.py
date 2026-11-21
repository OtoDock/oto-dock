"""What a test's app startup (a lifespan client) reaches on its host: the autouse
``_no_mcp_installs_at_boot`` of ``tests/conftest.py``, seen from a real client."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import app
from services.mcp import docker_manager, mcp_venv_bootstrap
from services.scheduler import shared


def test_a_lifespan_client_installs_no_mcp_and_starts_no_container(monkeypatch):
    """The real pass builds a venv for every bundled MCP in the checkout before
    the app answers, past the test's timeout on a host with no uv. The real
    bring-up dials the box's container engine. Each of the two is stopped at its
    first step here, so a regression fails this test and builds nothing."""
    # The lifespan's end raises the scheduler's shutdown flag: it comes down
    # with this test.
    monkeypatch.setattr(shared, "_shutting_down", False)
    reached: list[str] = []

    def _refused(step: str):
        def refuse(*_args, **_kwargs):
            reached.append(step)
            raise RuntimeError(f"{step} reached from a test")
        return refuse

    monkeypatch.setattr(mcp_venv_bootstrap, "find_uv", _refused("the install pass"))
    monkeypatch.setattr(docker_manager, "_warn_foreign_mcp_leftovers", _refused("the docker bring-up"))

    with TestClient(app):
        pass

    assert reached == []
