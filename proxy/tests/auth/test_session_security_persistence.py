"""SecurityContext persistence + fail-closed permission gate.

The per-session ``SecurityContext`` is persisted to ``sessions/security_index.json``
so a session that SURVIVES a proxy crash on a satellite (and its background
sub-agents) keeps full path-policy enforcement after a restart. The permission
gate now DENIES a context-less session (a dead session, or a closed session whose
self-contained 24h JWT was replayed) instead of failing OPEN. The context carries
no secrets, so persisting it is not a credential leak.

Pure ``session_state`` + gate logic — no live daemon, no satellite.
"""

import asyncio
import json
import sys
import time

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth.path_policy import SecurityContext  # noqa: E402
from core.session import session_state # noqa: E402


def _ctx(**over):
    base = dict(
        role="manager", username="alice", agent="demo", is_admin_agent=False,
        display_name="Alice A.", email="alice@example.com",
        target_kind="user_remote", target_label="Alice MBP",
        target_agents_dir="/home/alice/.oto-dock/agents",
        target_machine_id="mach-1", target_home_dir="/home/alice",
        target_allow_full_fs=False, target_os_user="alice",
        target_user_dirs={"downloads": "/home/alice/Downloads"},
        target_device_grants={"computer", "browser"},
    )
    base.update(over)
    return SecurityContext(**base)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate the persistence file + the in-memory dicts for each test."""
    monkeypatch.setattr(session_state, "_SECURITY_INDEX", tmp_path / "security_index.json")
    monkeypatch.setattr(session_state, "_session_security", {})
    monkeypatch.setattr(session_state, "_session_security_ts", {})
    return tmp_path


# ── serialize / deserialize ────────────────────────────────────────────────

def test_serialize_is_json_safe_and_roundtrips(iso):
    ctx = _ctx()
    d = session_state._serialize_security_ctx(ctx)
    # the set field must become a JSON-safe list; the dict field survives
    assert isinstance(d["target_device_grants"], list)
    assert sorted(d["target_device_grants"]) == ["browser", "computer"]
    json.dumps(d)  # must not raise
    assert session_state._deserialize_security_ctx(d) == ctx  # set restored


def test_deserialize_tolerates_field_drift(iso):
    d = session_state._serialize_security_ctx(_ctx())
    d["some_future_field"] = "ignored"  # unknown key dropped
    del d["email"]                       # missing key → default ""
    back = session_state._deserialize_security_ctx(d)
    assert back.email == ""
    assert back.username == "alice"


# ── persist on warmup → reload on restart ──────────────────────────────────

def test_set_persists_and_load_repopulates(iso):
    ctx = _ctx()
    session_state.set_session_security("sess-1", ctx)
    assert session_state._SECURITY_INDEX.exists()
    # simulate a restart: drop memory, reload from disk
    session_state._session_security.clear()
    session_state._session_security_ts.clear()
    session_state.load_session_security()
    assert session_state.get_session_security("sess-1") == ctx


def test_close_deletes_from_disk(iso):
    session_state.set_session_security("sess-1", _ctx())
    session_state.cleanup_session_permission_state("sess-1")
    assert session_state.get_session_security("sess-1") is None
    # a replayed JWT after close finds nothing even across a restart
    session_state.load_session_security()
    assert session_state.get_session_security("sess-1") is None


def test_load_prunes_entries_older_than_jwt_ttl(iso):
    session_state.set_session_security("fresh", _ctx())
    session_state.set_session_security("stale", _ctx(username="bob"))
    raw = json.loads(session_state._SECURITY_INDEX.read_text())
    raw["stale"]["_saved_at"] = time.time() - (session_state._SECURITY_TTL_S + 60)
    session_state._SECURITY_INDEX.write_text(json.dumps(raw))
    session_state._session_security.clear()
    session_state._session_security_ts.clear()
    session_state.load_session_security()
    assert session_state.get_session_security("fresh") is not None
    assert session_state.get_session_security("stale") is None
    assert "stale" not in json.loads(session_state._SECURITY_INDEX.read_text())


def test_refresh_allow_full_fs_repersists(iso):
    session_state.set_session_security("sess-1", _ctx(target_allow_full_fs=False))
    assert session_state.refresh_target_allow_full_fs("mach-1", True) == 1
    session_state._session_security.clear()
    session_state._session_security_ts.clear()
    session_state.load_session_security()
    assert session_state.get_session_security("sess-1").target_allow_full_fs is True


def test_refresh_device_grants_repersists(iso):
    session_state.set_session_security("sess-1", _ctx(target_device_grants={"computer"}))
    assert session_state.refresh_target_device_grants("mach-1", {"computer", "app"}) == 1
    session_state._session_security.clear()
    session_state._session_security_ts.clear()
    session_state.load_session_security()
    assert session_state.get_session_security("sess-1").target_device_grants == {"computer", "app"}


def test_corrupt_index_does_not_crash_load(iso):
    session_state._SECURITY_INDEX.write_text("{not valid json")
    session_state.load_session_security()  # must not raise
    assert session_state._session_security == {}


# ── fail-closed permission gate ────────────────────────────────────────────

def test_gate_denies_when_no_security_context(iso):
    """No context = dead/replayed session → deny (was fail-open skip → allow)."""
    from api.hooks.hooks import decide_tool_permission
    out = asyncio.run(decide_tool_permission("ghost-sess", "Read", {"file_path": "/etc/passwd"}))
    assert out["decision"] == "deny"


def test_gate_proceeds_when_context_present(iso, monkeypatch):
    """A live (admin/local) context passes Pass-1; dontAsk auto-approves in Pass-2."""
    from api.hooks import hooks
    session_state._sessions["live-sess"] = {"client_type": "dashboard"}
    session_state._session_security["live-sess"] = _ctx(
        role="admin", is_admin_agent=True, target_kind="local",
        target_machine_id="", target_label="", target_agents_dir="",
        target_home_dir="", target_os_user="", target_user_dirs={},
        target_device_grants=set(),
    )
    session_state.set_session_mode("live-sess", "dontAsk")
    try:
        out = asyncio.run(
            hooks.decide_tool_permission("live-sess", "mcp__demo__do_thing", {})
        )
        assert out["decision"] == "allow"
    finally:
        session_state._sessions.pop("live-sess", None)
        session_state._session_modes.pop("live-sess", None)
