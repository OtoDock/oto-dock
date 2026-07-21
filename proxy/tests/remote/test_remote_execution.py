"""Regression tests for core/remote/remote_execution.py."""

from __future__ import annotations


def test_load_hook_scripts_loads_all_hooks():
    """Regression: after the core/ -> core/remote/ subpackage move,
    ``_load_hook_scripts`` computed ``hooks_dir`` from a stale ``__file__``
    parent-count (and its ``app_config.PROXY_DIR`` primary branch never fired —
    config only exposes ``HOOKS_DIR``), so it resolved to ``proxy/core/hooks``
    (absent) and returned an EMPTY dict. Remote/satellite sessions therefore ran
    with NO permission_gate / tool_result_forwarder / subagent_tracker hooks.

    Pin that all three hook scripts load from the real proxy/hooks dir with
    non-empty content.
    """
    from core.remote import remote_execution as re

    re._HOOK_SCRIPTS_CACHE = None  # bypass the module-level cache
    try:
        scripts = re._load_hook_scripts()
    finally:
        re._HOOK_SCRIPTS_CACHE = None  # don't leak forced state to other tests

    for name in ("permission_gate.py", "tool_result_forwarder.py",
                 "subagent_tracker.py"):
        assert name in scripts, f"hook script not loaded: {name}"
        assert scripts[name].strip(), f"hook script empty: {name}"


class TestRestoreAdoptedCredentials:
    """Adopt-time counterpart of _bind_subscription: the in-memory binding,
    fan-out target and catch-up fan-out are restored from persisted state."""

    def _ctx(self, mount_username):
        class _C:
            pass
        c = _C()
        c.mount_username = mount_username
        return c

    def test_registers_target_and_repairs_rotation(self, monkeypatch):
        from core.remote.remote_execution import RemoteExecutionLayer
        from core.session import session_state
        from services.engines import subscription_pool, token_fanout
        from storage import subscription_store

        calls = {}

        def _restore(sid):
            calls["restored"] = sid
            return "sub-9"

        monkeypatch.setattr(subscription_pool, "restore_session_binding", _restore)
        monkeypatch.setattr(subscription_store, "get_credential_data",
                            lambda sub_id: {"oauth_token": {"accessToken": "at"}})
        monkeypatch.setattr(session_state, "get_session_security",
                            lambda sid: self._ctx("alice"))
        monkeypatch.setattr(token_fanout, "register_session_target",
                            lambda sid, target: calls.__setitem__("target", target))
        monkeypatch.setattr(subscription_pool, "fan_out_current_token",
                            lambda sub_id: calls.__setitem__("fanned", sub_id))

        RemoteExecutionLayer._restore_adopted_credentials("sess-a", "mach-1", "pa")
        t = calls["target"]
        assert t.kind == "claude"
        assert t.machine_id == "mach-1"
        assert t.agent_name == "pa"
        assert t.dir_relative == "users/alice/.claude"
        assert calls["fanned"] == "sub-9"

    def test_agent_scope_uses_workspace_dir(self, monkeypatch):
        from core.remote.remote_execution import RemoteExecutionLayer
        from core.session import session_state
        from services.engines import subscription_pool, token_fanout
        from storage import subscription_store

        calls = {}
        monkeypatch.setattr(subscription_pool, "restore_session_binding",
                            lambda sid: "sub-1")
        monkeypatch.setattr(subscription_store, "get_credential_data",
                            lambda sub_id: {"oauth_token": {"accessToken": "x"}})
        monkeypatch.setattr(session_state, "get_session_security", lambda sid: None)
        monkeypatch.setattr(token_fanout, "register_session_target",
                            lambda sid, target: calls.__setitem__("target", target))
        monkeypatch.setattr(subscription_pool, "fan_out_current_token",
                            lambda sub_id: None)

        RemoteExecutionLayer._restore_adopted_credentials("sess-b", "m", "pa")
        assert calls["target"].dir_relative == "workspace/.claude"

    def test_api_key_subscription_skips_registration(self, monkeypatch):
        from core.remote.remote_execution import RemoteExecutionLayer
        from services.engines import subscription_pool, token_fanout
        from storage import subscription_store

        calls = {}
        monkeypatch.setattr(subscription_pool, "restore_session_binding",
                            lambda sid: "sub-2")
        monkeypatch.setattr(subscription_store, "get_credential_data",
                            lambda sub_id: {"api_key": "k"})
        monkeypatch.setattr(token_fanout, "register_session_target",
                            lambda sid, target: calls.__setitem__("target", target))

        RemoteExecutionLayer._restore_adopted_credentials("sess-c", "m", "pa")
        assert "target" not in calls

    def test_no_binding_is_quiet_noop(self, monkeypatch):
        from core.remote.remote_execution import RemoteExecutionLayer
        from services.engines import subscription_pool

        monkeypatch.setattr(subscription_pool, "restore_session_binding",
                            lambda sid: None)
        RemoteExecutionLayer._restore_adopted_credentials("sess-d", "m", "pa")
