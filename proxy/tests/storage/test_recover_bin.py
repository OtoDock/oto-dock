"""Tests for the Workspace Recover Bin.

Covers ``storage/recover_bin_store.py`` (capture scope/owner resolution, size
cap, ``list_for`` per-user/manager/admin scoping with NO cross-user leak,
``delete_expired`` removing rows AND on-disk bytes) and the ``api/agents/agents.py``
recover-bin endpoints (restore → original path; collision → "(recovered)"
suffix that NEVER overrides; satellite re-sync fan-out; server-side scope
re-check denial; entry consumed only on a successful restore).

Run individually (the conftest DB pool exhausts if test files run together):
    proxy/venv/bin/python -m pytest tests/storage/test_recover_bin.py
"""

import pytest

import config
from storage import recover_bin_store as rb

AGENT = "test-agent"


@pytest.fixture(autouse=True)
def _recover_bin_dir(tmp_path, monkeypatch):
    """Redirect the on-disk recover-bin root to a per-test temp dir so tests
    never touch the real platform data dir."""
    monkeypatch.setattr(config, "RECOVER_BIN_DIR", tmp_path / "recover-bin")
    return tmp_path


def _bytes_path(agent, entry_id):
    return config.RECOVER_BIN_DIR / agent / entry_id


# ---------------------------------------------------------------------------
# capture(): scope + owner resolution
# ---------------------------------------------------------------------------

def test_capture_user_scope_resolves_owner(monkeypatch):
    monkeypatch.setattr(
        "storage.database.get_user_sub_by_username",
        lambda slug: "user-viewer" if slug == "alice" else None,
    )
    entry = rb.capture(AGENT, "users/alice/notes.txt", b"hello", "deleted")
    assert entry is not None
    assert entry["scope"] == "user"
    assert entry["owner_sub"] == "user-viewer"
    assert entry["original_name"] == "notes.txt"
    assert entry["reason"] == "deleted"
    assert entry["size"] == 5
    # Bytes captured on disk.
    assert _bytes_path(AGENT, entry["entry_id"]).read_bytes() == b"hello"


def test_capture_shared_scope_for_workspace():
    entry = rb.capture(AGENT, "workspace/sub/doc.txt", b"x", "deleted")
    assert entry is not None
    assert entry["scope"] == "shared"
    assert entry["owner_sub"] == ""


def test_capture_unknown_user_falls_back_to_shared(monkeypatch):
    # A users/<slug>/ whose slug no longer maps to a user is never orphaned —
    # it becomes shared (manager/admin recoverable) rather than lost.
    monkeypatch.setattr(
        "storage.database.get_user_sub_by_username", lambda slug: None,
    )
    entry = rb.capture(AGENT, "users/ghost/x.txt", b"x", "conflict")
    assert entry is not None
    assert entry["scope"] == "shared"
    assert entry["owner_sub"] == ""


def test_capture_invalid_reason_raises():
    with pytest.raises(ValueError):
        rb.capture(AGENT, "workspace/x.txt", b"x", "bogus")


# ---------------------------------------------------------------------------
# capture(): size cap + empty
# ---------------------------------------------------------------------------

def test_capture_over_cap_returns_none(monkeypatch):
    monkeypatch.setattr(config, "RECOVER_BIN_MAX_BYTES", 4)
    entry = rb.capture(AGENT, "workspace/big.bin", b"12345", "deleted")
    assert entry is None
    # No row persisted (and no bytes).
    assert rb.list_for(AGENT, "user-admin", True, True, True) == []


def test_capture_empty_returns_none():
    assert rb.capture(AGENT, "workspace/empty.txt", b"", "deleted") is None


def test_capture_at_cap_boundary_is_binned(monkeypatch):
    monkeypatch.setattr(config, "RECOVER_BIN_MAX_BYTES", 5)
    entry = rb.capture(AGENT, "workspace/exact.bin", b"12345", "deleted")
    assert entry is not None


# ---------------------------------------------------------------------------
# capture(): exclude session-state / config / secret paths; idempotency
# ---------------------------------------------------------------------------

def test_capture_excludes_session_and_secret_paths():
    # Session state + secrets are NEVER binned — they're rewritten every sync
    # and would flood the bin + leak credentials.
    assert rb.capture(AGENT, "users/dave/.claude/settings.json", b"x", "conflict") is None
    assert rb.capture(AGENT, "users/dave/.credentials/google-tokens/a.json", b"x", "conflict") is None
    assert rb.capture(AGENT, ".codex/sessions/s.jsonl", b"x", "deleted") is None
    # The agent config/ folder (prompt + context) IS recoverable — manager-
    # curated, like knowledge/. Real workspace content too, including a dotfile
    # deep in a repo (only the platform dot-DIRS are excluded, not every dotfile).
    assert rb.capture(AGENT, "config/prompt.md", b"x", "conflict") is not None
    assert rb.capture(AGENT, "knowledge/ref.md", b"x", "conflict") is not None
    assert rb.capture(AGENT, "users/dave/workspace/repo/.gitignore", b"x", "deleted") is not None
    assert rb.capture(AGENT, "workspace/doc.txt", b"x", "deleted") is not None


def test_capture_is_idempotent():
    e1 = rb.capture(AGENT, "workspace/dup.txt", b"same", "deleted")
    assert e1 is not None
    # Same path + same bytes → not re-binned (collapses the dashboard-delete +
    # reconnect-reconcile double-capture, and stops per-sync re-binning).
    assert rb.capture(AGENT, "workspace/dup.txt", b"same", "deleted") is None
    # A genuinely different version IS binned.
    assert rb.capture(AGENT, "workspace/dup.txt", b"changed", "conflict") is not None
    out = rb.list_for(AGENT, "user-admin", True, True, True)
    assert len([x for x in out if x["rel_path"] == "workspace/dup.txt"]) == 2


# ---------------------------------------------------------------------------
# list_for(): scoping — no cross-user leak
# ---------------------------------------------------------------------------

def _seed_three(monkeypatch):
    """alice (user-viewer) + bob (user-viewer2) user files + one shared file."""
    monkeypatch.setattr(
        "storage.database.get_user_sub_by_username",
        {"alice": "user-viewer", "bob": "user-viewer2"}.get,
    )
    rb.capture(AGENT, "users/alice/a.txt", b"a", "deleted")
    rb.capture(AGENT, "users/bob/b.txt", b"b", "deleted")
    rb.capture(AGENT, "workspace/shared.txt", b"s", "deleted")


def test_list_member_sees_only_own(monkeypatch):
    _seed_three(monkeypatch)
    # A viewer (no shared write) sees ONLY their own personal file.
    out = rb.list_for(AGENT, "user-viewer", can_edit=False, can_manage=False, is_admin=False)
    assert {e["rel_path"] for e in out} == {"users/alice/a.txt"}


def test_list_other_member_sees_only_own(monkeypatch):
    _seed_three(monkeypatch)
    out = rb.list_for(AGENT, "user-viewer2", can_edit=False, can_manage=False, is_admin=False)
    assert {e["rel_path"] for e in out} == {"users/bob/b.txt"}


def test_list_admin_sees_all(monkeypatch):
    _seed_three(monkeypatch)
    out = rb.list_for(AGENT, "user-admin", can_edit=True, can_manage=True, is_admin=True)
    assert {e["rel_path"] for e in out} == {
        "users/alice/a.txt", "users/bob/b.txt", "workspace/shared.txt",
    }


def _seed_tiers():
    """One file in each shared tier area: workspace, knowledge, config."""
    rb.capture(AGENT, "workspace/w.txt", b"w", "conflict")
    rb.capture(AGENT, "knowledge/k.md", b"k", "conflict")
    rb.capture(AGENT, "config/prompt.md", b"c", "conflict")


def test_list_viewer_sees_no_shared_tiers():
    _seed_tiers()
    out = rb.list_for(AGENT, "user-viewer", can_edit=False, can_manage=False, is_admin=False)
    assert out == []


def test_list_editor_sees_workspace_only():
    _seed_tiers()
    # Editor writes the shared workspace but NOT knowledge/config.
    out = rb.list_for(AGENT, "user-viewer", can_edit=True, can_manage=False, is_admin=False)
    assert {e["rel_path"] for e in out} == {"workspace/w.txt"}


def test_list_manager_sees_workspace_knowledge_config():
    _seed_tiers()
    out = rb.list_for(AGENT, "user-manager", can_edit=True, can_manage=True, is_admin=False)
    assert {e["rel_path"] for e in out} == {
        "workspace/w.txt", "knowledge/k.md", "config/prompt.md",
    }


def test_list_excludes_expired(monkeypatch):
    rb.capture(AGENT, "workspace/old.txt", b"x", "deleted", ttl_days=-1)
    rb.capture(AGENT, "workspace/new.txt", b"y", "deleted")
    out = rb.list_for(AGENT, "user-admin", can_edit=True, can_manage=True, is_admin=True)
    assert {e["rel_path"] for e in out} == {"workspace/new.txt"}


# ---------------------------------------------------------------------------
# delete() + delete_expired(): rows AND bytes
# ---------------------------------------------------------------------------

def test_delete_expired_removes_rows_and_bytes():
    expired = rb.capture(AGENT, "workspace/old.txt", b"x", "deleted", ttl_days=-1)
    alive = rb.capture(AGENT, "workspace/new.txt", b"y", "deleted")
    assert _bytes_path(AGENT, expired["entry_id"]).exists()

    removed = rb.delete_expired()
    assert removed == 1
    assert rb.get(expired["entry_id"]) is None
    assert not _bytes_path(AGENT, expired["entry_id"]).exists()
    # The non-expired entry survives (row + bytes).
    assert rb.get(alive["entry_id"]) is not None
    assert _bytes_path(AGENT, alive["entry_id"]).exists()


def test_delete_removes_row_and_bytes():
    e = rb.capture(AGENT, "workspace/x.txt", b"x", "deleted")
    p = _bytes_path(AGENT, e["entry_id"])
    assert p.exists()
    rb.delete(e["entry_id"])
    assert rb.get(e["entry_id"]) is None
    assert not p.exists()


# ---------------------------------------------------------------------------
# Restore endpoint: collision, re-sync, scope re-check, consume-on-success
# ---------------------------------------------------------------------------

def _admin():
    from auth.providers import UserContext
    return UserContext(sub="user-admin", email="a@t", name="Admin", role="admin")


def _member_with_agent():
    from auth.providers import UserContext
    return UserContext(
        sub="user-viewer", email="v@t", name="Viewer", role="member",
        agents=[AGENT], agent_roles={AGENT: "viewer"},
    )


def _editor_with_agent():
    from auth.providers import UserContext
    return UserContext(
        sub="user-viewer", email="e@t", name="Editor", role="member",
        agents=[AGENT], agent_roles={AGENT: "editor"},
    )


def _manager_with_agent():
    from auth.providers import UserContext
    return UserContext(
        sub="user-manager", email="m@t", name="Manager", role="member",
        agents=[AGENT], agent_roles={AGENT: "manager"},
    )


def _make_agent():
    from storage import agent_store
    agent_store.create_agent(AGENT, "Test Agent")
    agent_store._invalidate_cache()


@pytest.fixture
def _fanout_calls(monkeypatch):
    """Capture workspace_fanout.fan_out_write calls instead of hitting satellites."""
    calls = []

    async def _fake(agent_slug, rel_path, content, **kw):
        calls.append((agent_slug, rel_path, content))

    monkeypatch.setattr("services.remote.workspace_fanout.fan_out_write", _fake)
    return calls


@pytest.mark.asyncio
async def test_restore_no_collision_writes_original_path(_fanout_calls):
    _make_agent()
    from api.agents.agents import restore_recover_bin, RecoverRestoreRequest
    e = rb.capture(AGENT, "workspace/new.txt", b"data", "deleted")

    res = await restore_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]), _admin(),
    )
    assert [r["rel_path"] for r in res["restored"]] == ["workspace/new.txt"]
    assert res["renamed"] == []
    dest = config.get_agent_dir(AGENT) / "workspace/new.txt"
    assert dest.read_bytes() == b"data"
    assert _fanout_calls == [(AGENT, "workspace/new.txt", b"data")]
    # Entry consumed on success.
    assert rb.get(e["entry_id"]) is None


@pytest.mark.asyncio
async def test_restore_collision_uses_suffix_never_overrides(_fanout_calls):
    _make_agent()
    from api.agents.agents import restore_recover_bin, RecoverRestoreRequest
    # A current file already occupies the original path.
    dest = config.get_agent_dir(AGENT) / "workspace/doc.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"current")
    e = rb.capture(AGENT, "workspace/doc.txt", b"old", "conflict")

    res = await restore_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]), _admin(),
    )
    # Original is NEVER overridden.
    assert dest.read_bytes() == b"current"
    # Recovered alongside with the "(recovered)" suffix.
    recovered = config.get_agent_dir(AGENT) / "workspace/doc (recovered).txt"
    assert recovered.read_bytes() == b"old"
    assert [r["rel_path"] for r in res["restored"]] == [
        "workspace/doc (recovered).txt",
    ]
    assert res["renamed"][0]["original"] == "workspace/doc.txt"
    assert res["renamed"][0]["restored_as"] == "workspace/doc (recovered).txt"
    assert _fanout_calls == [(AGENT, "workspace/doc (recovered).txt", b"old")]


@pytest.mark.asyncio
async def test_restore_denies_member_on_shared(_fanout_calls):
    _make_agent()
    from api.agents.agents import restore_recover_bin, RecoverRestoreRequest
    e = rb.capture(AGENT, "workspace/shared.txt", b"x", "deleted")  # scope=shared

    res = await restore_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]),
        _member_with_agent(),
    )
    assert res["denied"] == [e["entry_id"]]
    assert res["restored"] == []
    # NOT consumed — a manager can still recover it.
    assert rb.get(e["entry_id"]) is not None
    assert _fanout_calls == []


@pytest.mark.asyncio
async def test_restore_allows_member_on_own_user_file(_fanout_calls, monkeypatch):
    _make_agent()
    from api.agents.agents import restore_recover_bin, RecoverRestoreRequest
    monkeypatch.setattr(
        "storage.database.get_user_sub_by_username",
        lambda slug: "user-viewer" if slug == "viewer" else None,
    )
    e = rb.capture(AGENT, "users/viewer/note.txt", b"mine", "deleted")
    assert e["scope"] == "user" and e["owner_sub"] == "user-viewer"

    res = await restore_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]),
        _member_with_agent(),
    )
    assert [r["rel_path"] for r in res["restored"]] == ["users/viewer/note.txt"]
    assert rb.get(e["entry_id"]) is None


@pytest.mark.asyncio
async def test_discard_removes_without_restoring(_fanout_calls):
    _make_agent()
    from api.agents.agents import discard_recover_bin, RecoverRestoreRequest
    e = rb.capture(AGENT, "workspace/junk.txt", b"x", "deleted")
    res = await discard_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]), _admin(),
    )
    assert res["discarded"] == [e["entry_id"]]
    assert res["denied"] == []
    assert rb.get(e["entry_id"]) is None  # gone for good
    assert _fanout_calls == []  # no restore, no re-sync


@pytest.mark.asyncio
async def test_discard_denies_member_on_shared():
    _make_agent()
    from api.agents.agents import discard_recover_bin, RecoverRestoreRequest
    e = rb.capture(AGENT, "workspace/shared.txt", b"x", "deleted")  # scope=shared
    res = await discard_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]),
        _member_with_agent(),
    )
    assert res["denied"] == [e["entry_id"]]
    assert res["discarded"] == []
    assert rb.get(e["entry_id"]) is not None  # manager-only — NOT removed


@pytest.mark.asyncio
async def test_restore_editor_can_restore_shared_workspace(_fanout_calls):
    _make_agent()
    from api.agents.agents import restore_recover_bin, RecoverRestoreRequest
    e = rb.capture(AGENT, "workspace/team.txt", b"data", "conflict")
    res = await restore_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]),
        _editor_with_agent(),
    )
    assert [r["rel_path"] for r in res["restored"]] == ["workspace/team.txt"]
    assert rb.get(e["entry_id"]) is None


@pytest.mark.asyncio
async def test_restore_editor_denied_on_knowledge(_fanout_calls):
    _make_agent()
    from api.agents.agents import restore_recover_bin, RecoverRestoreRequest
    e = rb.capture(AGENT, "knowledge/ref.md", b"data", "conflict")
    res = await restore_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]),
        _editor_with_agent(),
    )
    assert res["denied"] == [e["entry_id"]]
    assert rb.get(e["entry_id"]) is not None  # manager-tier — editor cannot


@pytest.mark.asyncio
async def test_restore_manager_can_restore_config(_fanout_calls):
    _make_agent()
    from api.agents.agents import restore_recover_bin, RecoverRestoreRequest
    e = rb.capture(AGENT, "config/prompt.md", b"data", "conflict")
    res = await restore_recover_bin(
        AGENT, RecoverRestoreRequest(entry_ids=[e["entry_id"]]),
        _manager_with_agent(),
    )
    assert [r["rel_path"] for r in res["restored"]] == ["config/prompt.md"]
