"""Rotation fan-out mechanics (``services/engines/token_fanout``).

Covers the target registry, the two credential-file writers (the single
source for each file's on-disk shape), per-directory dedupe across a scope's
sessions, and the remote-push grouping — the parts the pool's rotation
chokepoint builds on.
"""

import json
import stat

from services.engines import token_fanout as tf


def _clean():
    tf._targets.clear()


class TestWriters:
    def test_claude_credentials_file_shape_and_mode(self, tmp_path):
        blob = {"accessToken": "at", "refreshToken": "", "expiresAt": 5,
                "scopes": [], "subscriptionType": "", "rateLimitTier": ""}
        tf.write_claude_credentials_file(tmp_path, blob)
        path = tmp_path / ".credentials.json"
        assert json.loads(path.read_text()) == {"claudeAiOauth": blob}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_codex_auth_file_shape_and_mode(self, tmp_path):
        auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "t",
                                                   "refresh_token": ""}}
        tf.write_codex_auth_file(tmp_path, auth)
        path = tmp_path / "auth.json"
        assert json.loads(path.read_text()) == auth
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_writers_create_missing_dirs(self, tmp_path):
        target = tmp_path / "users" / "alice" / ".claude"
        tf.write_claude_credentials_file(target, {"accessToken": "a"})
        assert (target / ".credentials.json").exists()


class TestRegistry:
    def setup_method(self):
        _clean()

    def test_register_and_unregister(self):
        t = tf.CredentialFileTarget(kind="claude", host_dir="/x")
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
        shared = tf.CredentialFileTarget(kind="claude", host_dir=str(tmp_path))
        tf.register_session_target("s1", shared)
        tf.register_session_target("s2", shared)
        written = []
        tf.fan_out(["s1", "s2"], claude_blob={"accessToken": "new"},
                   codex_auth=None, on_written=written.append)
        assert sorted(written) == ["s1", "s2"]
        blob = json.loads((tmp_path / ".credentials.json").read_text())
        assert blob["claudeAiOauth"]["accessToken"] == "new"

    def test_kind_selects_the_right_file(self, tmp_path):
        claude_dir = tmp_path / "c"
        codex_dir = tmp_path / "x"
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(kind="claude", host_dir=str(claude_dir)))
        tf.register_session_target(
            "s2", tf.CredentialFileTarget(kind="codex", host_dir=str(codex_dir)))
        written = []
        tf.fan_out(["s1", "s2"], claude_blob={"accessToken": "a"},
                   codex_auth={"tokens": {"access_token": "b"}},
                   on_written=written.append)
        assert (claude_dir / ".credentials.json").exists()
        assert (codex_dir / "auth.json").exists()
        assert sorted(written) == ["s1", "s2"]

    def test_unregistered_sessions_are_skipped(self, tmp_path):
        written = []
        tf.fan_out(["ghost"], claude_blob={"accessToken": "a"},
                   codex_auth=None, on_written=written.append)
        assert written == []

    def test_missing_blob_for_kind_skips_without_callback(self, tmp_path):
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(kind="codex", host_dir=str(tmp_path)))
        written = []
        tf.fan_out(["s1"], claude_blob={"accessToken": "a"}, codex_auth=None,
                   on_written=written.append)
        assert written == []
        assert not (tmp_path / "auth.json").exists()

    def test_remote_targets_skipped_without_loop(self):
        # No captured event loop (unit-test context) → remote push is skipped
        # with a log line, never raises, never calls back.
        tf.register_session_target("s1", tf.CredentialFileTarget(
            kind="claude", machine_id="m1", agent_name="agent",
            dir_relative="users/u/.claude",
        ))
        written = []
        assert tf._loop is None
        tf.fan_out(["s1"], claude_blob={"accessToken": "a"}, codex_auth=None,
                   on_written=written.append)
        assert written == []

    def test_remote_push_groups_by_dir_and_acks(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        shared = tf.CredentialFileTarget(
            kind="claude", machine_id="m1", agent_name="agent",
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
        assert msg["type"] == "credentials_update"
        assert msg["agent_slug"] == "agent"
        assert msg["dir_relative"] == "users/u/.claude"
        assert msg["kind"] == "claude"
        assert msg["content"] == {"claudeAiOauth": {"accessToken": "a"}}

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
            "s1", tf.CredentialFileTarget(kind="claude", host_dir=str(tmp_path)))
        with pool._session_maps_lock:
            pool._session_subscriptions["s1"] = "new-sub"  # already re-homed
        written = []
        tf.fan_out(["s1"], claude_blob={"accessToken": "stale"}, codex_auth=None,
                   on_written=written.append, expected_sub_id="old-sub")
        assert written == []
        assert not (tmp_path / ".credentials.json").exists()

    def test_passes_sessions_still_bound(self, tmp_path):
        from services.engines import subscription_pool as pool
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(kind="claude", host_dir=str(tmp_path)))
        with pool._session_maps_lock:
            pool._session_subscriptions["s1"] = "old-sub"
        written = []
        tf.fan_out(["s1"], claude_blob={"accessToken": "fresh"}, codex_auth=None,
                   on_written=written.append, expected_sub_id="old-sub")
        assert written == ["s1"]
        blob = json.loads((tmp_path / ".credentials.json").read_text())
        assert blob["claudeAiOauth"]["accessToken"] == "fresh"

    def test_no_guard_keeps_legacy_behavior(self, tmp_path):
        tf.register_session_target(
            "s1", tf.CredentialFileTarget(kind="claude", host_dir=str(tmp_path)))
        written = []
        tf.fan_out(["s1"], claude_blob={"accessToken": "x"}, codex_auth=None,
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
        with patch.object(pool, "rebind_delisted_sessions",
                          side_effect=lambda **kw: calls.append("rebind") or 0), \
             patch.object(pool, "bound_oauth_subscription_ids",
                          side_effect=lambda: calls.append("list") or {"x"}), \
             patch.object(pool, "ensure_fresh_and_fan_out",
                          side_effect=lambda *a, **k: calls.append("fresh") or True):
            asyncio.run(tf._tick())
        assert calls == ["rebind", "list", "fresh"]

    def test_tick_survives_rebind_failure(self):
        import asyncio
        from unittest.mock import patch
        from services.engines import subscription_pool as pool

        with patch.object(pool, "rebind_delisted_sessions",
                          side_effect=RuntimeError("boom")), \
             patch.object(pool, "bound_oauth_subscription_ids", return_value=set()):
            asyncio.run(tf._tick())  # must not raise

    def _run_unbound_tick(self, *, boot_grace, rows, persisted, bound=frozenset()):
        import asyncio
        from unittest.mock import patch
        from services.engines import subscription_pool as pool

        freshened = []
        with patch.object(pool, "rebind_delisted_sessions", return_value=0), \
             patch.object(pool, "rebalance_scopes", return_value=None), \
             patch.object(pool, "bound_oauth_subscription_ids",
                          return_value=set(bound)), \
             patch.object(pool, "within_boot_grace", return_value=boot_grace), \
             patch.object(pool, "ensure_fresh_and_fan_out",
                          side_effect=lambda sid, *a, **k: freshened.append(sid) or True), \
             patch("storage.subscription_store.list_persisted_binding_sub_ids",
                   return_value=set(persisted)), \
             patch("storage.subscription_store.list_subscriptions",
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
