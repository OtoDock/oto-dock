"""Rotation fan-out mechanics (``services/engines/token_fanout``).

Covers the target registry, the one credential-file writer (the file is
named by the ENGINE's declaration, ``LayerCapabilities.auth.credential_file``),
per-directory dedupe across a scope's sessions, and the remote-push grouping
— the parts the pool's rotation chokepoint builds on. The payload a fan-out
delivers is the engine's full file content; this module never shapes it.
"""

import json
import stat
from unittest.mock import patch

import pytest

from services.engines import token_fanout as tf

CLAUDE_FILE = {"claudeAiOauth": {"accessToken": "at", "refreshToken": "", "expiresAt": 5,
                                 "scopes": [], "subscriptionType": "", "rateLimitTier": ""}}
CODEX_FILE = {"auth_mode": "chatgpt", "tokens": {"access_token": "t", "refresh_token": ""}}


def _clean():
    tf._targets.clear()
    tf._missed.clear()


class TestWriter:
    def test_writes_the_payload_verbatim_with_mode_0600(self, tmp_path):
        tf.write_credential_file(tmp_path, ".credentials.json", CLAUDE_FILE)
        path = tmp_path / ".credentials.json"
        assert json.loads(path.read_text()) == CLAUDE_FILE
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_writer_never_follows_a_planted_link(self, tmp_path):
        victim = tmp_path / "victim.env"
        victim.write_text("KEEP=1\n")
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".credentials.json").symlink_to(victim)
        tf.write_credential_file(cfg, ".credentials.json", CLAUDE_FILE)
        assert victim.read_text() == "KEEP=1\n"
        path = cfg / ".credentials.json"
        assert not path.is_symlink() and json.loads(path.read_text()) == CLAUDE_FILE
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_writer_creates_missing_dirs(self, tmp_path):
        target = tmp_path / "users" / "alice" / ".codex"
        tf.write_credential_file(target, "auth.json", CODEX_FILE)
        assert json.loads((target / "auth.json").read_text()) == CODEX_FILE

    def test_a_missing_folder_beneath_an_agent_is_made_with_no_link_followed(self, tmp_path, monkeypatch):
        """In an agent's folder the writer makes a missing folder beneath the
        agent's real folder: a folder link above it refuses the write, and
        the folder the link names gets nothing."""
        import config as app_config
        agents = tmp_path / "agents"
        monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
        (agents / "pa" / "users").mkdir(parents=True)
        tf.write_credential_file(agents / "pa" / "users" / "bob" / ".codex", "auth.json", CODEX_FILE)
        assert json.loads((agents / "pa" / "users" / "bob" / ".codex" / "auth.json").read_text()) == CODEX_FILE
        stand_in = tmp_path / "stand-in"
        stand_in.mkdir()
        (agents / "pa" / "users" / "alice").symlink_to(stand_in)
        with pytest.raises(OSError):
            tf.write_credential_file(agents / "pa" / "users" / "alice" / ".codex", "auth.json", CODEX_FILE)
        assert list(stand_in.iterdir()) == []

    def test_a_renewal_after_a_sessions_close_makes_no_home(self, tmp_path):
        """A session's home is made by its start alone: the writer makes no
        home that is gone, and the fan-out skips one with its line."""
        store = tmp_path / "users" / "alice" / ".claude"
        home = store / "own" / "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
        with pytest.raises(OSError):
            tf.write_credential_file(home, ".credentials.json", CLAUDE_FILE)
        assert not (store / "own").exists()
        _clean()
        tf.register_session_target("s-gone", tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(home)))
        written: list[str] = []
        with patch.object(tf, "write_credential_file", wraps=tf.write_credential_file) as writer:
            tf.fan_out(["s-gone"], layer="claude-code-cli", payload=CLAUDE_FILE, on_written=written.append)
        writer.assert_not_called()
        assert written == [] and not (store / "own").exists()
        home.mkdir(parents=True)
        tf.fan_out(["s-gone"], layer="claude-code-cli", payload=CLAUDE_FILE, on_written=written.append)
        assert written == ["s-gone"] and json.loads((home / ".credentials.json").read_text()) == CLAUDE_FILE
        _clean()


class TestCredentialFileSpec:
    def test_each_cli_engine_declares_its_frozen_row(self):
        claude = tf.credential_file_spec("claude-code-cli")
        assert (claude.wire_kind, claude.dirname, claude.filename) == (
            "claude", ".claude", ".credentials.json")
        codex = tf.credential_file_spec("codex-cli")
        assert (codex.wire_kind, codex.dirname, codex.filename) == (
            "codex", ".codex", "auth.json")

    def test_an_env_delivered_or_unknown_engine_fails_closed(self):
        with pytest.raises(ValueError):
            tf.credential_file_spec("direct-llm")
        from core.session.session_manager import UnknownExecutionPath
        with pytest.raises(UnknownExecutionPath):
            tf.credential_file_spec("acme-cli")


class TestRegistry:
    def setup_method(self):
        _clean()

    def test_register_and_unregister(self):
        t = tf.CredentialFileTarget(layer="claude-code-cli", host_dir="/x")
        tf.register_session_target("s1", t)
        assert tf.session_target("s1") == t
        tf.unregister_session_target("s1")
        assert tf.session_target("s1") is None

    def test_unregister_unknown_is_noop(self):
        tf.unregister_session_target("never-registered")


class TestFanOut:
    def setup_method(self):
        _clean()

    def test_local_write_dedupes_per_scope_dir(self, tmp_path):
        # Two sessions share one scope dir — one write, both callbacks.
        shared = tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(tmp_path))
        tf.register_session_target("s1", shared)
        tf.register_session_target("s2", shared)
        written = []
        tf.fan_out(["s1", "s2"], layer="claude-code-cli", payload=CLAUDE_FILE,
                   on_written=written.append)
        assert sorted(written) == ["s1", "s2"]
        assert json.loads((tmp_path / ".credentials.json").read_text()) == CLAUDE_FILE

    def test_the_engine_names_the_file(self, tmp_path):
        codex_dir = tmp_path / "x"
        tf.register_session_target(
            "s2", tf.CredentialFileTarget(layer="codex-cli", host_dir=str(codex_dir)))
        written = []
        tf.fan_out(["s2"], layer="codex-cli", payload=CODEX_FILE, on_written=written.append)
        assert json.loads((codex_dir / "auth.json").read_text()) == CODEX_FILE
        assert not (codex_dir / ".credentials.json").exists()
        assert written == ["s2"]

    def test_unregistered_sessions_are_skipped(self, tmp_path):
        written = []
        tf.fan_out(["ghost"], layer="claude-code-cli", payload=CLAUDE_FILE,
                   on_written=written.append)
        assert written == []

    def test_a_target_of_another_engine_is_skipped_not_written(self, tmp_path):
        # Impossible by construction (a session bound to a Claude row is a
        # Claude session) — and a wrong-format write would be the worse failure.
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(layer="codex-cli", host_dir=str(tmp_path)))
        written = []
        tf.fan_out(["s1"], layer="claude-code-cli", payload=CLAUDE_FILE,
                   on_written=written.append)
        assert written == []
        assert not (tmp_path / "auth.json").exists()
        assert not (tmp_path / ".credentials.json").exists()

    def test_remote_targets_skipped_without_loop(self, monkeypatch):
        # No captured event loop (unit-test context) → remote push is skipped
        # with a log line, never raises, never calls back. An earlier test's
        # app startup leaves its closed loop here.
        monkeypatch.setattr(tf, "_loop", None)
        tf.register_session_target("s1", tf.CredentialFileTarget(
            layer="claude-code-cli", machine_id="m1", agent_name="agent",
            dir_relative="users/u/.claude",
        ))
        written = []
        tf.fan_out(["s1"], layer="claude-code-cli", payload=CLAUDE_FILE,
                   on_written=written.append)
        assert written == []

    def test_remote_push_groups_by_dir_and_acks(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        shared = tf.CredentialFileTarget(
            layer="claude-code-cli", machine_id="m1", agent_name="agent",
            dir_relative="users/u/.claude",
        )
        tf.register_session_target("s1", shared)
        tf.register_session_target("s2", shared)

        cm = MagicMock()
        cm.is_connected.return_value = True
        cm.send_command = AsyncMock()
        written = []

        async def run():
            await tf._push_remote(
                "m1", "agent", "users/u/.claude", "claude",
                {"claudeAiOauth": {"accessToken": "a"}},
                ["s1", "s2"], written.append,
            )

        with patch("core.remote.satellite_connection.get_connection_manager",
                   return_value=cm):
            asyncio.run(run())
        assert sorted(written) == ["s1", "s2"]
        msg = cm.send_command.call_args.args[1]
        # The frame a released satellite clamps on — byte for byte.
        assert msg["type"] == "credentials_update"
        assert msg["agent_slug"] == "agent"
        assert msg["dir_relative"] == "users/u/.claude"
        assert msg["kind"] == "claude"
        assert msg["content"] == {"claudeAiOauth": {"accessToken": "a"}}

    def test_remote_push_carries_the_engines_wire_kind(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        tf.register_session_target("s1", tf.CredentialFileTarget(
            layer="codex-cli", machine_id="m1", agent_name="agent",
            dir_relative="workspace/.codex",
        ))
        cm = MagicMock()
        cm.is_connected.return_value = True
        cm.send_command = AsyncMock()
        loop = asyncio.new_event_loop()
        try:
            tf._loop = loop
            with patch("core.remote.satellite_connection.get_connection_manager",
                       return_value=cm):
                tf.fan_out(["s1"], layer="codex-cli", payload=CODEX_FILE,
                           on_written=lambda sid: None)
                loop.run_until_complete(asyncio.sleep(0))
                loop.run_until_complete(asyncio.sleep(0))
        finally:
            tf._loop = None
            loop.close()
        msg = cm.send_command.call_args.args[1]
        assert msg["kind"] == "codex" and msg["dir_relative"] == "workspace/.codex"
        assert msg["content"] == CODEX_FILE

    def test_remote_push_failure_skips_callbacks(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        cm = MagicMock()
        cm.is_connected.return_value = True
        cm.send_command = AsyncMock(side_effect=RuntimeError("timeout"))
        written = []

        async def run():
            await tf._push_remote(
                "m1", "agent", "users/u/.claude", "claude",
                {"claudeAiOauth": {}}, ["s1"], written.append,
            )

        with patch("core.remote.satellite_connection.get_connection_manager",
                   return_value=cm):
            asyncio.run(run())
        assert written == []


def test_a_push_the_machine_answers_session_gone_is_not_written():
    """The satellite answers a push for a home that is gone with ``ok`` and a
    note: nothing was written, so the session's snapshot does not move."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    cm = MagicMock()
    cm.is_connected.return_value = True
    cm.send_command = AsyncMock(return_value={"type": "ack", "status": "ok", "note": "session gone, not written"})
    written: list[str] = []

    async def run():
        await tf._push_remote("m1", "agent", "users/u/.claude/own/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
                              "claude", {"claudeAiOauth": {}}, ["s1"], written.append)

    with patch("core.remote.satellite_connection.get_connection_manager", return_value=cm):
        asyncio.run(run())
    assert written == []
    cm.send_command = AsyncMock(return_value={"type": "ack", "status": "ok"})
    with patch("core.remote.satellite_connection.get_connection_manager", return_value=cm):
        asyncio.run(run())
    assert written == ["s1"]


class TestAMachineThatMissedAPush:
    """A renewal reaches a paired machine's sessions by a push. One that
    finds the machine away is owed: the machine's next sign-in pushes the
    account's current sign-in to those sessions, before the five-minute
    worker would look. One owner, the fan-out, asks the pool for it."""

    HOME = "workspace/.claude/own/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"

    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def _target(self, sid="s1", machine="m1", rel=None):
        tf.register_session_target(sid, tf.CredentialFileTarget(
            layer="claude-code-cli", machine_id=machine, agent_name="agent", dir_relative=rel or self.HOME))

    def _machine(self, *, connected=True, answer=None, fails=False):
        from unittest.mock import AsyncMock, MagicMock
        cm = MagicMock()
        cm.is_connected.return_value = connected
        cm.send_command = AsyncMock(side_effect=RuntimeError("the link dropped") if fails else None,
                                    return_value=answer or {"type": "ack", "status": "ok"})
        return cm

    async def _renewal(self, cm, written, sids=("s1",), rel=None):
        with patch("core.remote.satellite_connection.get_connection_manager", return_value=cm):
            await tf._push_remote("m1", "agent", rel or self.HOME, "claude", {"claudeAiOauth": {"accessToken": "new"}},
                                  list(sids), written.append)

    def _pool(self, monkeypatch, cm, written, bound=None):
        """The pool's side: who a session is bound to, and its push of the
        account's stored sign-in, run here as the fan-out's own push."""
        import asyncio

        from services.engines import subscription_pool as pool
        asked: list[tuple[str, list[str]]] = []
        loop = asyncio.get_running_loop()

        def _fan_out_current_token(sub_id, only=None):
            asked.append((sub_id, sorted(only or [])))
            for sid in only or []:
                t = tf.session_target(sid)
                asyncio.run_coroutine_threadsafe(self._renewal(cm, written, (sid,), t.dir_relative), loop).result(5)
        monkeypatch.setattr(pool, "bound_attribution", lambda sid: ((bound or {}).get(sid, "sub-1"), None))
        monkeypatch.setattr(pool, "fan_out_current_token", _fan_out_current_token)
        return asked

    @pytest.mark.asyncio
    async def test_a_machine_away_at_the_renewal_gets_the_sign_in_when_it_signs_in(self, monkeypatch):
        self._target()
        written: list[str] = []
        await self._renewal(self._machine(connected=False), written)
        assert written == [] and tf._missed == {"m1": {"s1"}}
        back = self._machine()
        asked = self._pool(monkeypatch, back, written)
        await tf.machine_signed_in("m1")
        assert asked == [("sub-1", ["s1"])]
        assert written == ["s1"] and not tf._missed
        frame = back.send_command.call_args.args[1]
        assert frame["type"] == "credentials_update" and frame["dir_relative"] == self.HOME
        # Nothing is owed any more: the next sign-in asks nobody.
        await tf.machine_signed_in("m1")
        assert asked == [("sub-1", ["s1"])]

    @pytest.mark.asyncio
    async def test_a_push_that_failed_is_owed_too_and_a_second_sign_in_pays_it(self, monkeypatch):
        """The link dropped under the push, and again under the first
        sign-in's push: the session stays owed until one lands."""
        self._target()
        written: list[str] = []
        await self._renewal(self._machine(fails=True), written)
        assert tf._missed == {"m1": {"s1"}}
        asked = self._pool(monkeypatch, self._machine(fails=True), written)
        await tf.machine_signed_in("m1")
        assert written == [] and tf._missed == {"m1": {"s1"}} and len(asked) == 1
        asked = self._pool(monkeypatch, self._machine(), written)
        await tf.machine_signed_in("m1")
        assert written == ["s1"] and not tf._missed and asked == [("sub-1", ["s1"])]

    @pytest.mark.asyncio
    async def test_a_push_that_failed_with_the_link_up_is_paid_by_the_workers_next_round(self, monkeypatch):
        """No sign-in follows a push that failed while the machine stayed
        signed in. The worker's round names the session to the pool's call,
        which sends it in its one list of the round: once, and never again
        when it landed. A machine that is away is asked nothing."""
        import asyncio
        from unittest.mock import AsyncMock

        from services.engines import subscription_pool as pool
        self._target()
        written: list[str] = []
        await self._renewal(self._machine(fails=True), written)
        assert tf._missed == {"m1": {"s1"}}
        up = self._machine()
        rounds: list[frozenset[str]] = []
        loop = asyncio.get_running_loop()

        def _fresh(sub_id, runway, owed=frozenset()):
            rounds.append(frozenset(owed))
            for sid in sorted(owed):
                asyncio.run_coroutine_threadsafe(self._renewal(up, written, (sid,)), loop).result(5)
            return True
        monkeypatch.setattr("services.engines.subscription_windows.poll_due", AsyncMock(return_value=0))
        monkeypatch.setattr(pool, "rebind_delisted_sessions", lambda: 0)
        monkeypatch.setattr(pool, "rebalance_scopes", lambda: None)
        monkeypatch.setattr(pool, "bound_oauth_subscription_ids", lambda: {"sub-1"})
        monkeypatch.setattr(pool, "within_boot_grace", lambda: True)
        monkeypatch.setattr(pool, "ensure_fresh_and_fan_out", _fresh)
        with patch("core.remote.satellite_connection.get_connection_manager",
                   return_value=self._machine(connected=False)):
            await tf._tick()
        assert rounds == [frozenset()] and written == [] and tf._missed == {"m1": {"s1"}}
        with patch("core.remote.satellite_connection.get_connection_manager", return_value=up):
            await tf._tick()
            assert rounds[-1] == {"s1"} and written == ["s1"] and not tf._missed
            await tf._tick()
        assert rounds[-1] == frozenset() and written == ["s1"] and up.send_command.call_count == 1

    @pytest.mark.asyncio
    async def test_a_session_that_ended_meanwhile_gets_nothing(self, monkeypatch):
        self._target("s1")
        self._target("s2", rel="workspace/.claude/own/11111111-2222-3333-4444-555555555555")
        written: list[str] = []
        away = self._machine(connected=False)
        await self._renewal(away, written, ("s1",))
        await self._renewal(away, written, ("s2",), "workspace/.claude/own/11111111-2222-3333-4444-555555555555")
        assert tf._missed == {"m1": {"s1", "s2"}}
        tf.unregister_session_target("s1")
        assert tf._missed == {"m1": {"s2"}}
        back = self._machine()
        asked = self._pool(monkeypatch, back, written)
        await tf.machine_signed_in("m1")
        assert asked == [("sub-1", ["s2"])] and written == ["s2"]
        assert back.send_command.call_count == 1
        # The last one ends too: the machine's entry goes with it.
        self._target("s3")
        await self._renewal(away, written, ("s3",))
        tf.unregister_session_target("s3")
        assert not tf._missed
        await tf.machine_signed_in("m1")
        assert len(asked) == 1

    @pytest.mark.asyncio
    async def test_a_session_gone_on_the_machine_or_unbound_is_owed_nothing(self, monkeypatch):
        """The machine answers "session gone, not written": nothing is owed.
        A session bound to no account at the sign-in is asked of nobody."""
        self._target()
        written: list[str] = []
        await self._renewal(self._machine(connected=False), written)
        await self._renewal(self._machine(answer={"type": "ack", "status": "ok",
                                                  "note": "session gone, not written"}), written)
        assert not tf._missed and written == []
        await self._renewal(self._machine(connected=False), written)
        asked = self._pool(monkeypatch, self._machine(), written, bound={"s1": None})
        await tf.machine_signed_in("m1")
        assert asked == [] and written == [] and not tf._missed

    @pytest.mark.asyncio
    async def test_a_later_push_that_lands_settles_the_debt(self, monkeypatch):
        """The five-minute worker reached the session first: the sign-in
        after it asks nobody."""
        self._target()
        written: list[str] = []
        await self._renewal(self._machine(connected=False), written)
        await self._renewal(self._machine(), written)
        assert written == ["s1"] and not tf._missed
        asked = self._pool(monkeypatch, self._machine(), written)
        await tf.machine_signed_in("m1")
        assert asked == []

    @pytest.mark.asyncio
    async def test_a_session_that_moved_to_another_machine_is_not_pushed_for_the_old_one(self, monkeypatch):
        self._target()
        written: list[str] = []
        await self._renewal(self._machine(connected=False), written)
        self._target(machine="m2")
        asked = self._pool(monkeypatch, self._machine(), written)
        await tf.machine_signed_in("m1")
        assert asked == [] and written == []

    @pytest.mark.asyncio
    async def test_a_store_that_cannot_be_read_at_the_sign_in_keeps_the_debt(self, monkeypatch):
        import psycopg

        from services.engines import subscription_pool as pool
        self._target()
        written: list[str] = []
        await self._renewal(self._machine(connected=False), written)

        def _down(sub_id, only=None):
            raise psycopg.OperationalError("the database is away")
        monkeypatch.setattr(pool, "bound_attribution", lambda sid: ("sub-1", None))
        monkeypatch.setattr(pool, "fan_out_current_token", _down)
        await tf.machine_signed_in("m1")
        assert tf._missed == {"m1": {"s1"}}

    @pytest.mark.asyncio
    async def test_each_account_is_asked_for_its_own_sessions(self, monkeypatch):
        other = "workspace/.claude/own/11111111-2222-3333-4444-555555555555"
        self._target("s1")
        self._target("s2", rel=other)
        written: list[str] = []
        away = self._machine(connected=False)
        await self._renewal(away, written, ("s1",))
        await self._renewal(away, written, ("s2",), other)
        asked = self._pool(monkeypatch, self._machine(), written, bound={"s1": "sub-1", "s2": "sub-2"})
        await tf.machine_signed_in("m1")
        assert sorted(asked) == [("sub-1", ["s1"]), ("sub-2", ["s2"])] and sorted(written) == ["s1", "s2"]

    @pytest.mark.asyncio
    async def test_the_catch_up_asks_the_pools_own_maps_and_never_the_store(self, monkeypatch):
        """Who a session is bound to is read from the maps the pool holds in
        memory. A session they do not hold is asked of nobody: the pool
        pushes only to sessions those maps hold, so the store's answer never
        led to a push and only cost a read on the loop."""
        from services.engines import pool_bindings, subscription_pool as pool
        self._target()
        written: list[str] = []
        away = self._machine(connected=False)
        await self._renewal(away, written)
        reads: list[str] = []
        asked: list[str] = []
        monkeypatch.setattr(pool_bindings.subscription_store, "get_session_binding",
                            lambda sid: reads.append(sid) or {"subscription_id": "sub-1"})
        monkeypatch.setattr(pool, "fan_out_current_token", lambda sub_id, only=None: asked.append(sub_id))
        await tf.machine_signed_in("m1")
        assert reads == [] and asked == []
        await self._renewal(away, written)
        monkeypatch.setitem(pool._session_subscriptions, "s1", "sub-1")
        await tf.machine_signed_in("m1")
        assert reads == [] and asked == ["sub-1"]

    @pytest.mark.asyncio
    async def test_a_push_whose_send_fails_after_the_sessions_end_owes_nothing(self):
        """The note keeps a session only while it is registered. This is the
        rule that bounds it: a push that fails after its session ended
        leaves nothing behind for a sign-in that may never come."""
        from unittest.mock import AsyncMock, MagicMock
        self._target()
        cm = MagicMock()
        cm.is_connected.return_value = True

        async def _send(machine_id, frame, timeout=15.0):
            tf.unregister_session_target("s1")
            raise RuntimeError("the link dropped")
        cm.send_command = AsyncMock(side_effect=_send)
        written: list[str] = []
        await self._renewal(cm, written)
        assert written == [] and tf._missed == {}

    @pytest.mark.asyncio
    async def test_one_accounts_failed_read_keeps_it_owed_and_the_next_account_is_still_asked(self, monkeypatch):
        """Two accounts have sessions on one machine. The first account's
        stored sign-in cannot be read at the sign-in: its session stays
        owed, and the second account's session gets its sign-in all the
        same."""
        import psycopg

        from services.engines import subscription_pool as pool
        other = "workspace/.claude/own/11111111-2222-3333-4444-555555555555"
        self._target("s1")
        self._target("s2", rel=other)
        written: list[str] = []
        away = self._machine(connected=False)
        await self._renewal(away, written, ("s1",))
        await self._renewal(away, written, ("s2",), other)
        self._pool(monkeypatch, self._machine(), written, bound={"s1": "sub-1", "s2": "sub-2"})
        works = pool.fan_out_current_token
        seen: list[str] = []

        def _the_first_cannot_be_read(sub_id, only=None):
            seen.append(sub_id)
            if len(seen) == 1:
                raise psycopg.OperationalError("the database is away")
            works(sub_id, only)
        monkeypatch.setattr(pool, "fan_out_current_token", _the_first_cannot_be_read)
        await tf.machine_signed_in("m1")
        assert sorted(seen) == ["sub-1", "sub-2"]
        owed, paid = ("s1", "s2") if seen[0] == "sub-1" else ("s2", "s1")
        assert tf._missed == {"m1": {owed}} and written == [paid]

    @pytest.mark.asyncio
    async def test_a_compute_machine_on_the_platforms_disk_is_written_at_once_and_owed_nothing(
            self, tmp_path, monkeypatch):
        """Its sessions' files are the platform's own: a renewal writes them
        with no push, so its sign-in finds nothing owed and asks nobody."""
        tf.register_session_target("s1", tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(tmp_path)))
        written: list[str] = []
        tf.fan_out(["s1"], layer="claude-code-cli", payload=CLAUDE_FILE, on_written=written.append)
        assert written == ["s1"] and json.loads((tmp_path / ".credentials.json").read_text()) == CLAUDE_FILE
        assert not tf._missed
        asked = self._pool(monkeypatch, self._machine(), written)
        await tf.machine_signed_in("compute-beside")
        assert asked == []


class TestExpectedSubGuard:
    """``expected_sub_id``: a stale rotation must not clobber the credential
    file of a session that a selection-change rebind just moved elsewhere."""

    def _reset(self):
        from services.engines import subscription_pool as pool
        _clean()
        pool._session_subscriptions.clear()

    def setup_method(self):
        self._reset()

    def teardown_method(self):
        self._reset()

    def test_drops_sessions_bound_elsewhere(self, tmp_path):
        from services.engines import subscription_pool as pool
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(tmp_path)))
        with pool._session_maps_lock:
            pool._session_subscriptions["s1"] = "new-sub"  # already re-homed
        written = []
        tf.fan_out(["s1"], layer="claude-code-cli", payload=CLAUDE_FILE,
                   on_written=written.append, expected_sub_id="old-sub")
        assert written == []
        assert not (tmp_path / ".credentials.json").exists()

    def test_passes_sessions_still_bound(self, tmp_path):
        from services.engines import subscription_pool as pool
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(tmp_path)))
        with pool._session_maps_lock:
            pool._session_subscriptions["s1"] = "old-sub"
        written = []
        tf.fan_out(["s1"], layer="claude-code-cli", payload=CLAUDE_FILE,
                   on_written=written.append, expected_sub_id="old-sub")
        assert written == ["s1"]
        assert json.loads((tmp_path / ".credentials.json").read_text()) == CLAUDE_FILE

    def test_no_guard_keeps_legacy_behavior(self, tmp_path):
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(layer="claude-code-cli", host_dir=str(tmp_path)))
        written = []
        tf.fan_out(["s1"], layer="claude-code-cli", payload=CLAUDE_FILE,
                   on_written=written.append)
        assert written == ["s1"]


class TestWorkerTick:
    def test_tick_rebinds_before_freshening(self):
        """The tick's rebind pass runs FIRST so the freshness pass keeps the
        account each session will actually keep using — and retries rebinds
        whose write couldn't land."""
        import asyncio
        from unittest.mock import patch

        from services.engines import subscription_pool as pool

        calls = []

        async def _poll(**kw):
            calls.append("poll")
            return 0

        with patch("services.engines.subscription_windows.poll_due", _poll), \
             patch.object(pool, "rebind_delisted_sessions",
                          side_effect=lambda **kw: calls.append("rebind") or 0), \
             patch.object(pool, "bound_oauth_subscription_ids",
                          side_effect=lambda: calls.append("list") or {"x"}), \
             patch.object(pool, "ensure_fresh_and_fan_out",
                          side_effect=lambda *a, **k: calls.append("fresh") or True):
            asyncio.run(tf._tick())
        # The provider-window poll leads so the rebind and rebalance passes
        # converge on a fresh reading in the same tick.
        assert calls == ["poll", "rebind", "list", "fresh"]

    def test_tick_survives_rebind_failure(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from services.engines import subscription_pool as pool

        with patch("services.engines.subscription_windows.poll_due",
                   AsyncMock(side_effect=RuntimeError("poll boom"))), \
             patch.object(pool, "rebind_delisted_sessions",
                          side_effect=RuntimeError("boom")), \
             patch.object(pool, "bound_oauth_subscription_ids", return_value=set()):
            asyncio.run(tf._tick())  # must not raise

    def _run_unbound_tick(self, *, boot_grace, rows, persisted, bound=frozenset()):
        import asyncio
        from unittest.mock import patch

        from services.engines import subscription_pool as pool

        freshened = []
        from unittest.mock import AsyncMock
        with patch("services.engines.subscription_windows.poll_due",
                   AsyncMock(return_value=0)), \
             patch.object(pool, "rebind_delisted_sessions", return_value=0), \
             patch.object(pool, "rebalance_scopes", return_value=None), \
             patch.object(pool, "bound_oauth_subscription_ids",
                          return_value=set(bound)), \
             patch.object(pool, "within_boot_grace", return_value=boot_grace), \
             patch.object(pool, "ensure_fresh_and_fan_out",
                          side_effect=lambda sid, *a, **k: freshened.append(sid) or True), \
             patch("storage.billing.subscription_store.list_persisted_binding_sub_ids",
                   return_value=set(persisted)), \
             patch("storage.billing.subscription_store.list_subscriptions",
                   return_value=rows):
            asyncio.run(tf._tick())
        return freshened

    def test_unbound_active_oauth_rows_get_freshened(self):
        """An idle account must not discover a dead grant at the owner's next
        chat — the tick freshens unbound ACTIVE OAuth rows too."""
        rows = [
            {"id": "idle", "auth_type": "oauth", "status": "active"},
            {"id": "apikey", "auth_type": "api_key", "status": "active"},
            {"id": "dead", "auth_type": "oauth", "status": "expired"},
            {"id": "held", "auth_type": "oauth", "status": "active"},
            {"id": "livebound", "auth_type": "oauth", "status": "active"},
        ]
        freshened = self._run_unbound_tick(
            boot_grace=False, rows=rows, persisted={"held"}, bound={"livebound"},
        )
        # 'livebound' via the bound loop; the unbound pass adds ONLY 'idle' —
        # persisted-binding rows count as bound (their session may be live but
        # unannounced), non-oauth and non-active rows are skipped.
        assert freshened == ["livebound", "idle"]

    def test_unbound_pass_stands_down_during_boot_grace(self):
        rows = [{"id": "idle", "auth_type": "oauth", "status": "active"}]
        freshened = self._run_unbound_tick(
            boot_grace=True, rows=rows, persisted=set(),
        )
        assert freshened == []
