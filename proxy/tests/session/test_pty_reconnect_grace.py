"""Interactive remote-PTY satellite-reconnect grace.

A satellite↔proxy WS blip must NOT tear down a live remote interactive terminal.
On a drop the proxy holds the machine's ``RemotePtyProcess`` handles (kept in
place in ``core.remote.remote_pty``'s registry) + starts a per-machine grace timer
instead of killing them; the satellite's post-auth ``pty_alive`` reconciles them
on reconnect (re-adopt survivors, exit the dead, close orphans), and
``_expire_pty_grace`` tears them down only if the window lapses.

Runnable two ways: under pytest (when the proxy is NOT live), or standalone via
``python tests/session/test_pty_reconnect_grace.py`` (no pytest/conftest → no DB pool, so
it is safe while the proxy is live).
"""

import asyncio
from unittest.mock import AsyncMock

import pytest


def _fake_rp(machine_id, session_id, on_exit):
    """A real RemotePtyProcess (registers itself in remote_pty._remote_ptys) with
    recorder callbacks and no live WS — exactly what the reconcile path drives."""
    from core.remote import remote_pty
    return remote_pty.RemotePtyProcess(
        machine_id=machine_id, session_id=session_id, rows=24, cols=80,
        on_output=lambda d: None, on_exit=on_exit, scrollback_limit=1000,
    )


class _FakeSess:
    def __init__(self):
        self.statuses = []

    def notify_status(self, state):
        self.statuses.append(state)


@pytest.mark.asyncio
async def test_reconcile_classifies_and_acts():
    """reconcile_machine_ptys splits held vs satellite-live into re-adopt / exit /
    orphan, fires on_exit for the dead, and keeps the survivors registered."""
    from core.remote import remote_pty

    exits = []
    _fake_rp("m", "s_alive", on_exit=lambda c: exits.append("s_alive"))
    _fake_rp("m", "s_dead", on_exit=lambda c: exits.append("s_dead"))
    try:
        # Satellite still runs s_alive + an orphan s_orphan; s_dead is gone.
        readopted, exited, orphans = remote_pty.reconcile_machine_ptys(
            "m", ["s_alive", "s_orphan"])
        assert readopted == ["s_alive"]
        assert exited == ["s_dead"]
        assert orphans == ["s_orphan"]
        assert exits == ["s_dead"]                              # dead → on_exit
        assert ("m", "s_dead") not in remote_pty._remote_ptys   # popped
        assert ("m", "s_alive") in remote_pty._remote_ptys      # re-adopted (kept)
    finally:
        remote_pty._remote_ptys.pop(("m", "s_alive"), None)
        remote_pty._remote_ptys.pop(("m", "s_dead"), None)


@pytest.mark.asyncio
async def test_an_exit_of_an_earlier_open_never_ends_the_pty_started_again(monkeypatch):
    """The machine reports an exit after its own teardown and keeps its
    terminals by session id: every open carries a token, the exit names it,
    and the exit of the PTY closed under an id never reaches the one started
    again under that id. A handle taken back (no token) takes the id's exit."""
    from core.remote import remote_pty

    monkeypatch.setattr(remote_pty.RemotePtyProcess, "_send_ff", lambda self, msg: None)
    exits = []
    old = remote_pty.RemotePtyProcess(
        machine_id="mr", session_id="s1", rows=24, cols=80, on_output=lambda d: None,
        on_exit=lambda c: exits.append(("old", c)), scrollback_limit=1000, open_token="tok-old")
    old.close()
    new = remote_pty.RemotePtyProcess(
        machine_id="mr", session_id="s1", rows=24, cols=80, on_output=lambda d: None,
        on_exit=lambda c: exits.append(("new", c)), scrollback_limit=1000, open_token="tok-new")
    try:
        remote_pty.feed_exit("mr", "s1", 0, "tok-old")      # the closed PTY's exit, late
        assert exits == [("old", None)] and not new.closed
        remote_pty.feed_exit("mr", "s1", 3, "tok-new")      # the new PTY's own exit
        assert exits == [("old", None), ("new", 3)] and new.closed
        assert ("mr", "s1") not in remote_pty._remote_ptys
        taken = remote_pty.adopt_remote_pty(machine_id="mr", session_id="s2", rows=24, cols=80,
                                            on_output=lambda d: None, on_exit=lambda c: exits.append(("taken", c)))
        assert taken.open_token == ""
        remote_pty.feed_exit("mr", "s2", 0, "tok-any")      # a taken-back PTY has no token of its own
        assert exits[-1] == ("taken", 0)
        ended = remote_pty.RemotePtyProcess(
            machine_id="mr", session_id="s3", rows=24, cols=80, on_output=lambda d: None,
            on_exit=lambda c: exits.append(("memory", c, ended.exit_reason)), scrollback_limit=1000,
            open_token="tok-3")
        remote_pty.feed_exit("mr", "s3", -9, "tok-3", reason="memory")   # the guard ended its engine
        assert exits[-1] == ("memory", -9, "memory")
    finally:
        remote_pty._remote_ptys.pop(("mr", "s1"), None)
        remote_pty._remote_ptys.pop(("mr", "s2"), None)


@pytest.mark.asyncio
async def test_the_open_and_the_close_name_the_token(monkeypatch):
    from core.remote import remote_pty, satellite_connection
    sent = []

    class _CM:
        async def send_command(self, machine_id, msg, timeout):
            sent.append(msg)
            return {"pid": 7, "open_token": msg["open_token"]}

        async def send_fire_and_forget(self, machine_id, msg):
            sent.append(msg)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", _CM)
    try:
        rp = await remote_pty.spawn_remote_pty(
            machine_id="mt", session_id="s1", agent_slug="a", execution_path="claude-code-cli",
            config_payload={}, rows=24, cols=80, on_output=lambda d: None)
        assert len(rp.open_token) == 16 and sent[0]["open_token"] == rp.open_token
        rp.close()
        await asyncio.sleep(0)
        assert sent[-1] == {"type": "pty_close", "session_id": "s1", "open_token": rp.open_token}
    finally:
        remote_pty._remote_ptys.pop(("mt", "s1"), None)


@pytest.mark.asyncio
async def test_machine_pty_queries():
    from core.remote import remote_pty

    assert not remote_pty.has_machine_ptys("mq")
    _fake_rp("mq", "s1", on_exit=lambda c: None)
    try:
        assert remote_pty.has_machine_ptys("mq")
        assert remote_pty.machine_pty_session_ids("mq") == {"s1"}
    finally:
        remote_pty._remote_ptys.pop(("mq", "s1"), None)


@pytest.mark.asyncio
async def test_reconcile_ptys_readopts_and_cancels_timer():
    """A reconnect with the session still alive: re-adopt (keep the handle),
    cancel the grace timer, notify the viewer "reconnected", reap no orphan."""
    from core.remote import remote_pty
    from core.remote.satellite_connection import SatelliteConnectionManager
    from core.session import interactive_session

    cm = SatelliteConnectionManager()
    cm.send_fire_and_forget = AsyncMock()
    _fake_rp("m3", "s3", on_exit=lambda c: None)

    async def _never():
        await asyncio.sleep(3600)

    cm._pty_grace_timers["m3"] = asyncio.create_task(_never())
    fake = _FakeSess()
    interactive_session._sessions["s3"] = fake
    try:
        await cm._reconcile_ptys("m3", ["s3"])
        assert not cm.is_pty_in_grace("m3")                 # timer cancelled+popped
        assert ("m3", "s3") in remote_pty._remote_ptys      # re-adopted (kept)
        assert fake.statuses == ["reconnected"]
        cm.send_fire_and_forget.assert_not_awaited()        # no orphan close
    finally:
        interactive_session._sessions.pop("s3", None)
        remote_pty._remote_ptys.pop(("m3", "s3"), None)


@pytest.mark.asyncio
async def test_reconcile_ptys_exits_dead_and_closes_orphan():
    """A reconnect where our held session died (gone from pty_alive) AND the
    satellite still runs one we no longer track: exit the dead, pty_close the
    orphan (kills the reverse-orphan / covers a proxy restart)."""
    from core.remote import remote_pty
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    cm.send_fire_and_forget = AsyncMock()
    exits = []
    _fake_rp("m4", "s_dead", on_exit=exits.append)
    try:
        await cm._reconcile_ptys("m4", ["s_orphan"])
        assert exits == [None]                                   # s_dead exited
        assert ("m4", "s_dead") not in remote_pty._remote_ptys
        cm.send_fire_and_forget.assert_awaited_once_with(
            "m4", {"type": "pty_close", "session_id": "s_orphan"})
    finally:
        remote_pty._remote_ptys.pop(("m4", "s_dead"), None)


@pytest.mark.asyncio
async def test_expire_pty_grace_terminates():
    """When the window lapses with no reconnect, the held PTYs wait for the
    machine (2.0): nothing is torn down, the viewer keeps its banner, the
    reaper spares them, and the machine's report decides them at its return."""
    from core.remote import remote_pty, satellite_connection as sc
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    exits = []
    _fake_rp("m5", "s5", on_exit=exits.append)
    orig = sc._GRACE_WINDOW_S
    sc._GRACE_WINDOW_S = 0.01
    try:
        cm._pty_grace_timers["m5"] = asyncio.create_task(cm._expire_pty_grace("m5"))
        assert cm.is_pty_in_grace("m5")
        await asyncio.sleep(0.06)
        assert exits == []                                   # nothing torn down
        assert cm.is_pty_in_grace("m5")                      # it waits for the machine
        assert "m5" not in cm._pty_grace_timers and remote_pty.is_machine_away("m5")
        assert ("m5", "s5") in remote_pty._remote_ptys
    finally:
        sc._GRACE_WINDOW_S = orig
        remote_pty._away_since.clear()
        remote_pty._remote_ptys.pop(("m5", "s5"), None)


if __name__ == "__main__":
    # Standalone runner (no pytest/conftest → no DB pool; safe while proxy live).
    import os
    import sys
    import traceback
    # tests/<area>/<file>.py -> proxy/ is 3 levels up.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    _tests = [
        test_reconcile_classifies_and_acts,
        test_machine_pty_queries,
        test_reconcile_ptys_readopts_and_cancels_timer,
        test_reconcile_ptys_exits_dead_and_closes_orphan,
        test_expire_pty_grace_terminates,
    ]
    _fails = 0
    for _t in _tests:
        try:
            asyncio.run(_t())
            print(f"PASS {_t.__name__}")
        except Exception:  # noqa: BLE001  # the script runner reports each failure and runs the rest
            _fails += 1
            traceback.print_exc()
            print(f"FAIL {_t.__name__}")
    print(f"\n{len(_tests) - _fails}/{len(_tests)} passed")
    sys.exit(1 if _fails else 0)
