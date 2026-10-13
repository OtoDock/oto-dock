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
    cm._pending_acks[cid] = (mid, fut, None)
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
async def test_the_file_listing_asks_a_paging_satellite_for_pages(monkeypatch):
    """The file listing sends the same paged request as the merge, through
    the manager's own command path (the joined pages resolve that wait)."""
    from types import SimpleNamespace
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
    monkeypatch.setattr("core.session.session_state._session_security", {})

    assert await rff.list_remote_files("s1", "workspace/notes") == ["workspace/notes/a.txt"]
    assert [m for _mid, m in sent] == [rws.manifest_request(cm, "m1", "a")]
    assert all(mid == "m1" for mid, _m in sent)
    assert cm._pending_acks == {}


@pytest.mark.asyncio
async def test_the_listing_with_sizes_and_the_private_pull(monkeypatch, tmp_path):
    """``list_remote_entries`` carries each file's listed size; ``pull_into``
    pulls one file into a private path for the session's own agent, with
    the caller's byte cap, and refuses a session held for another agent or
    a path that is not canonical without asking the machine."""
    from types import SimpleNamespace
    from core.remote import remote_file_flow as rff
    from core.remote.satellite_connection import SatelliteConnectionManager as _Mgr

    cm = _cm_with("0.5.130")

    async def send_command(machine_id, msg, *, timeout=30.0, command_id=None):
        return {"type": "file_manifest", "files": [
            {"path": "workspace/t/a.md", "mtime": 1.0, "size": 12},
            {"path": "workspace/t/b.md.partial", "mtime": 1.0, "size": 3},
            {"path": "workspace/other.md", "mtime": 1.0, "size": 1},
        ]}

    pulls: list[tuple] = []

    async def pull_file_to_path(machine_id, ref, dest_path, *, agent_slug="",
                                stall_s=None, max_bytes=None):
        pulls.append((machine_id, ref.kind, ref.value, dest_path, agent_slug, max_bytes))
        return True

    # The fake keeps the real method's signature, so a call it would refuse
    # (a missing keyword, a renamed one) fails here too.
    import inspect
    assert inspect.signature(pull_file_to_path).parameters.keys() == \
        {k for k in inspect.signature(_Mgr.pull_file_to_path).parameters if k != "self"}
    monkeypatch.setattr(cm, "send_command", send_command)
    monkeypatch.setattr(cm, "pull_file_to_path", pull_file_to_path)
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    monkeypatch.setattr(rff, "_get_remote_session_info",
                        lambda sid: SimpleNamespace(machine_id="m1", agent_name="a"))

    assert await rff.list_remote_entries("s1", "workspace/t", agent="a") == [
        {"path": "workspace/t/a.md", "size": 12}]
    assert await rff.list_remote_entries("s1", "workspace/t", agent="b") is None

    dest = tmp_path / "snap" / "a.md"
    assert await rff.pull_into("s1", "workspace/t/a.md", dest, agent="a", max_bytes=99)
    assert pulls == [("m1", "agent_tree", "workspace/t/a.md", dest, "a", 99)]
    assert not await rff.pull_into("s1", "workspace/t/a.md", dest, agent="b")
    assert not await rff.pull_into("s1", "workspace/../config/x", dest, agent="a")
    assert len(pulls) == 1


def test_skips_are_asked_of_a_0_5_145_satellite_and_old_windows_absence_is_unknown():
    def cm(version, os_name):
        c = _cm_with(version)
        c._connections["m1"].capabilities = {"os": os_name}
        return c

    assert cm("0.5.145", "linux").satellite_supports_skipped_manifest("m1")
    assert not cm("0.5.144", "linux").satellite_supports_skipped_manifest("m1")
    assert rws.manifest_request(cm("0.5.145", "linux"), "m1", "a",
                                report_skipped=True)["report_skipped"] is True
    assert "report_skipped" not in rws.manifest_request(cm("0.5.145", "linux"), "m1", "a")
    blind = {v: cm(v, "windows").satellite_absence_unreliable("m1")
             for v in ("0.5.129", "0.5.130", "0.5.138", "0.5.143", "0.5.144", "0.5.145")}
    assert blind == {"0.5.129": False, "0.5.130": True, "0.5.138": True,
                     "0.5.143": True, "0.5.144": False, "0.5.145": False}
    assert not cm("0.5.138", "linux").satellite_absence_unreliable("m1")
    assert cm("0.5.145", "windows").satellite_restores_holds("m1")
    assert not cm("0.5.144", "windows").satellite_restores_holds("m1")
    assert not cm("0.5.145", "linux").satellite_restores_holds("m1")
    assert not _cm_with("0.5.145").satellite_absence_unreliable("m-offline")


@pytest.mark.asyncio
async def test_the_listing_asks_a_satellite_that_lists_what_it_skipped(monkeypatch):
    """``report_skipped`` reaches only a satellite that lists what it could
    not read (0.5.145); its skipped entries come back flagged, a folder's
    with ``dir``, inside the prefix or covering it, and an older satellite
    is asked for nothing and lists nothing of the kind."""
    from types import SimpleNamespace
    from core.remote import remote_file_flow as rff

    sent: list[dict] = []
    answer = {"type": "file_manifest", "files": [
        {"path": "workspace/t/a.md", "mtime": 1.0, "size": 12},
        {"path": "workspace/t/locked.md", "skipped": "unreadable", "size": 3, "mtime": 1.0},
        {"path": "workspace/t/sub", "skipped": "unreadable_dir", "dir": True},
        {"path": "workspace", "skipped": "link", "dir": True},
        {"path": "workspace/other.md", "skipped": "unreadable", "size": 1, "mtime": 1.0},
        {"path": "", "skipped": "unreadable_dir", "dir": True},
    ]}

    def _with(version: str):
        cm = _cm_with(version)

        async def send_command(machine_id, msg, *, timeout=30.0, command_id=None):
            sent.append(dict(msg))
            return answer
        monkeypatch.setattr(cm, "send_command", send_command)
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
        return cm

    monkeypatch.setattr(rff, "_get_remote_session_info",
                        lambda sid: SimpleNamespace(machine_id="m1", agent_name="a"))
    monkeypatch.setattr("core.session.session_state._session_security", {})

    _with("0.5.145")
    out = await rff.list_remote_entries("s1", "workspace/t", report_skipped=True)
    assert sent[-1].get("report_skipped") is True
    assert out == [
        {"path": "", "size": 0, "skipped": "unreadable_dir", "dir": True},
        {"path": "workspace", "size": 0, "skipped": "link", "dir": True},
        {"path": "workspace/t/a.md", "size": 12},
        {"path": "workspace/t/locked.md", "size": 0, "skipped": "unreadable"},
        {"path": "workspace/t/sub", "size": 0, "skipped": "unreadable_dir", "dir": True},
    ]
    # Not asked: the plain listing, whatever the satellite.
    assert await rff.list_remote_files("s1", "workspace/t") == ["workspace/t/a.md"]
    assert "report_skipped" not in sent[-1]

    _with("0.5.144")
    out = await rff.list_remote_entries("s1", "workspace/t", report_skipped=True)
    assert "report_skipped" not in sent[-1]
    assert out == [{"path": "workspace/t/a.md", "size": 12}]


def test_absence_is_reliable_follows_the_satellite(monkeypatch):
    from types import SimpleNamespace
    from core.remote import remote_file_flow as rff

    cm = _cm_with("0.5.145")
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    monkeypatch.setattr(rff, "_get_remote_session_info", lambda sid: None)
    assert not rff.absence_is_reliable("s1")
    monkeypatch.setattr(rff, "_get_remote_session_info",
                        lambda sid: SimpleNamespace(machine_id="m1", agent_name="a"))
    assert rff.absence_is_reliable("s1")
    monkeypatch.setattr(cm, "satellite_absence_unreliable", lambda mid: True)
    assert not rff.absence_is_reliable("s1")
