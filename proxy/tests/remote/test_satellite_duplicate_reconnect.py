"""Duplicate-reconnect race on the satellite connection registry.

When a satellite reconnects while the proxy still holds its old socket,
``register()`` replaces the dict entry and closes the old ws — and the OLD
handler's ``finally: deregister(...)`` then fires. Pre-fix it popped the
entry unconditionally, unregistering the LIVE connection and marking the
machine offline while the satellite kept its healthy new socket ("all
remote machines down" on the proxy, every satellite showing connected).

Status writes are OFF-loop since 2026-09-04 (the per-machine persister, see
``SatelliteConnectionManager._request_persist``): tests ``drain_persists()``
before asserting on the store mock, and the mock is patched at
``storage.remote_store`` because the persister resolves it at call time.
"""

import asyncio
from unittest.mock import patch

import pytest

from core.remote.satellite_connection import SatelliteConnectionManager


class _FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self, code=1000, reason=""):
        self.closed = True

    async def send_text(self, text):
        pass


def _register(mgr, machine_id, ws):
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        return asyncio.get_event_loop().run_until_complete(
            _register_async(mgr, machine_id, ws)
        )


async def _register_async(mgr, machine_id, ws):
    return await mgr.register(machine_id, ws, {})


@pytest.mark.asyncio
async def test_stale_deregister_does_not_evict_new_connection():
    mgr = SatelliteConnectionManager()
    with patch("storage.remote_store.update_machine_status") as status, \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        old_ws, new_ws = _FakeWS(), _FakeWS()
        old_conn = await mgr.register("m1", old_ws, {})
        new_conn = await mgr.register("m1", new_ws, {})  # duplicate
        assert old_ws.closed  # old socket closed by register
        assert await mgr.drain_persists()

        # The OLD handler's finally fires with ITS connection → no-op.
        status.reset_mock()
        await mgr.deregister("m1", expected=old_conn)
        assert await mgr.drain_persists()
        assert mgr.get_connection("m1") is new_conn
        # No "disconnected" status write from the stale path.
        assert not any(
            c.args[1] == "disconnected" for c in status.call_args_list
        )

        # The CURRENT handler's deregister still tears down for real.
        await mgr.deregister("m1", expected=new_conn)
        assert await mgr.drain_persists()
        assert mgr.get_connection("m1") is None
        assert status.call_args_list[-1].args[:2] == ("m1", "disconnected")


@pytest.mark.asyncio
async def test_duplicate_register_carries_inflight_sessions():
    mgr = SatelliteConnectionManager()
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        old_conn = await mgr.register("m1", _FakeWS(), {})
        q = asyncio.Queue()
        old_conn.session_queues["sid-1"] = q
        old_conn.session_execution_paths["sid-1"] = "claude-code-cli"

        new_conn = await mgr.register("m1", _FakeWS(), {})
        # Same queue OBJECT — producers keep their reference.
        assert new_conn.session_queues.get("sid-1") is q
        assert new_conn.session_execution_paths.get("sid-1") == "claude-code-cli"
        # Old writer task cancelled by register (its handler's deregister
        # is a no-op now).
        await asyncio.sleep(0)
        assert old_conn.writer_task.cancelled() or old_conn.writer_task.done()
        await mgr.deregister("m1", expected=new_conn)
        assert await mgr.drain_persists()


@pytest.mark.asyncio
async def test_unguarded_deregister_keeps_old_behavior():
    mgr = SatelliteConnectionManager()
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        await mgr.register("m1", _FakeWS(), {})
        await mgr.deregister("m1")  # no expected → unconditional
        assert mgr.get_connection("m1") is None
        assert await mgr.drain_persists()


@pytest.mark.asyncio
async def test_a_replacement_fails_what_waits_on_the_old_connection(tmp_path, monkeypatch):
    """The old socket's acks, pulls and credit fail by identity at the swap,
    at once, and a push on it aborts: nothing waits out a timeout on a
    socket that can never answer. What the new connection sends is kept."""
    from core.remote.satellite_connection import CreditFailed
    from services.path_policy_v2 import PathRef
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4)
    mgr = SatelliteConnectionManager()
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        old_conn = await mgr.register("m1", _FakeWS(), {})
        push = asyncio.create_task(mgr.push_file(
            "m1", PathRef("agent_tree", "w.bin"), b"x" * 64, agent_slug="a1"))
        pull = asyncio.create_task(mgr.pull_file_to_path(
            "m1", PathRef("agent_tree", "w.bin"), tmp_path / "w.bin", agent_slug="a1"))
        command = asyncio.create_task(mgr.send_command("m1", {"type": "file_stat"}, timeout=60))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(mgr._pending_pulls) == 1 and old_conn.bulk_credit.inflight:
                break
        assert old_conn.bulk_credit.inflight > 0

        new_conn = await mgr.register("m1", _FakeWS(), {})
        later = asyncio.create_task(mgr.send_command("m1", {"type": "file_stat"}, timeout=60))
        assert await asyncio.wait_for(push, 2) is False
        assert await asyncio.wait_for(pull, 2) is False
        with pytest.raises(RuntimeError, match="replaced"):
            await asyncio.wait_for(command, 2)
        with pytest.raises(CreditFailed):
            old_conn.bulk_credit.try_take(1)
        await asyncio.sleep(0)
        assert old_conn.bulk_credit.inflight == 0
        assert mgr._pending_pulls == {}
        # Only the new socket's commands wait (its own verify kick and this).
        assert mgr._pending_acks
        assert all(e[2] is new_conn for e in mgr._pending_acks.values())
        later.cancel()
        await mgr.deregister("m1", expected=new_conn)
        assert await mgr.drain_persists()


@pytest.mark.asyncio
async def test_a_transfer_that_starts_across_a_replacement_uses_the_new_connection(tmp_path, monkeypatch):
    """A duplicate reconnect lands while a push hashes its file: the push
    goes out on the new connection instead of failing on the replaced one."""
    import json
    from services.path_policy_v2 import PathRef

    class _RecWS(_FakeWS):
        def __init__(self):
            super().__init__()
            self.sent: list[dict] = []

        async def send_text(self, text):
            self.sent.append(json.loads(text))

    mgr = SatelliteConnectionManager()
    src = tmp_path / "doc.bin"
    src.write_bytes(b"x" * 64)
    new_ws = _RecWS()
    conns: list = []

    def _hash_during_reconnect(path):
        conns.append(asyncio.run_coroutine_threadsafe(
            _register_async(mgr, "m1", new_ws), loop).result())
        return "sha256:" + "0" * 64
    with patch("storage.remote_store.update_machine_status"), \
         patch("storage.remote_store.update_machine_capabilities"), \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        loop = asyncio.get_running_loop()
        old_ws = _RecWS()
        old_conn = await mgr.register("m1", old_ws, {})
        monkeypatch.setattr("core.remote.file_sync._hash_file", _hash_during_reconnect)
        push = asyncio.create_task(mgr.push_file(
            "m1", PathRef("agent_tree", "w.bin"), src, agent_slug="a1"))
        frames: list[dict] = []
        for _ in range(200):
            await asyncio.sleep(0.01)
            frames = [f for f in new_ws.sent if f.get("type") == "file_push"]
            if frames:
                break
        new_conn = conns[0]
        assert new_conn is not old_conn and len(frames) == 1
        assert not [f for f in old_ws.sent if f.get("type") == "file_push"]
        assert old_conn.bulk_credit.inflight == 0
        # Answer the frame on the new connection: the push lands.
        await mgr.handle_message("m1", {"type": "ack", "command_id": frames[0]["command_id"],
                                        "status": "ok", "error": ""})
        assert await asyncio.wait_for(push, 2) is True
        await mgr.deregister("m1", expected=new_conn)
        assert await mgr.drain_persists()
