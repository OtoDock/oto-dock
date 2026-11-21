"""A move's copy and its proof: the copy waits for the engine's last lines
and tries once more when the source changed under it
(``core/remote/session_files.copy_session``, ``services/remote/chat_move._run``),
and the proof sweep waits a bounded time on each machine
(``chat_move.sweep``)."""

import asyncio
import time
import uuid

from services.remote import chat_move
from storage import move_schema
from tests._timing import bound
from tests.remote.test_chat_move import (  # noqa: F401  (``online`` is a fixture)
    _agent,
    _awaiting,
    _chat,
    _machine,
    online,
)

# -- the proof sweep waits a bounded time on each machine ----------------------------


def test_a_silent_machine_holds_no_other_rows_proof(temp_db, online, monkeypatch):  # noqa: F811
    """A target whose size read never answers is asked again later; the
    sweep goes on to the next row within the bound."""
    from core.remote import session_files as sf
    from services.remote import switch_leftovers
    from storage import remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-bob", "Bob laptop", owner="u-bob")
    online.update({"m-ann", "m-bob"})
    slug = _agent()
    silent_sid, fine_sid = str(uuid.uuid4()), str(uuid.uuid4())
    silent = _awaiting(_chat(slug, "u-ann", pin="m-ann", sid=silent_sid), sid=silent_sid,
                       target="m-ann")
    fine = _awaiting(_chat(slug, "u-bob", pin="m-bob", sid=fine_sid), sid=fine_sid,
                     target="m-bob")
    monkeypatch.setattr(switch_leftovers, "DELETE_WAIT_S", 0.3)

    async def main_size(place, scope, rel):
        if place == "m-ann":
            await asyncio.sleep(3600)
        return 99

    async def delete_files(place, scope):
        return 1
    monkeypatch.setattr(sf, "main_size", main_size)
    monkeypatch.setattr(sf, "delete_files", delete_files)
    chat_move._proof_next.clear()
    started = time.monotonic()
    asyncio.run(asyncio.wait_for(chat_move.sweep(), timeout=30))
    assert time.monotonic() - started < bound(5)
    assert remote_store.get_chat_move(silent)["state"] == move_schema.AWAITING_RESUME
    assert silent in chat_move._proof_next                 # asked again after its back-off
    assert remote_store.get_chat_move(fine)["state"] == move_schema.SETTLED


def test_a_silent_source_holds_its_delete_back_and_nothing_else(temp_db, online, monkeypatch):  # noqa: F811
    from core.remote import session_files as sf
    from services.remote import switch_leftovers
    from storage import remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    sid = str(uuid.uuid4())
    cid = _chat(_agent(), "u-ann", pin="local", sid=sid)
    mid = _awaiting(cid, sid=sid, source="m-ann", target="local")
    monkeypatch.setattr(switch_leftovers, "DELETE_WAIT_S", 0.3)

    async def main_size(place, scope, rel):
        return 99

    async def delete_files(place, scope):
        await asyncio.sleep(3600)
    monkeypatch.setattr(sf, "main_size", main_size)
    monkeypatch.setattr(sf, "delete_files", delete_files)
    chat_move._proof_next.clear()
    asyncio.run(asyncio.wait_for(chat_move.sweep(), timeout=30))
    assert remote_store.get_chat_move(mid)["state"] == move_schema.AWAITING_RESUME
    assert mid in chat_move._proof_next


# -- the copy marks a source that changed under it ---------------------------------------

def test_a_platform_file_that_grew_after_its_list_is_marked_changed(monkeypatch):
    """The engine wrote its last lines between the list and the stage: the
    copy says the source changed, before a byte is pushed."""
    from core.remote import session_files as sf
    from tests.remote.test_session_files_local import (
        _home,
        _Remote,
        _scope,
        _transcript,
    )
    s = _scope()
    _transcript(s, side=False)
    remote = _Remote(monkeypatch, {})
    real_stage = sf.local_stage_from_home

    def stage_after_a_write(scope, move_id, rel):
        with open(_home(scope) / rel, "ab") as fh:
            fh.write(b'{"type":"assistant"}\n')
        return real_stage(scope, move_id, rel)
    monkeypatch.setattr(sf, "local_stage_from_home", stage_after_a_write)
    try:
        asyncio.run(sf.copy_session(s, source="local", target="m-1", move_id=str(uuid.uuid4())))
    except sf.TransferError as e:
        assert e.changed and not e.committed
    else:
        raise AssertionError("the copy went ahead on a file that grew")
    assert remote.staged == {}


def test_a_machine_file_that_grew_before_its_pull_is_marked_changed(monkeypatch):
    """The pull stays capped at the listed size (a source lands no more than
    it listed) and fails; the source then lists differently, so the copy
    says it changed."""
    from core.remote import session_files as sf
    from tests.remote.test_session_files_local import _Remote, _scope
    s = _scope()
    rel = f".claude/projects/k/{s.session_id}.jsonl"
    remote = _Remote(monkeypatch, {rel: b"v1"})

    async def capped_pull(machine_id, scope, move_id, f, **kw):
        remote.files[rel] = b"v1 and the engine's last lines"
        raise sf.TransferError("transfer_failed", f"pull of {f.rel} failed")
    monkeypatch.setattr(sf, "remote_pull", capped_pull)
    try:
        asyncio.run(sf.copy_session(s, source="m-1", target="local", move_id=str(uuid.uuid4())))
    except sf.TransferError as e:
        assert e.changed
    else:
        raise AssertionError("the copy went ahead")

    async def failed_pull(machine_id, scope, move_id, f, **kw):
        raise sf.TransferError("transfer_failed", f"pull of {f.rel} failed")
    monkeypatch.setattr(sf, "remote_pull", failed_pull)
    try:
        asyncio.run(sf.copy_session(s, source="m-1", target="local", move_id=str(uuid.uuid4())))
    except sf.TransferError as e:
        assert not e.changed              # the same list: the link, not the engine
    else:
        raise AssertionError("the copy went ahead")


def test_the_wait_ends_once_the_sizes_stop_moving(monkeypatch):
    from core.remote import session_files as sf
    from tests.remote.test_session_files_local import _scope
    s = _scope()
    rel = f".claude/projects/k/{s.session_id}.jsonl"
    reads = []

    async def remote_stat(machine_id, scope):
        reads.append(1)
        return [(rel, min(len(reads), 4) * 10)]
    monkeypatch.setattr(sf, "remote_stat", remote_stat)
    assert asyncio.run(sf.wait_quiet("m-1", s, quiet_s=0.2, every_s=0.05, bound_s=5.0))
    assert len(reads) >= 4 + 4               # it grew for four reads, then held for 0.2 s

    async def always_growing(machine_id, scope):
        reads.append(1)
        return [(rel, len(reads))]
    monkeypatch.setattr(sf, "remote_stat", always_growing)
    started = time.monotonic()
    assert not asyncio.run(sf.wait_quiet("m-1", s, quiet_s=0.2, every_s=0.05, bound_s=0.6))
    assert time.monotonic() - started < bound(3)

    async def no_file(machine_id, scope):
        return []
    monkeypatch.setattr(sf, "remote_stat", no_file)
    started = time.monotonic()
    assert asyncio.run(sf.wait_quiet("m-1", s, quiet_s=5.0, bound_s=10.0))
    assert time.monotonic() - started < bound(1)


# -- the job tries once more ------------------------------------------------------------


class _CopyTwice:
    """``session_files`` with a copy that fails as told, then works."""

    def __init__(self, monkeypatch, first):
        from core.remote import session_files as sf
        self.stages, self.quiet, self.discarded = [], [], []

        async def copy_session(scope, *, source, target, move_id, progress=None):
            self.stages.append(move_id)
            if len(self.stages) == 1 and first is not None:
                raise first
            return sf.CopyResult(files=[sf.SessionFile(f".claude/projects/k/{scope.session_id}.jsonl",
                                                       10, "sha256:x")],
                                 bytes=10, main_rel=f".claude/projects/k/{scope.session_id}.jsonl",
                                 main_size=10)

        async def wait_quiet(place, scope, **kw):
            self.quiet.append(place)
            return True

        async def discard(place, scope, move_id):
            self.discarded.append((place, move_id))

        async def delete_files(place, scope):
            return 1
        monkeypatch.setattr(sf, "copy_session", copy_session)
        monkeypatch.setattr(sf, "wait_quiet", wait_quiet)
        monkeypatch.setattr(sf, "discard", discard)
        monkeypatch.setattr(sf, "delete_files", delete_files)


def _move(cid, target="m-ann"):
    from tests.remote.test_chat_move import _asker, _settled

    async def go():
        await chat_move.request_move(cid, target, asker=_asker("u-ann", "manager"))
        await _settled(cid)
    asyncio.run(go())


def test_a_copy_the_engine_wrote_under_is_tried_once_more_and_keeps_the_memory(
        temp_db, online, monkeypatch):  # noqa: F811
    from core.remote import session_files as sf
    from storage import database as db, remote_store
    from tests.remote.test_chat_move import _user
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    sid = str(uuid.uuid4())
    cid = _chat(_agent(), "u-ann", sid=sid)
    files = _CopyTwice(monkeypatch, sf.TransferError(
        "transfer_failed", "the session changed at the source during the move", changed=True))
    _move(cid)
    move = remote_store.latest_chat_move(cid)
    assert move["memory"] == move_schema.MEMORY_FILES and move["state"] == move_schema.AWAITING_RESUME
    assert db.get_chat(cid)["session_id"] == sid
    assert len(files.stages) == 2 and files.stages[0] == move["id"] != files.stages[1]
    assert files.quiet == ["local", "local"]            # before each try
    assert ("m-ann", files.stages[0]) in files.discarded  # the first try's staging went


def test_a_second_change_still_moves_on_a_summary(temp_db, online, monkeypatch):  # noqa: F811
    from core.remote import session_files as sf
    from storage import remote_store
    from tests.remote.test_chat_move import _user
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    files = _CopyTwice(monkeypatch, None)

    async def always_changed(scope, *, source, target, move_id, progress=None):
        files.stages.append(move_id)
        raise sf.TransferError("transfer_failed", "changed", changed=True)
    monkeypatch.setattr(sf, "copy_session", always_changed)
    _move(cid)
    assert len(files.stages) == 2
    assert remote_store.latest_chat_move(cid)["memory"] == move_schema.MEMORY_SUMMARY


def test_busy_is_tried_once_more_only_at_a_source_this_move_just_closed(
        temp_db, online, monkeypatch):  # noqa: F811
    from core.remote import session_files as sf
    from storage import remote_store
    from tests.remote.test_chat_move import _user
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-two", "Ann desktop", owner="u-ann")
    online.update({"m-ann", "m-two"})
    slug = _agent()

    async def closed_one(chat):
        return True
    monkeypatch.setattr(chat_move, "_close_live", closed_one)
    cid = _chat(slug, "u-ann", pin="m-two", sid=str(uuid.uuid4()))
    files = _CopyTwice(monkeypatch, sf.TransferError(sf.WHY_BUSY, "busy", place="m-two"))
    _move(cid)
    assert len(files.stages) == 2
    assert remote_store.latest_chat_move(cid)["memory"] == move_schema.MEMORY_FILES
    # A session open at the TARGET is someone's: never tried again.
    cid = _chat(slug, "u-ann", pin="m-two", sid=str(uuid.uuid4()))
    files = _CopyTwice(monkeypatch, sf.TransferError(sf.WHY_BUSY, "busy", place="m-ann"))
    _move(cid)
    assert len(files.stages) == 1
    assert remote_store.latest_chat_move(cid)["state"] == move_schema.FAILED
