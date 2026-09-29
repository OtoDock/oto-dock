"""Rotation fan-out mechanics (``services/engines/token_fanout``).

Covers the target registry, the one credential-file writer (the file is
named by the ENGINE's declaration, ``LayerCapabilities.auth.credential_file``),
per-directory dedupe across a scope's sessions, and the remote-push grouping
— the parts the pool's rotation chokepoint builds on. The payload a fan-out
delivers is the engine's full file content; this module never shapes it.
"""

import json
import stat

import pytest

from services.engines import token_fanout as tf

CLAUDE_FILE = {"claudeAiOauth": {"accessToken": "at", "refreshToken": "", "expiresAt": 5,
                                 "scopes": [], "subscriptionType": "", "rateLimitTier": ""}}
CODEX_FILE = {"auth_mode": "chatgpt", "tokens": {"access_token": "t", "refresh_token": ""}}


def _clean():
    tf._targets.clear()


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

    def test_remote_targets_skipped_without_loop(self):
        # No captured event loop (unit-test context) → remote push is skipped
        # with a log line, never raises, never calls back.
        tf.register_session_target("s1", tf.CredentialFileTarget(
            layer="claude-code-cli", machine_id="m1", agent_name="agent",
            dir_relative="users/u/.claude",
        ))
        written = []
        assert tf._loop is None
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
