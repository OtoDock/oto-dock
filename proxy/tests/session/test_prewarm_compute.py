"""A pre-warm follows its target: it is built through the builders, so on
the compute machine that serves the platform's place when one does, and a
held one is reused only while a new chat would start on the same side of
that place (``ws/dashboard_warmup_tail._built_where_it_starts``): one person's
sessions never sit half in the proxy and half on the compute machine."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import placement
from core.session import session_manager
from ws import dashboard_warmup_tail as tail

COMPUTE = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute")


def _world(monkeypatch, *, serving: bool, held_on: str | None):
    monkeypatch.setattr(session_manager, "compute_placement_for",
                        lambda _path, **_kw: COMPUTE if serving else None)
    held = None if held_on is None else SimpleNamespace(machine_id=held_on)
    monkeypatch.setattr(session_manager, "live_session", lambda _sid: held)


@pytest.mark.parametrize("serving, held_on, reused", [
    (False, "", True),        # in the proxy, and the proxy serves: reused
    (True, "c-1", True),      # on the compute machine that serves now: reused
    (True, "", False),        # made before the compute machine signed in
    (False, "c-1", False),    # made on a compute machine that left
    (True, "c-2", False),     # another compute machine
    (False, None, True),      # nobody holds it: the liveness check decides
])
def test_a_prewarm_is_reused_only_on_the_side_a_new_chat_starts(monkeypatch, serving, held_on, reused):
    _world(monkeypatch, serving=serving, held_on=held_on)
    assert tail._built_where_it_starts("pw-1", "claude-code-cli") is reused


def test_the_held_prewarm_is_judged_by_where_the_platforms_place_runs_now(monkeypatch):
    """``_prewarm_on_its_place`` (the eager pre-warm's reuse) asks the same
    question once the place is the platform's."""
    _world(monkeypatch, serving=True, held_on="")
    host = SimpleNamespace(_pre_warmed_sid="pw-1", user_sub="sub-1")

    async def _place(*_a, **_k):
        return placement.LOCAL, None
    host._default_place_now = _place
    on_place = tail.WarmupTail._prewarm_on_its_place
    assert asyncio.run(on_place(host, "personal-assistant", "manager", "claude-code-cli", "")) is False
    _world(monkeypatch, serving=True, held_on="c-1")
    assert asyncio.run(on_place(host, "personal-assistant", "manager", "claude-code-cli", "")) is True


@pytest.mark.parametrize("held_on, own, expect", [
    ("c-2", {"c-2"}, "c-2"),   # on a machine with its own copy: the chat records it
    ("c-1", set(), ""),        # on a machine that shares the folders: none
    ("", set(), ""),           # in the proxy
    (None, set(), ""),         # nobody holds it
])
def test_a_chat_that_adopts_a_prewarm_records_where_it_runs(monkeypatch, held_on, own, expect):
    _world(monkeypatch, serving=True, held_on=held_on)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                        lambda: SimpleNamespace(owns_copy=lambda mid: mid in own))
    assert session_manager.own_copy_machine_of("pw-1") == expect


def test_the_adoption_binds_the_session_and_its_machine_in_one_job(monkeypatch):
    from storage import database, remote_store
    writes: list = []
    monkeypatch.setattr(database, "update_chat", lambda cid, **kw: writes.append(("chat", cid, kw)))
    monkeypatch.setattr(remote_store, "set_chat_compute_machine", lambda cid, m: writes.append(("machine", cid, m)))
    monkeypatch.setattr(session_manager, "own_copy_machine_of", lambda sid: "c-2" if sid == "pw-1" else "")
    tail._bind_reused("chat-1", session_id="pw-1", execution_target="local")
    assert writes == [("chat", "chat-1", {"session_id": "pw-1", "execution_target": "local"}),
                      ("machine", "chat-1", "c-2")]
