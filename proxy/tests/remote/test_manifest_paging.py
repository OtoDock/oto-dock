"""A satellite's manifest reply can be paged. The proxy asks a 0.5.130
satellite for pages, joins the frames of one command in order, binds them
to the sending machine, bounds the total and clears the pages when the wait
ends whatever way; an older satellite's single frame resolves at once.
"""

import asyncio

import pytest

from core.remote import remote_workspace_sync as rws
from core.remote.satellite_connection import SatelliteConnection, SatelliteConnectionManager


def _cm_with(version: str, mid: str = "m1") -> SatelliteConnectionManager:
    cm = SatelliteConnectionManager()
    cm._connections[mid] = SatelliteConnection(machine_id=mid, ws=None, satellite_version=version)
    return cm


def _pending(cm, mid="m1", cid="c1"):
    fut = asyncio.get_event_loop().create_future()
    cm._pending_acks[cid] = (mid, fut)
    return fut


def _frame(files, *, page=None, more=None, cid="c1"):
    msg = {"type": "file_manifest", "command_id": cid, "agent_slug": "a", "files": files}
    if page is not None:
        msg["page"] = page
    if more is not None:
        msg["more"] = more
    return msg


@pytest.mark.asyncio
async def test_a_single_frame_resolves_at_once():
    cm = _cm_with("0.5.129")
    fut = _pending(cm)
    cm._on_manifest_frame("m1", _frame([{"path": "a"}]))
    assert fut.done() and fut.result()["files"] == [{"path": "a"}]
    assert cm._manifest_pages == {}


@pytest.mark.asyncio
async def test_pages_are_joined_in_order_and_resolve_on_the_last():
    cm = _cm_with("0.5.130")
    fut = _pending(cm)
    cm._on_manifest_frame("m1", _frame([{"path": "a"}], page=0, more=True))
    cm._on_manifest_frame("m1", _frame([{"path": "b"}], page=1, more=True))
    assert not fut.done() and len(cm._manifest_pages["c1"].entries) == 2
    cm._on_manifest_frame("m1", _frame([{"path": "c"}], page=2, more=False))
    assert fut.done()
    assert [e["path"] for e in fut.result()["files"]] == ["a", "b", "c"]
    assert cm._manifest_pages == {}


@pytest.mark.asyncio
async def test_a_frame_from_another_machine_or_an_unknown_command_is_dropped():
    cm = _cm_with("0.5.130")
    fut = _pending(cm)
    cm._on_manifest_frame("m2", _frame([{"path": "x"}], page=0, more=True))
    cm._on_manifest_frame("m1", _frame([{"path": "x"}], page=0, more=True, cid="nope"))
    assert not fut.done() and cm._manifest_pages == {}
    cm._on_manifest_frame("m1", _frame([{"path": "a"}]))
    assert fut.result()["files"] == [{"path": "a"}]


@pytest.mark.asyncio
async def test_the_total_is_bounded(monkeypatch):
    monkeypatch.setattr(SatelliteConnectionManager, "_MANIFEST_MAX_ENTRIES", 3)
    cm = _cm_with("0.5.130")
    fut = _pending(cm)
    cm._on_manifest_frame("m1", _frame([{"path": "a"}, {"path": "b"}], page=0, more=True))
    cm._on_manifest_frame("m1", _frame([{"path": "c"}, {"path": "d"}], page=1, more=True))
    assert fut.done() and isinstance(fut.exception(), RuntimeError)
    assert cm._manifest_pages == {}


@pytest.mark.asyncio
async def test_the_total_is_bounded_by_its_size_too(monkeypatch):
    """Few entries, each large: the entry ceiling alone would let the joined
    reply grow by a socket-sized frame per page."""
    monkeypatch.setattr(SatelliteConnectionManager, "_MANIFEST_MAX_BYTES", 1000)
    cm = _cm_with("0.5.130")
    fut = _pending(cm)
    big = [{"path": "x" * 600}]
    cm._on_manifest_frame("m1", _frame(big, page=0, more=True))
    assert not fut.done()
    cm._on_manifest_frame("m1", _frame(big, page=1, more=True))
    assert fut.done() and isinstance(fut.exception(), RuntimeError)
    assert cm._manifest_pages == {}


@pytest.mark.asyncio
async def test_a_missing_or_repeated_page_fails_the_wait():
    """A page lost on the way (the satellite's send queue drops its oldest
    frame when full) would leave a joined reply that reads as complete, and
    the reconnect merge would delete what the lost page listed. The wait
    fails instead, so the merge skips."""
    cm = _cm_with("0.5.130")
    gap = _pending(cm, cid="gap")
    cm._on_manifest_frame("m1", _frame([{"path": "a"}], page=0, more=True, cid="gap"))
    cm._on_manifest_frame("m1", _frame([{"path": "c"}], page=2, more=False, cid="gap"))
    assert gap.done() and isinstance(gap.exception(), RuntimeError)
    late = _pending(cm, cid="late")
    cm._on_manifest_frame("m1", _frame([{"path": "b"}], page=1, more=False, cid="late"))
    assert late.done() and isinstance(late.exception(), RuntimeError)
    again = _pending(cm, cid="again")
    cm._on_manifest_frame("m1", _frame([{"path": "a"}], page=0, more=True, cid="again"))
    cm._on_manifest_frame("m1", _frame([{"path": "a"}], page=0, more=False, cid="again"))
    assert again.done() and isinstance(again.exception(), RuntimeError)
    assert cm._manifest_pages == {}
    # A one-page reply is page 0 and resolves.
    one = _pending(cm, cid="one")
    cm._on_manifest_frame("m1", _frame([{"path": "a"}], page=0, more=False, cid="one"))
    assert one.result()["files"] == [{"path": "a"}]


@pytest.mark.asyncio
async def test_the_pages_are_cleared_when_the_wait_ends_without_a_last_frame():
    cm = _cm_with("0.5.130")
    cm._manifest_pages["c1"] = [{"path": "a"}]
    _pending(cm)
    await cm.deregister("m1")
    assert cm._manifest_pages == {}
    # A timed-out send_command clears its pages too.
    cm = _cm_with("0.5.130")

    class _Conn:
        satellite_version = "0.5.130"

        async def enqueue_send(self, msg, **kw):
            cm._manifest_pages[msg["command_id"]] = [{"path": "a"}]

    cm._connections["m1"] = _Conn()
    with pytest.raises(RuntimeError):
        await cm.send_command("m1", {"type": "request_manifest"}, timeout=0.01)
    assert cm._manifest_pages == {}


def test_the_request_asks_for_pages_only_from_a_satellite_that_pages():
    assert rws.manifest_request(_cm_with("0.5.129"), "m1", "a") == {
        "type": "request_manifest", "agent_slug": "a"}
    req = rws.manifest_request(_cm_with("0.5.130"), "m1", "a")
    assert req["page_size"] == rws._MANIFEST_PAGE_SIZE and 256 <= req["page_size"] <= 8192
    assert _cm_with("0.5.130").satellite_supports_paged_manifest("m1")
    assert not _cm_with("0.5.129").satellite_supports_paged_manifest("m1")


@pytest.mark.asyncio
async def test_the_other_manifest_senders_ask_a_paging_satellite_for_pages(monkeypatch):
    """The file listing and the plans walk send the same paged request as
    the merge, through the manager's own command path (the joined pages
    resolve that wait)."""
    from types import SimpleNamespace
    from api.sessions import sessions as sessions_api
    from core.remote import remote_file_flow as rff

    cm = _cm_with("0.5.130")
    sent: list[tuple[str, dict]] = []

    async def send_command(machine_id, msg, *, timeout=30.0, command_id=None):
        sent.append((machine_id, dict(msg)))
        return {"type": "file_manifest", "files": [
            {"path": "workspace/.claude/plans/p.md", "mtime": 5.0, "size": 7},
            {"path": "workspace/notes/a.txt", "mtime": 1.0, "size": 1},
        ]}
    monkeypatch.setattr(cm, "send_command", send_command)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    info = SimpleNamespace(machine_id="m1", agent_name="a")
    monkeypatch.setattr(rff, "_get_remote_session_info", lambda sid: info)
    monkeypatch.setattr(sessions_api, "_get_remote_session_info", lambda sid: info)
    monkeypatch.setattr("core.session.session_state._session_security", {})

    assert await rff.list_remote_files("s1", "workspace/notes") == ["workspace/notes/a.txt"]
    plans = await sessions_api._list_remote_plans("s1")
    assert plans == [{"filename": "p.md", "modified": 5.0, "size": 7}]
    assert [m for _mid, m in sent] == [rws.manifest_request(cm, "m1", "a")] * 2
    assert all(mid == "m1" for mid, _m in sent)
    assert cm._pending_acks == {}
