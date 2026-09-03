"""Shared knowledge libraries — store, projector, gates, API.

Covers the v1 shared-libraries contract:
promote-in-place + per-consumer mirrors under
``knowledge/shared/<source>/``; RO strict everywhere (revert→trash);
RW adoption incl. the explicit-delete channel; the ``can_write_back``
subtree rule; the path-policy mirror gate; sandbox mount shapes; prompt
rows; and the ``require_creator_interactive`` carve-out on the REST API
(+ the widened departments field gate).

Run: cd proxy && python -m pytest tests/agents/test_knowledge_libraries.py -v
"""

import asyncio
import os
import shutil

import pytest
from fastapi import HTTPException

import config
from auth.providers import UserContext
from storage import agent_store, db_knowledge_libraries, recover_bin_store

SRC = "kl-source"
CON_A = "kl-con-a"          # collaborative consumer, RO attachment
CON_B = "kl-con-b"          # collaborative consumer, RW attachment
PERSONAL = "kl-personal"    # personal-only consumer
OTHER = "kl-other"          # exists, never attached

ADMIN_SUB = "user-admin"


def _mk_agents():
    for slug in (SRC, CON_A, CON_B, PERSONAL, OTHER):
        shutil.rmtree(config.get_agent_dir(slug), ignore_errors=True)
    agent_store.create_agent(SRC, "Source", collaborative=True, default_scope="agent")
    agent_store.create_agent(CON_A, "ConA", collaborative=True, default_scope="agent")
    agent_store.create_agent(CON_B, "ConB", collaborative=True, default_scope="agent")
    agent_store.create_agent(PERSONAL, "Pers", collaborative=False, default_scope="user")
    agent_store.create_agent(OTHER, "Other", collaborative=True, default_scope="user")


def _seed_source():
    k = config.get_agent_dir(SRC) / "knowledge"
    (k / "docs").mkdir(parents=True, exist_ok=True)
    (k / "index.md").write_text("index v1")
    (k / "docs" / "brand.md").write_text("brand v1")
    # Must never project:
    (k / "memory").mkdir(exist_ok=True)
    (k / "memory" / "topic.md").write_text("agent memory")
    (k / ".credentials").mkdir(exist_ok=True)
    (k / ".credentials" / "tok.json").write_text("{}")
    (k / "shared").mkdir(exist_ok=True)
    return k


@pytest.fixture
def kl_env(temp_db):
    _mk_agents()
    _seed_source()
    db_knowledge_libraries.promote(SRC, created_by=ADMIN_SUB)
    db_knowledge_libraries.attach(SRC, CON_A, writable=False, created_by=ADMIN_SUB)
    db_knowledge_libraries.attach(SRC, CON_B, writable=True, created_by=ADMIN_SUB)
    return temp_db


@pytest.fixture
def quiet_fanout(monkeypatch):
    """Record projector fan-out jobs instead of touching satellite machinery."""
    from services.knowledge import library_projector
    calls: list[tuple] = []

    async def _rec(slug, rel, path):
        calls.append((slug, rel, path))

    monkeypatch.setattr(library_projector, "_fan_out", _rec)
    return calls


def _run(coro):
    return asyncio.run(coro)


def _mirror(consumer):
    from services.knowledge import library_projector
    return library_projector.mirror_dir(consumer, SRC)


# ───────────────────────────────────────────────────────────────────────────
# Store
# ───────────────────────────────────────────────────────────────────────────


class TestStore:
    def test_promote_idempotent(self, temp_db):
        _mk_agents()
        assert db_knowledge_libraries.promote(SRC, created_by=ADMIN_SUB) is True
        assert db_knowledge_libraries.promote(SRC, created_by=ADMIN_SUB) is False
        assert db_knowledge_libraries.is_promoted(SRC)

    def test_attach_requires_promoted(self, temp_db):
        _mk_agents()
        with pytest.raises(ValueError):
            db_knowledge_libraries.attach(
                SRC, CON_A, writable=False, created_by=ADMIN_SUB)

    def test_reattach_never_flips_writable(self, kl_env):
        assert db_knowledge_libraries.attach(
            SRC, CON_A, writable=True, created_by=ADMIN_SUB) is False
        assert db_knowledge_libraries.writable_attachment(SRC, CON_A) is False
        assert db_knowledge_libraries.set_writable(SRC, CON_A, True) is True
        assert db_knowledge_libraries.writable_attachment(SRC, CON_A) is True

    def test_consumer_and_source_views(self, kl_env):
        atts = db_knowledge_libraries.attachments_for_consumer(CON_B)
        assert atts == [{"source_agent": SRC, "subdir": "", "writable": True,
                         "name": ""}]
        cons = db_knowledge_libraries.consumers_of(SRC)
        assert {c["consumer_agent"] for c in cons} == {CON_A, CON_B}
        assert db_knowledge_libraries.writable_pairs_for(CON_B) == {(SRC, "")}
        assert db_knowledge_libraries.writable_pairs_for(CON_A) == frozenset()

    def test_unpromote_detaches(self, kl_env):
        consumers = db_knowledge_libraries.unpromote(SRC)
        assert set(consumers) == {CON_A, CON_B}
        assert not db_knowledge_libraries.is_promoted(SRC)
        assert db_knowledge_libraries.attachments_for_consumer(CON_A) == []

    def test_delete_agent_cascades_both_directions(self, kl_env):
        agent_store.delete_agent(CON_A)
        assert {c["consumer_agent"]
                for c in db_knowledge_libraries.consumers_of(SRC)} == {CON_B}
        agent_store.delete_agent(SRC)
        assert not db_knowledge_libraries.is_promoted(SRC)
        assert db_knowledge_libraries.attachments_for_consumer(CON_B) == []


# ───────────────────────────────────────────────────────────────────────────
# Projector
# ───────────────────────────────────────────────────────────────────────────


class TestProjector:
    def test_reconcile_copies_and_excludes(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        mir = _mirror(CON_A)
        assert (mir / "index.md").read_text() == "index v1"
        assert (mir / "docs" / "brand.md").read_text() == "brand v1"
        assert not (mir / "memory").exists()
        assert not (mir / ".credentials").exists()
        assert not (mir / "shared").exists()
        # mtime equalized with the source file.
        src_st = (config.get_agent_dir(SRC) / "knowledge" / "index.md").stat()
        assert int((mir / "index.md").stat().st_mtime) == int(src_st.st_mtime)

    def test_segment_exclusions_any_depth(self, kl_env, quiet_fanout):
        # .git/.credentials are SEGMENT-excluded (a git clone inside the
        # knowledge folder must never fan its object store out); nested
        # "memory"/"shared" folders are legitimate content — only the
        # top-level ones are reserved.
        from services.knowledge import library_projector
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "repo" / ".git" / "objects").mkdir(parents=True)
        (k / "repo" / ".git" / "objects" / "pack").write_text("git bytes")
        (k / "repo" / ".git" / "HEAD").write_text("ref: main")
        (k / "repo" / "README.md").write_text("repo readme")
        (k / "docs" / ".credentials").mkdir(parents=True, exist_ok=True)
        (k / "docs" / ".credentials" / "t.json").write_text("{}")
        (k / "docs" / "memory").mkdir(exist_ok=True)
        (k / "docs" / "memory" / "note.md").write_text("nested memory ok")
        (k / "docs" / "shared").mkdir(exist_ok=True)
        (k / "docs" / "shared" / "asset.md").write_text("nested shared ok")
        _run(library_projector.reconcile_source(SRC))
        mir = _mirror(CON_A)
        assert (mir / "repo" / "README.md").read_text() == "repo readme"
        assert not (mir / "repo" / ".git").exists()
        assert not (mir / "docs" / ".credentials").exists()
        assert (mir / "docs" / "memory" / "note.md").exists()
        assert (mir / "docs" / "shared" / "asset.md").exists()
        # Targeted propagation refuses excluded rels outright.
        assert library_projector.is_excluded_rel("repo/.git/HEAD")
        assert library_projector.is_excluded_rel("docs/.credentials/t.json")
        assert not library_projector.is_excluded_rel("docs/memory/note.md")
        assert not library_projector.is_excluded_rel("docs/shared/asset.md")
        _run(library_projector.propagate_source_write(SRC, "repo/.git/HEAD"))
        assert not (mir / "repo" / ".git").exists()

    def test_source_edit_and_delete_propagate(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "index.md").write_text("index v2")
        _run(library_projector.propagate_source_write(SRC, "index.md"))
        assert (_mirror(CON_A) / "index.md").read_text() == "index v2"
        (k / "docs" / "brand.md").unlink()
        _run(library_projector.propagate_source_write(
            SRC, "docs/brand.md", deleted=True))
        assert not (_mirror(CON_B) / "docs" / "brand.md").exists()

    def test_ro_local_edit_healed_and_binned(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        mir_file = _mirror(CON_A) / "index.md"
        mir_file.write_text("tampered")
        # Future mtime = "edited after projection".
        st = mir_file.stat()
        os.utime(mir_file, (st.st_atime, st.st_mtime + 60))
        _run(library_projector.reconcile_source(SRC))
        assert mir_file.read_text() == "index v1"
        entries = recover_bin_store.list_for(CON_A, ADMIN_SUB, True, True, True)
        assert any("index.md" in e["rel_path"] for e in entries)

    def test_rw_edit_adopted_into_source_and_siblings(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        mir_file = _mirror(CON_B) / "index.md"
        mir_file.write_text("edited by con-b")
        st = mir_file.stat()
        os.utime(mir_file, (st.st_atime, st.st_mtime + 60))
        _run(library_projector.propagate_mirror_write(CON_B, SRC, "index.md"))
        src_file = config.get_agent_dir(SRC) / "knowledge" / "index.md"
        assert src_file.read_text() == "edited by con-b"
        assert (_mirror(CON_A) / "index.md").read_text() == "edited by con-b"

    def test_ro_mirror_write_refused(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        assert _run(library_projector.propagate_mirror_write(
            CON_A, SRC, "index.md")) is False

    def test_explicit_mirror_delete_deletes_source(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        assert _run(library_projector.propagate_mirror_delete(
            CON_B, SRC, "index.md")) is True
        assert not (config.get_agent_dir(SRC) / "knowledge" / "index.md").exists()
        assert not (_mirror(CON_A) / "index.md").exists()
        entries = recover_bin_store.list_for(SRC, ADMIN_SUB, True, True, True)
        assert any(e["reason"] == "deleted" for e in entries)

    # ── Merge base (2026-09-03): deletes are attributed, not healed blindly ──

    def test_reconcile_records_merge_base(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        from storage import db_library_mirror_state as st
        _run(library_projector.reconcile_source(SRC))
        rows = st.get_map(CON_B, SRC)
        assert set(rows) == {"index.md", "docs/brand.md"}
        src = config.get_agent_dir(SRC) / "knowledge" / "index.md"
        assert rows["index.md"]["base_size"] == src.stat().st_size
        assert rows["index.md"]["base_hash"] == library_projector._hash_file(src)
        # A second sweep is a no-op on the store (steady state writes nothing).
        _run(library_projector.reconcile_source(SRC))
        assert st.get_map(CON_B, SRC) == rows

    def test_rw_plain_rm_after_sync_propagates(self, kl_env, quiet_fanout):
        """The bug class of 2026-09-03: a sandbox `rm` in an RW mirror used to
        heal back 5 minutes later. With a base row and an unchanged source it
        is a deliberate delete → source + every other mirror lose the file,
        the source bytes land in the source's recover-bin."""
        from services.knowledge import library_projector
        from storage import db_library_mirror_state as st
        _run(library_projector.reconcile_source(SRC))
        (_mirror(CON_B) / "index.md").unlink()  # RW mirror, plain rm
        _run(library_projector.reconcile_source(SRC))
        assert not (config.get_agent_dir(SRC) / "knowledge" / "index.md").exists()
        assert not (_mirror(CON_B) / "index.md").exists()
        assert not (_mirror(CON_A) / "index.md").exists()
        entries = recover_bin_store.list_for(SRC, ADMIN_SUB, True, True, True)
        assert any(e["reason"] == "deleted" and "index.md" in e["rel_path"] for e in entries)
        assert "index.md" not in st.get_map(CON_B, SRC)
        assert "index.md" not in st.get_map(CON_A, SRC)
        # Fan-out saw the source delete and the sibling mirror delete.
        assert (SRC, "knowledge/index.md", None) in quiet_fanout
        assert (CON_A, f"knowledge/shared/{SRC}/index.md", None) in quiet_fanout
        # Untouched files survive.
        assert (_mirror(CON_B) / "docs" / "brand.md").read_text() == "brand v1"

    def test_rw_rm_before_any_sync_heals(self, kl_env, quiet_fanout):
        """No base row = the mirror never converged on the file → first
        projection, never a delete (an un-synced/new consumer can't delete)."""
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        # Simulate "never converged": drop the base row, then rm the mirror.
        from storage import db_library_mirror_state as st
        st.delete_many([(CON_B, SRC, "index.md")])
        (_mirror(CON_B) / "index.md").unlink()
        _run(library_projector.reconcile_source(SRC))
        assert (_mirror(CON_B) / "index.md").read_text() == "index v1"
        assert (config.get_agent_dir(SRC) / "knowledge" / "index.md").exists()

    def test_rw_rm_loses_to_newer_source_edit(self, kl_env, quiet_fanout):
        """Mirror deleted but the source changed since the base → the edit
        wins: the mirror heals with the new content, nothing is deleted."""
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        (_mirror(CON_B) / "index.md").unlink()
        src = config.get_agent_dir(SRC) / "knowledge" / "index.md"
        src.write_text("index v2 (edited after the mirror's delete)")
        _run(library_projector.reconcile_source(SRC))
        assert src.exists()
        assert (_mirror(CON_B) / "index.md").read_text() == "index v2 (edited after the mirror's delete)"
        assert (_mirror(CON_A) / "index.md").read_text() == "index v2 (edited after the mirror's delete)"

    def test_ro_rm_still_heals(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        (_mirror(CON_A) / "index.md").unlink()  # RO mirror, plain rm
        _run(library_projector.reconcile_source(SRC))
        assert (_mirror(CON_A) / "index.md").read_text() == "index v1"
        assert (config.get_agent_dir(SRC) / "knowledge" / "index.md").exists()

    def test_source_rm_not_readopted_from_writable_mirror(self, kl_env, quiet_fanout):
        """The second hole of 2026-09-03: a source-side `rm` used to be adopted
        back from any writable mirror that still held the file. With a base
        row the mirror copy is removed instead (recover-bin only if it
        diverged from the base)."""
        from services.knowledge import library_projector
        from storage import db_library_mirror_state as st
        _run(library_projector.reconcile_source(SRC))
        src = config.get_agent_dir(SRC) / "knowledge" / "index.md"
        src.unlink()  # source agent's sandbox rm — no chokepoint
        _run(library_projector.reconcile_source(SRC))
        assert not src.exists()
        assert not (_mirror(CON_B) / "index.md").exists()
        assert not (_mirror(CON_A) / "index.md").exists()
        assert "index.md" not in st.get_map(CON_B, SRC)
        # Unchanged copies are not binned (the source's git history is the
        # safety net); nothing "deleted" is recorded for the mirror.
        entries = recover_bin_store.list_for(CON_B, ADMIN_SUB, True, True, True)
        assert not any("index.md" in e["rel_path"] for e in entries)

    def test_source_rm_bins_a_diverged_mirror_copy(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        src = config.get_agent_dir(SRC) / "knowledge" / "index.md"
        src.unlink()
        mir = _mirror(CON_B) / "index.md"
        mir.write_text("local edit the source never saw")
        stt = mir.stat()
        os.utime(mir, (stt.st_atime, stt.st_mtime + 60))
        _run(library_projector.reconcile_source(SRC))
        assert not mir.exists()
        entries = recover_bin_store.list_for(CON_B, ADMIN_SUB, True, True, True)
        assert any(e["reason"] == "conflict" and "index.md" in e["rel_path"] for e in entries)

    def test_new_rw_mirror_file_still_adopted(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        from storage import db_library_mirror_state as st
        _run(library_projector.reconcile_source(SRC))
        (_mirror(CON_B) / "docs" / "new.md").write_text("authored in the mirror")
        _run(library_projector.reconcile_source(SRC))
        assert (config.get_agent_dir(SRC) / "knowledge" / "docs" / "new.md").read_text() == "authored in the mirror"
        assert (_mirror(CON_A) / "docs" / "new.md").read_text() == "authored in the mirror"
        assert "docs/new.md" in st.get_map(CON_B, SRC)
        assert "docs/new.md" in st.get_map(CON_A, SRC)

    def test_mass_delete_guard_heals_a_wiped_mirror(self, kl_env, quiet_fanout, monkeypatch):
        from services.knowledge import library_projector
        k = config.get_agent_dir(SRC) / "knowledge"
        for i in range(6):
            (k / "docs" / f"f{i}.md").write_text(f"file {i}")
        _run(library_projector.reconcile_source(SRC))
        # 8 tracked files; wipe the whole mirror; guard = max(MIN, 50 %).
        monkeypatch.setattr(library_projector, "MASS_DELETE_MIN", 2)
        shutil.rmtree(_mirror(CON_B))
        _run(library_projector.reconcile_source(SRC))
        for i in range(6):
            assert (k / "docs" / f"f{i}.md").exists()
            assert (_mirror(CON_B) / "docs" / f"f{i}.md").exists()
        assert (k / "index.md").exists()
        # Below the guard, deletes DO propagate (2 of 8 with MIN=2 → limit 4).
        (_mirror(CON_B) / "docs" / "f0.md").unlink()
        (_mirror(CON_B) / "docs" / "f1.md").unlink()
        _run(library_projector.reconcile_source(SRC))
        assert not (k / "docs" / "f0.md").exists()
        assert not (k / "docs" / "f1.md").exists()
        assert (k / "docs" / "f2.md").exists()

    def test_explicit_delete_drops_every_base_row(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        from storage import db_library_mirror_state as st
        _run(library_projector.reconcile_source(SRC))
        assert _run(library_projector.propagate_mirror_delete(CON_B, SRC, "index.md")) is True
        assert "index.md" not in st.get_map(CON_A, SRC)
        assert "index.md" not in st.get_map(CON_B, SRC)
        # And a later sweep does not resurrect anything.
        _run(library_projector.reconcile_source(SRC))
        assert not (config.get_agent_dir(SRC) / "knowledge" / "index.md").exists()

    def test_teardown_drops_base_rows(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        from storage import db_library_mirror_state as st
        _run(library_projector.reconcile_source(SRC))
        assert st.get_map(CON_A, SRC)
        _run(library_projector.detach_teardown(SRC, CON_A))
        assert st.get_map(CON_A, SRC) == {}
        assert st.get_map(CON_B, SRC)
        _run(library_projector.teardown_source(SRC))
        assert st.get_map(CON_B, SRC) == {}

    def test_quick_check_skips_hashing_converged_pairs(self, kl_env, quiet_fanout, monkeypatch):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        calls: list[str] = []
        real = library_projector._hash_file

        def _counting(path):
            calls.append(str(path))
            return real(path)

        monkeypatch.setattr(library_projector, "_hash_file", _counting)
        _run(library_projector.reconcile_source(SRC))
        assert calls == []  # equal size + mtime + fresh base → no reads at all

    def test_turn_end_kick_targets_writable_sources(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        assert library_projector._sources_touched_by(CON_B) == [SRC]
        assert library_projector._sources_touched_by(CON_A) == []      # RO only
        assert library_projector._sources_touched_by(SRC) == [SRC]     # promoted
        assert library_projector._sources_touched_by(OTHER) == []

    def test_detach_and_teardown(self, kl_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        _run(library_projector.detach_teardown(SRC, CON_A))
        assert not _mirror(CON_A).exists()
        _run(library_projector.teardown_source(SRC))
        assert not _mirror(CON_B).exists()


# ───────────────────────────────────────────────────────────────────────────
# Sync gates (can_write_back + merge classification)
# ───────────────────────────────────────────────────────────────────────────


class TestSyncGates:
    def test_can_write_back_mirror_rules(self, temp_db):
        from core.remote.file_sync import can_write_back
        p = f"knowledge/shared/{SRC}/index.md"
        # No attachment context → fail closed, even owner-tier.
        assert can_write_back(p, "manager", "alice") is False
        assert can_write_back(
            p, "manager", "alice", writable_libraries=frozenset()) is False
        assert can_write_back(
            p, "manager", "alice",
            writable_libraries=frozenset({(SRC, "")})) is True
        # Attachment RW is necessary but not sufficient: knowledge tier holds.
        assert can_write_back(
            p, "editor", "alice",
            writable_libraries=frozenset({(SRC, "")})) is False
        assert can_write_back(
            p, "manager", "",
            writable_libraries=frozenset({(SRC, "")})) is False
        # Plain knowledge stays owner-curation.
        assert can_write_back("knowledge/x.md", "manager", "alice") is True

    def test_credentials_segment_never_pushes(self, temp_db):
        from core.remote.file_sync import _is_other_user_or_sensitive
        assert _is_other_user_or_sensitive(
            f"knowledge/shared/{SRC}/.credentials/t.json", "alice") is True
        assert _is_other_user_or_sensitive(
            "knowledge/.credentials/t.json", "alice") is True

    def test_merge_push_only_captures_ro_divergence(self, temp_db):
        from core.remote.file_sync import FileEntry, diff_manifests
        ro_prefix = {f"knowledge/shared/{SRC}/"}
        local = [FileEntry(f"knowledge/shared/{SRC}/a.md", "h-platform", 100.0, 10)]
        remote = [{"path": f"knowledge/shared/{SRC}/a.md", "hash": "h-edited",
                   "mtime": 200.0, "size": 12}]
        plan = diff_manifests(
            local, remote, push_only_dirs=set(ro_prefix) | {"config/"},
            scrub_dirs=ro_prefix, target_username=None, target_role="manager",
            session_username="alice",
        )
        act = next(a for a in plan.actions
                   if a.rel_path == f"knowledge/shared/{SRC}/a.md")
        assert act.op == "push"
        assert act.capture_side == "satellite"
        assert act.capture_reason == "conflict"

    def test_merge_scrubs_mirror_extras_on_admin_machines(self, temp_db):
        from core.remote.file_sync import diff_manifests
        ro_prefix = {f"knowledge/shared/{SRC}/"}
        remote = [{"path": f"knowledge/shared/{SRC}/stray.md", "hash": "h1",
                   "mtime": 200.0, "size": 5}]
        plan = diff_manifests(
            [], remote, push_only_dirs=set(ro_prefix) | {"config/"},
            scrub_dirs=ro_prefix, target_username=None, target_role="manager",
            session_username="alice",
        )
        assert f"knowledge/shared/{SRC}/stray.md" in plan.to_scrub


# ───────────────────────────────────────────────────────────────────────────
# Path policy + files API gate
# ───────────────────────────────────────────────────────────────────────────


def _ctx(role="manager", username="alice", libs=()):
    from auth.path_policy import SecurityContext
    return SecurityContext(
        role=role, username=username, agent=CON_A, is_admin_agent=False,
        knowledge_libraries=tuple(libs),
    )


class TestPathPolicy:
    def _write(self, ctx, rel):
        from auth.path_policy import check_host_path_access
        host = (config.AGENTS_DIR / CON_A / rel).resolve()
        return check_host_path_access(host, ctx, writing=True)

    def test_ro_mirror_denied_for_manager(self, temp_db):
        _mk_agents()
        d = self._write(_ctx(libs=((SRC, "", False),)),
                        f"knowledge/shared/{SRC}/index.md")
        assert d.allowed is False

    def test_rw_mirror_allowed_for_manager(self, temp_db):
        _mk_agents()
        d = self._write(_ctx(libs=((SRC, "", True),)),
                        f"knowledge/shared/{SRC}/index.md")
        assert d.allowed is True

    def test_rw_mirror_still_denied_for_editor(self, temp_db):
        _mk_agents()
        d = self._write(_ctx(role="editor", libs=((SRC, "", True),)),
                        f"knowledge/shared/{SRC}/index.md")
        assert d.allowed is False

    def test_shared_root_reserved(self, temp_db):
        _mk_agents()
        d = self._write(_ctx(libs=((SRC, "", True),)), "knowledge/shared/stray.md")
        assert d.allowed is False

    def test_plain_knowledge_unchanged(self, temp_db):
        _mk_agents()
        d = self._write(_ctx(), "knowledge/notes.md")
        assert d.allowed is True

    def test_ctx_roundtrip_serialization(self, temp_db):
        from core.session.session_state import (
            _deserialize_security_ctx, _serialize_security_ctx,
        )
        import json
        ctx = _ctx(libs=((SRC, "", False), (OTHER, "docs", True)))
        restored = _deserialize_security_ctx(
            json.loads(json.dumps(_serialize_security_ctx(ctx))))
        assert restored == ctx


class TestFilesApiGate:
    def test_mirror_write_gate(self, kl_env):
        from api.agents.files import _check_library_mirror_write
        with pytest.raises(HTTPException) as e:
            _check_library_mirror_write(f"knowledge/shared/{SRC}/x.md", CON_A)
        assert e.value.status_code == 403
        _check_library_mirror_write(f"knowledge/shared/{SRC}/x.md", CON_B)
        with pytest.raises(HTTPException):
            _check_library_mirror_write("knowledge/shared", CON_B)
        _check_library_mirror_write("knowledge/plain.md", CON_A)


# ───────────────────────────────────────────────────────────────────────────
# Sandbox mounts + prompt rows
# ───────────────────────────────────────────────────────────────────────────


class TestSandboxMounts:
    def _args(self, **kw):
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig
        from pathlib import Path
        cfg = SandboxConfig(
            role=kw.get("role", "manager"),
            username=kw.get("username", "alice"),
            agent_name=CON_A,
            is_admin_agent=False,
            host_agents_dir=config.AGENTS_DIR.resolve(),
            host_mcps_dir=config.MCPS_DIR.resolve(),
            host_claude_dir=Path("/tmp/claude-test"),
            config_visible=kw.get("config_visible"),
            mount_shared=kw.get("mount_shared", True),
            knowledge_libraries=kw.get("libs", []),
        )
        return SandboxBuilder(cfg)._workspace_mounts()

    def test_ro_attachment_gets_nested_robind(self, kl_env):
        args = self._args(libs=[(SRC, "", False)])
        joined = " ".join(args)
        assert f"--ro-bind" in joined
        assert f"/knowledge/shared/{SRC}" in joined

    def test_rw_attachment_rides_parent_bind(self, kl_env):
        args = self._args(libs=[(SRC, "", True)])
        assert f"/knowledge/shared/{SRC}" not in " ".join(args)

    def test_personal_only_mounts_mirrors_ro(self, kl_env):
        args = self._args(mount_shared=False, libs=[(SRC, "", True)])
        j = " ".join(args)
        assert f"/knowledge/shared/{SRC}" in j
        assert "--bind " + str(
            config.get_agent_dir(CON_A) / "knowledge") + " /knowledge" not in j

    def test_agent_scope_owner_gets_nested_robind(self, kl_env):
        args = self._args(username="", config_visible=True, libs=[(SRC, "", False)])
        assert f"/knowledge/shared/{SRC}" in " ".join(args)


class TestPromptRows:
    def test_folders_rows_render(self, temp_db):
        from auth.path_prompt import _build_folders_section
        ctx = _ctx(libs=((SRC, "", False), (OTHER, "docs", True)))
        text = _build_folders_section(ctx, default_scope="agent")
        assert f"/knowledge/shared/{SRC}/` (RO)" in text
        assert f"/knowledge/shared/{OTHER}/docs/` (RW)" in text

    def test_service_block_renders_ro(self, temp_db):
        from auth.path_prompt import _build_folders_section
        ctx = _ctx(username="", libs=((SRC, "", True),))
        text = _build_folders_section(ctx, default_scope="agent")
        assert f"/knowledge/shared/{SRC}/` (RO)" in text


# ───────────────────────────────────────────────────────────────────────────
# knowledge_rw gate coherence — manager-provenance agent-scope task fires
# ───────────────────────────────────────────────────────────────────────────


class TestKnowledgeRwGateCoherence:
    """Every enforcement surface must give the SAME verdict for a
    manager-provenance agent-scope task fire (``knowledge_rw=True``):
    kernel mount argv, the permission-hook write gate, the satellite
    write-back predicate (both the per-turn shape and the initial-sync
    merge), and the prompt's Folders block. /config and memory/ stay
    denied everywhere."""

    def _sandbox_args(self, *, knowledge_rw, libs=()):
        from pathlib import Path
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig
        cfg = SandboxConfig(
            role="manager", username="", agent_name=CON_A,
            is_admin_agent=False,
            host_agents_dir=config.AGENTS_DIR.resolve(),
            host_mcps_dir=config.MCPS_DIR.resolve(),
            host_claude_dir=Path("/tmp/claude-test"),
            config_visible=False,
            knowledge_rw=knowledge_rw,
            knowledge_libraries=list(libs),
        )
        return SandboxBuilder(cfg)._workspace_mounts()

    def _svc_ctx(self, *, knowledge_rw, libs=()):
        from auth.path_policy import SecurityContext
        return SecurityContext(
            role="manager", username="", agent=CON_A, is_admin_agent=False,
            session_scope="agent", config_visible=False,
            knowledge_libraries=tuple(libs), knowledge_rw=knowledge_rw,
        )

    def _write(self, ctx, rel):
        from auth.path_policy import check_host_path_access
        host = (config.AGENTS_DIR / CON_A / rel).resolve()
        return check_host_path_access(host, ctx, writing=True)

    def test_mount_knowledge_rw_bind_no_config(self, kl_env):
        args = self._sandbox_args(knowledge_rw=True)
        pairs = list(zip(args, args[1:]))
        kdir = str(config.get_agent_dir(CON_A) / "knowledge")
        assert ("--bind", kdir) in pairs           # knowledge RW
        assert "/config" not in " ".join(args)     # config never mounts

    def test_mount_default_stays_ro(self, kl_env):
        args = self._sandbox_args(knowledge_rw=False)
        pairs = list(zip(args, args[1:]))
        kdir = str(config.get_agent_dir(CON_A) / "knowledge")
        assert ("--ro-bind", kdir) in pairs

    def test_mount_ro_mirror_nested_under_rw_parent(self, kl_env):
        # The newly-RW branch must keep RO mirrors kernel-RO (the nested
        # --ro-bind loop the config_visible branch has).
        args = self._sandbox_args(knowledge_rw=True, libs=[(SRC, "", False)])
        j = " ".join(args)
        assert f"--ro-bind" in j and f"/knowledge/shared/{SRC}" in j

    def test_hook_knowledge_write_follows_flag(self, temp_db):
        _mk_agents()
        assert self._write(
            self._svc_ctx(knowledge_rw=True), "knowledge/notes.md").allowed
        assert not self._write(
            self._svc_ctx(knowledge_rw=False), "knowledge/notes.md").allowed

    def test_hook_config_denied_even_with_flag(self, temp_db):
        _mk_agents()
        d = self._write(self._svc_ctx(knowledge_rw=True), "config/agent.md")
        assert d.allowed is False

    def test_hook_memory_denied_even_with_flag(self, temp_db):
        _mk_agents()
        d = self._write(
            self._svc_ctx(knowledge_rw=True), "knowledge/memory/topic.md")
        assert d.allowed is False
        assert "memory" in (d.reason or "")

    def test_hook_mirror_rule_composes_with_flag(self, temp_db):
        _mk_agents()
        # RO mirror: denied even under provenance.
        assert not self._write(
            self._svc_ctx(knowledge_rw=True, libs=((SRC, "", False),)),
            f"knowledge/shared/{SRC}/x.md").allowed
        # RW mirror + provenance: both required, both present → allowed.
        assert self._write(
            self._svc_ctx(knowledge_rw=True, libs=((SRC, "", True),)),
            f"knowledge/shared/{SRC}/x.md").allowed
        # RW mirror WITHOUT provenance: denied for the service session.
        assert not self._write(
            self._svc_ctx(knowledge_rw=False, libs=((SRC, "", True),)),
            f"knowledge/shared/{SRC}/x.md").allowed

    def test_can_write_back_knowledge_with_flag(self, temp_db):
        from core.remote.file_sync import can_write_back
        assert can_write_back(
            "knowledge/x.md", "manager", "", knowledge_rw=True) is True
        assert can_write_back(
            "knowledge/x.md", "manager", "", knowledge_rw=False) is False
        # Owner tier still required — the flag never widens an editor.
        assert can_write_back(
            "knowledge/x.md", "editor", "", knowledge_rw=True) is False
        # config/ is untouched by the flag.
        assert can_write_back(
            "config/agent.md", "manager", "", knowledge_rw=True) is False

    def test_can_write_back_mirror_with_flag(self, temp_db):
        from core.remote.file_sync import can_write_back
        p = f"knowledge/shared/{SRC}/index.md"
        assert can_write_back(
            p, "manager", "", writable_libraries=frozenset({(SRC, "")}),
            knowledge_rw=True) is True
        assert can_write_back(
            p, "manager", "", writable_libraries=frozenset({(SRC, "")}),
            knowledge_rw=False) is False
        assert can_write_back(
            p, "manager", "", writable_libraries=frozenset(),
            knowledge_rw=True) is False

    def test_initial_sync_merge_agrees_with_the_flag(self, temp_db):
        # The initial-sync pull gate (diff_manifests) must agree with the
        # per-turn applier: a satellite-authored knowledge file pulls ONLY
        # when the session carries the provenance flag.
        from core.remote.file_sync import diff_manifests
        remote = [{"path": "knowledge/new.md", "hash": "h1",
                   "mtime": 100.0, "size": 5}]
        with_flag = diff_manifests(
            [], remote, target_username=None, target_role="manager",
            session_username="", knowledge_rw=True,
        )
        assert [a.op for a in with_flag.actions
                if a.rel_path == "knowledge/new.md"] == ["pull"]
        without = diff_manifests(
            [], remote, target_username=None, target_role="manager",
            session_username="", knowledge_rw=False,
        )
        assert not [a for a in without.actions
                    if a.rel_path == "knowledge/new.md"]

    def test_prompt_folders_block_follows_flag(self, temp_db):
        from auth.path_prompt import _build_folders_section
        rw = _build_folders_section(
            self._svc_ctx(knowledge_rw=True), default_scope="agent")
        assert "`/knowledge/` (RW)" in rw
        ro = _build_folders_section(
            self._svc_ctx(knowledge_rw=False), default_scope="agent")
        assert "`/knowledge/` (RO)" in ro

    def test_ctx_roundtrip_keeps_the_flag(self, temp_db):
        import json
        from core.session.session_state import (
            _deserialize_security_ctx, _serialize_security_ctx,
        )
        ctx = self._svc_ctx(knowledge_rw=True, libs=((SRC, "", True),))
        restored = _deserialize_security_ctx(
            json.loads(json.dumps(_serialize_security_ctx(ctx))))
        assert restored == ctx
        assert restored.knowledge_rw is True


# ───────────────────────────────────────────────────────────────────────────
# REST API authz (require_creator_interactive carve-out)
# ───────────────────────────────────────────────────────────────────────────


def _cookie_admin():
    return UserContext(sub=ADMIN_SUB, email="a@t", name="A", role="admin",
                       agents=[])


def _cookie_member():
    return UserContext(sub="user-viewer", email="m@t", name="M", role="member",
                       agents=[SRC, CON_A],
                       agent_roles={SRC: "manager", CON_A: "manager"})


def _session_admin():
    """Real-user-backed session token, platform admin — the carve-out."""
    return UserContext(sub=ADMIN_SUB, email="a@t", name="A", role="admin",
                       agents=[], is_api_key=True, session_id="s1", agent=CON_A)


def _session_nouser():
    return UserContext(sub="session:s2", email="session@internal",
                       name="Session Token", role="agent", agents=[],
                       is_api_key=True, session_id="s2", agent=CON_A)


def _master_key():
    return UserContext(sub="api-key", email="api@internal", name="API Key",
                       role="admin", agents=[], is_api_key=True)


class TestApiAuthz:
    def _promote(self, user, agent=SRC, enabled=True):
        from api.agents.knowledge_libraries import (
            LibraryToggleRequest, set_knowledge_library,
        )
        return _run(set_knowledge_library(
            agent, LibraryToggleRequest(enabled=enabled, name="Test Library"),
            user=user))

    def _attach(self, user, consumer=CON_A, source=SRC, writable=False):
        from api.agents.knowledge_libraries import (
            AttachRequest, attach_knowledge_library,
        )
        return _run(attach_knowledge_library(
            consumer, AttachRequest(source_agent=source, writable=writable),
            user=user))

    def test_cookie_admin_full_flow(self, temp_db):
        _mk_agents()
        _seed_source()
        assert self._promote(_cookie_admin())["status"] == "shared"
        r = self._attach(_cookie_admin())
        assert r["status"] == "attached"

    def test_session_admin_carveout_allowed(self, temp_db):
        _mk_agents()
        _seed_source()
        assert self._promote(_session_admin())["status"] == "shared"

    def test_no_user_session_rejected(self, temp_db):
        _mk_agents()
        with pytest.raises(HTTPException) as e:
            self._promote(_session_nouser())
        assert e.value.status_code == 403

    def test_master_key_rejected(self, temp_db):
        _mk_agents()
        with pytest.raises(HTTPException) as e:
            self._promote(_master_key())
        assert e.value.status_code == 403

    def test_member_rejected(self, temp_db):
        _mk_agents()
        with pytest.raises(HTTPException) as e:
            self._promote(_cookie_member())
        assert e.value.status_code == 403

    def test_attach_self_and_unpromoted_rejected(self, temp_db):
        _mk_agents()
        _seed_source()
        self._promote(_cookie_admin())
        with pytest.raises(HTTPException) as e:
            self._attach(_cookie_admin(), consumer=SRC, source=SRC)
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e:
            self._attach(_cookie_admin(), source=OTHER)
        assert e.value.status_code == 400

    def test_detach_missing_404(self, temp_db):
        _mk_agents()
        from api.agents.knowledge_libraries import detach_knowledge_library
        with pytest.raises(HTTPException) as e:
            _run(detach_knowledge_library(CON_A, SRC, user=_cookie_admin()))
        assert e.value.status_code == 404

    def test_manager_read_endpoint(self, kl_env):
        from api.agents.knowledge_libraries import get_knowledge_attachments
        r = _run(get_knowledge_attachments(CON_A, user=_cookie_member()))
        assert r["attachments"] == [
            {"source_agent": SRC, "subdir": "", "writable": False, "name": "",
             "has_bulletin": False}]
        r2 = _run(get_knowledge_attachments(SRC, user=_cookie_member()))
        assert r2["is_library"] is True
        (lib,) = r2["libraries"]
        assert lib["subdir"] == ""
        assert {c["consumer_agent"] for c in lib["consumers"]} == {CON_A, CON_B}


class TestDepartmentGateCarveout:
    def test_session_admin_may_assign_department(self, temp_db, monkeypatch):
        """The widened field gate: a real-user-backed session principal with
        platform role admin passes; no-user sessions still 403."""
        u = _session_admin()
        assert u.acting_sub == ADMIN_SUB
        assert not (u.acting_sub is None or u.role not in ("admin", "creator"))
        n = _session_nouser()
        assert n.acting_sub is None

    def test_require_creator_interactive_matrix(self, temp_db):
        from auth.providers import require_creator_interactive
        assert require_creator_interactive(_cookie_admin()) is not None
        assert require_creator_interactive(_session_admin()) is not None
        for bad in (_session_nouser(), _master_key(), _cookie_member()):
            with pytest.raises(HTTPException):
                require_creator_interactive(bad)


class TestLibraryDisplayName:
    """The library's human label.

    DISPLAY ONLY by deliberate design: an audit of the alternative (letting
    the name drive the mirror folder) found 32 sites keyed on the path
    segment, including the kernel-level ``--ro-bind`` and five write gates,
    where a partial change silently turns a read-only mirror writable. That
    is deferred to its own release. These tests pin the
    label behaviour AND that the path is still the source agent, so the
    deferred change cannot land here by accident.
    """

    def test_promote_stores_the_label(self, kl_env):
        # Re-promote sets the label on the fixture's existing library.
        db_knowledge_libraries.promote(
            SRC, created_by=ADMIN_SUB, name="Brand Guidelines")
        assert db_knowledge_libraries.library_name(SRC) == "Brand Guidelines"

    def test_repromote_renames_in_place(self, kl_env):
        """Re-sharing with a new label renames without a detach/reattach —
        and reports 'not newly created' so the API's log stays truthful."""
        assert db_knowledge_libraries.promote(
            CON_A, created_by=ADMIN_SUB, name="First") is True
        assert db_knowledge_libraries.promote(
            CON_A, created_by=ADMIN_SUB, name="Second") is False
        assert db_knowledge_libraries.library_name(CON_A) == "Second"

    def test_label_absent_for_unshared(self, kl_env):
        # CON_A is a consumer, never promoted itself.
        assert db_knowledge_libraries.library_name(CON_A) == ""

    def test_label_reaches_every_read_helper(self, kl_env):
        # Re-promote to set the label on the fixture's existing library.
        db_knowledge_libraries.promote(
            SRC, created_by=ADMIN_SUB, name="Brand Guidelines")
        (lib,) = [l for l in db_knowledge_libraries.list_libraries()
                  if l["source_agent"] == SRC]
        assert lib["name"] == "Brand Guidelines"
        (att,) = db_knowledge_libraries.attachments_for_consumer(CON_A)
        assert att["name"] == "Brand Guidelines"

    def test_pre_naming_library_reads_as_blank(self, kl_env):
        """Rows written before the column existed backfill to '' — the UI
        falls back to the agent slug rather than rendering nothing."""
        db_knowledge_libraries.promote(SRC, created_by=ADMIN_SUB)
        assert db_knowledge_libraries.library_name(SRC) == ""

    def test_api_requires_a_name_when_sharing(self, kl_env):
        from api.agents.knowledge_libraries import (
            LibraryToggleRequest, set_knowledge_library,
        )
        with pytest.raises(HTTPException) as exc:
            _run(set_knowledge_library(
                CON_A, LibraryToggleRequest(enabled=True, name="   "),
                user=_cookie_admin()))
        assert exc.value.status_code == 400
        # Rejected before any write — CON_A is still not a library.
        assert not db_knowledge_libraries.is_promoted(CON_A)

    def test_api_rejects_an_overlong_name(self, kl_env):
        from api.agents.knowledge_libraries import (
            LibraryToggleRequest, set_knowledge_library,
        )
        with pytest.raises(HTTPException) as exc:
            _run(set_knowledge_library(
                CON_A, LibraryToggleRequest(enabled=True, name="x" * 65),
                user=_cookie_admin()))
        assert exc.value.status_code == 400

    def test_unshare_needs_no_name(self, kl_env):
        from api.agents.knowledge_libraries import (
            LibraryToggleRequest, set_knowledge_library,
        )
        db_knowledge_libraries.promote(SRC, created_by=ADMIN_SUB, name="X")
        r = _run(set_knowledge_library(
            SRC, LibraryToggleRequest(enabled=False), user=_cookie_admin()))
        assert r["status"] == "unshared"

    def test_get_exposes_the_agents_own_label(self, kl_env):
        from api.agents.knowledge_libraries import get_knowledge_attachments
        db_knowledge_libraries.promote(
            SRC, created_by=ADMIN_SUB, name="Brand Guidelines")
        r = _run(get_knowledge_attachments(SRC, user=_cookie_member()))
        (lib,) = r["libraries"]
        assert lib["name"] == "Brand Guidelines"

    def test_the_mirror_folder_is_still_the_source_agent(self, kl_env):
        """THE GUARD. The label must never reach the path — that is the
        deferred, security-sensitive change."""
        from services.knowledge import library_projector
        db_knowledge_libraries.promote(
            SRC, created_by=ADMIN_SUB, name="Totally Different Name")
        mir = library_projector.mirror_dir(CON_A, SRC)
        assert mir.name == SRC
        assert mir.parent.name == library_projector.SHARED_SUBDIR
        # …and nothing derived from the label appears in the path.
        assert "totally" not in str(mir).lower()


# ───────────────────────────────────────────────────────────────────────────
# Per-folder libraries — schema migration convergence
# ───────────────────────────────────────────────────────────────────────────


# NOTE: the composite-key tables ship migration-free — the single-key v1
# shape never reached a released install (operator decision 2026-08-24:
# the two dev installs were reset by hand). Fresh init IS the only shape.


# ───────────────────────────────────────────────────────────────────────────
# Per-folder libraries — validation, store, projector, boundaries
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def kl_subdir_env(temp_db):
    """SRC shares two disjoint subtrees: 'marketing' (RW to CON_B, RO to
    CON_A) and 'docs' (RO to CON_B). A sibling dir 'marketing-extra' exists
    and is NOT shared — the segment-boundary probe."""
    _mk_agents()
    k = config.get_agent_dir(SRC) / "knowledge"
    (k / "marketing").mkdir(parents=True, exist_ok=True)
    (k / "marketing" / "plan.md").write_text("plan v1")
    (k / "marketing-extra").mkdir(exist_ok=True)
    (k / "marketing-extra" / "secret.md").write_text("not shared")
    (k / "docs").mkdir(exist_ok=True)
    (k / "docs" / "guide.md").write_text("guide v1")
    (k / "private.md").write_text("root file, not shared")
    db_knowledge_libraries.promote(
        SRC, created_by=ADMIN_SUB, name="Marketing", subdir="marketing")
    db_knowledge_libraries.promote(
        SRC, created_by=ADMIN_SUB, name="Docs", subdir="docs")
    db_knowledge_libraries.attach(
        SRC, CON_A, writable=False, created_by=ADMIN_SUB, subdir="marketing")
    db_knowledge_libraries.attach(
        SRC, CON_B, writable=True, created_by=ADMIN_SUB, subdir="marketing")
    db_knowledge_libraries.attach(
        SRC, CON_B, writable=False, created_by=ADMIN_SUB, subdir="docs")
    return temp_db


class TestSubdirValidation:
    def _promote(self, subdir, name="Lib", agent=SRC):
        from api.agents.knowledge_libraries import (
            LibraryToggleRequest, set_knowledge_library,
        )
        return _run(set_knowledge_library(
            agent, LibraryToggleRequest(enabled=True, name=name, subdir=subdir),
            user=_cookie_admin()))

    def test_subdir_promote_ok(self, temp_db):
        _mk_agents()
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "docs").mkdir(parents=True, exist_ok=True)
        r = self._promote("docs/")
        assert r["status"] == "shared" and r["subdir"] == "docs"

    def test_reserved_first_segment_rejected(self, temp_db):
        _mk_agents()
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "memory" / "private").mkdir(parents=True, exist_ok=True)
        for sub in ("memory", "memory/private", "shared", ".credentials"):
            with pytest.raises(HTTPException) as e:
                self._promote(sub)
            assert e.value.status_code == 400

    def test_path_shape_rejected(self, temp_db):
        _mk_agents()
        for sub in ("../escape", "a/../b", "a//b", "/abs", "a\\b", ".hidden"):
            with pytest.raises(HTTPException) as e:
                self._promote(sub)
            assert e.value.status_code == 400

    def test_missing_dir_rejected(self, temp_db):
        _mk_agents()
        with pytest.raises(HTTPException) as e:
            self._promote("does-not-exist")
        assert e.value.status_code == 400

    def test_symlink_root_and_escape_rejected(self, temp_db):
        _mk_agents()
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "real").mkdir(parents=True, exist_ok=True)
        (k / "linked").symlink_to(k / "real")
        with pytest.raises(HTTPException):
            self._promote("linked")
        # A subtree containing a symlink that leaves it is rejected too.
        (k / "real" / "esc.md").symlink_to(k / "private.md") \
            if (k / "private.md").exists() else \
            (k / "real" / "esc.md").symlink_to("/etc/hostname")
        with pytest.raises(HTTPException):
            self._promote("real")

    def test_disjointness_both_directions_and_casefold(self, temp_db):
        _mk_agents()
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "docs" / "inner").mkdir(parents=True, exist_ok=True)
        (k / "Docs2").mkdir(exist_ok=True)
        self._promote("docs")
        # Nested under an existing library → 400 (either direction).
        with pytest.raises(HTTPException):
            self._promote("docs/inner")
        # Root share excludes subdir libraries and vice versa.
        with pytest.raises(HTTPException):
            self._promote("")
        # Case-folded: DOCS collides with docs (case-insensitive satellites).
        (k / "DOCS").mkdir(exist_ok=True) if not (k / "DOCS").exists() else None
        # On a case-insensitive host mkdir may alias; validate via API only.
        with pytest.raises(HTTPException):
            self._promote("DOCS") if (k / "DOCS").is_dir() else self._promote("docs/inner")

    def test_windows_reserved_name_rejected(self, temp_db):
        _mk_agents()
        for bad in ("con", "NUL", "com3", "lpt9", "con.backup"):
            with pytest.raises(HTTPException) as e:
                self._promote("", name=bad)
            assert e.value.status_code == 400

    def test_bad_name_chars_rejected(self, temp_db):
        _mk_agents()
        for bad in ("a/b", "a\\b", 'a"b', "a?b", ".dotted", "trail."):
            with pytest.raises(HTTPException) as e:
                self._promote("", name=bad)
            assert e.value.status_code == 400
        # The endpoint strips outer whitespace before validating; the raw
        # validator still rejects a preserved trailing space.
        from api.agents.knowledge_libraries import validate_library_name
        with pytest.raises(HTTPException):
            validate_library_name("trail ")
        # A name with spaces is fine.
        r = self._promote("", name="Brand Guidelines")
        assert r["status"] == "shared"

    def test_rename_same_subdir_is_not_an_overlap(self, temp_db):
        _mk_agents()
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "docs").mkdir(parents=True, exist_ok=True)
        self._promote("docs", name="First")
        r = self._promote("docs", name="Second")
        assert r["status"] == "shared" and r["created"] is False
        assert db_knowledge_libraries.library_name(SRC, "docs") == "Second"


class TestSubdirLibraries:
    def test_projector_walks_only_the_subtree(self, kl_subdir_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        mir_a = _mirror(CON_A)
        # CON_A has ONLY the marketing library.
        assert (mir_a / "marketing" / "plan.md").read_text() == "plan v1"
        assert not (mir_a / "marketing-extra").exists()
        assert not (mir_a / "docs").exists()
        assert not (mir_a / "private.md").exists()
        # CON_B has marketing + docs, still nothing else.
        mir_b = _mirror(CON_B)
        assert (mir_b / "marketing" / "plan.md").exists()
        assert (mir_b / "docs" / "guide.md").exists()
        assert not (mir_b / "private.md").exists()

    def test_subdir_internal_memory_folder_is_content(self, kl_subdir_env,
                                                      quiet_fanout):
        # A folder named "memory" INSIDE a subdir library is legal content —
        # only the true source-root memory/ is reserved.
        from services.knowledge import library_projector
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "marketing" / "memory").mkdir()
        (k / "marketing" / "memory" / "notes.md").write_text("legit")
        _run(library_projector.reconcile_source(SRC))
        assert (_mirror(CON_B) / "marketing" / "memory" / "notes.md").exists()
        assert not library_projector.is_excluded_rel("marketing/memory/notes.md")
        assert library_projector.is_excluded_rel("memory/topic.md")

    def test_per_library_writable_enforcement(self, kl_subdir_env,
                                              quiet_fanout):
        # Same consumer (CON_B), same source: marketing RW, docs RO.
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        mir_b = _mirror(CON_B)
        (mir_b / "marketing" / "new.md").write_text("authored in mirror")
        assert _run(library_projector.propagate_mirror_write(
            CON_B, SRC, "marketing/new.md")) is True
        assert (config.get_agent_dir(SRC) / "knowledge" / "marketing"
                / "new.md").exists()
        (mir_b / "docs" / "sneak.md").write_text("nope")
        assert _run(library_projector.propagate_mirror_write(
            CON_B, SRC, "docs/sneak.md")) is False

    def test_source_write_projects_only_matching_library(self, kl_subdir_env,
                                                         quiet_fanout):
        from services.knowledge import library_projector
        k = config.get_agent_dir(SRC) / "knowledge"
        (k / "marketing-extra" / "more.md").write_text("still not shared")
        _run(library_projector.propagate_source_write(
            SRC, "marketing-extra/more.md"))
        assert not (_mirror(CON_A) / "marketing-extra").exists()
        (k / "marketing" / "plan.md").write_text("plan v2")
        _run(library_projector.propagate_source_write(SRC, "marketing/plan.md"))
        assert (_mirror(CON_A) / "marketing" / "plan.md").read_text() == "plan v2"

    def test_single_library_teardown_spares_sibling(self, kl_subdir_env,
                                                    quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        mir_b = _mirror(CON_B)
        assert (mir_b / "docs" / "guide.md").exists()
        consumers = db_knowledge_libraries.unpromote(SRC, "docs")
        assert consumers == [CON_B]
        _run(library_projector.teardown_library(SRC, "docs", consumers))
        assert not (mir_b / "docs").exists()
        # The sibling marketing library is untouched.
        assert (mir_b / "marketing" / "plan.md").exists()

    def test_mirror_stray_outside_subtrees_is_swept(self, kl_subdir_env,
                                                    quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        stray = _mirror(CON_A) / "smuggled.md"
        stray.write_text("kernel-bypass write")
        _run(library_projector.reconcile_source(SRC))
        assert not stray.exists()
        entries = recover_bin_store.list_for(CON_A, ADMIN_SUB, True, True, True)
        assert any("smuggled.md" in e["rel_path"] for e in entries)

    def test_segment_boundary_marketing_vs_extra(self, kl_subdir_env):
        # The RO/RW gates must never let a 'marketing' library authorize
        # 'marketing-extra' (str.startswith would).
        from core.remote.file_sync import can_write_back, library_for_mirror_rel
        pairs = frozenset({(SRC, "marketing")})
        inside = f"knowledge/shared/{SRC}/marketing/x.md"
        sibling = f"knowledge/shared/{SRC}/marketing-extra/x.md"
        assert library_for_mirror_rel(inside, pairs) == (SRC, "marketing")
        assert library_for_mirror_rel(sibling, pairs) is None
        assert can_write_back(inside, "manager", "alice",
                              writable_libraries=pairs) is True
        assert can_write_back(sibling, "manager", "alice",
                              writable_libraries=pairs) is False
        # Hook layer, same boundary.
        ctx = _ctx(libs=((SRC, "marketing", True),))
        from auth.path_policy import check_host_path_access
        ok = check_host_path_access(
            (config.AGENTS_DIR / CON_A / inside).resolve(), ctx, writing=True)
        deny = check_host_path_access(
            (config.AGENTS_DIR / CON_A / sibling).resolve(), ctx, writing=True)
        assert ok.allowed is True and deny.allowed is False
        # Files-API layer.
        from api.agents.files import _check_library_mirror_write
        # attached via kl_subdir_env: CON_B RW marketing
        _check_library_mirror_write(
            f"knowledge/shared/{SRC}/marketing/x.md", CON_B)
        with pytest.raises(HTTPException):
            _check_library_mirror_write(
                f"knowledge/shared/{SRC}/marketing-extra/x.md", CON_B)

    def test_sandbox_mounts_per_library_subtree(self, kl_subdir_env):
        # RO marketing for CON_A → nested ro-bind of the SUBTREE only.
        from pathlib import Path
        from core.sandbox.sandbox import SandboxBuilder, SandboxConfig
        cfg = SandboxConfig(
            role="manager", username="alice", agent_name=CON_A,
            is_admin_agent=False,
            host_agents_dir=config.AGENTS_DIR.resolve(),
            host_mcps_dir=config.MCPS_DIR.resolve(),
            host_claude_dir=Path("/tmp/claude-test"),
            knowledge_libraries=[(SRC, "marketing", False)],
        )
        args = SandboxBuilder(cfg)._workspace_mounts()
        pairs = list(zip(args, args[1:], args[2:]))
        mirror_sub = str(config.get_agent_dir(CON_A) / "knowledge" / "shared"
                         / SRC / "marketing")
        assert ("--ro-bind", mirror_sub,
                f"/knowledge/shared/{SRC}/marketing") in pairs

    def test_satellite_merge_per_library_dirs(self, kl_subdir_env):
        # RO docs prefix rides push_only + scrub; RW marketing pulls through
        # can_write_back — per-library, not per-source.
        from core.remote.file_sync import FileEntry, diff_manifests
        ro_prefix = {f"knowledge/shared/{SRC}/docs/"}
        wl = frozenset({(SRC, "marketing")})
        local = [FileEntry(f"knowledge/shared/{SRC}/docs/guide.md",
                           "h-platform", 100.0, 10)]
        remote = [
            {"path": f"knowledge/shared/{SRC}/docs/guide.md",
             "hash": "h-edited", "mtime": 200.0, "size": 12},
            {"path": f"knowledge/shared/{SRC}/marketing/new.md",
             "hash": "h-new", "mtime": 200.0, "size": 5},
        ]
        plan = diff_manifests(
            local, remote, push_only_dirs=set(ro_prefix) | {"config/"},
            scrub_dirs=ro_prefix, target_username="alice",
            target_role="manager", writable_libraries=wl,
        )
        ops = {a.rel_path: a.op for a in plan.actions}
        # RO library divergence → re-push (with conflict capture).
        assert ops[f"knowledge/shared/{SRC}/docs/guide.md"] == "push"
        # RW library satellite-authored file → pull (adopted).
        assert ops[f"knowledge/shared/{SRC}/marketing/new.md"] == "pull"


# ───────────────────────────────────────────────────────────────────────────
# Bulletins — auto-injected shared context; authored at the source and,
# since 1.5, by WRITABLE attachments (RO mirrors stay capture+heal)
# ───────────────────────────────────────────────────────────────────────────


class TestBulletins:
    def _write_bulletin(self, subdir: str, name: str, text: str):
        k = config.get_agent_dir(SRC) / "knowledge"
        d = (k / subdir / "bulletin") if subdir else (k / "bulletin")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text(text)
        return d / f"{name}.md"

    def test_injected_for_consumer_and_source(self, kl_subdir_env):
        self._write_bulletin("marketing", "Marketing", "Campaign freeze Friday.")
        # Consumer of the marketing library sees it…
        section = config._render_library_bulletins(CON_A)
        assert section is not None
        assert "# Library Bulletins" in section
        assert "Marketing" in section and "Campaign freeze Friday." in section
        assert f"from the **{SRC}** agent" in section
        # …and the SOURCE agent itself sees its own library's bulletin.
        own = config._render_library_bulletins(SRC)
        assert own is not None and "this agent's own library" in own

    def test_missing_file_and_unnamed_library_skip(self, kl_env):
        # kl_env's root library has no name and no bulletin file → nothing.
        assert config._render_library_bulletins(CON_A) is None
        # Named but still no file → still nothing.
        db_knowledge_libraries.promote(SRC, created_by=ADMIN_SUB, name="Lib")
        assert config._render_library_bulletins(CON_A) is None

    def test_name_with_space_and_root_library(self, kl_env):
        db_knowledge_libraries.promote(
            SRC, created_by=ADMIN_SUB, name="Brand Guidelines")
        self._write_bulletin("", "Brand Guidelines", "Use the new logo.")
        section = config._render_library_bulletins(CON_B)
        assert section and "Brand Guidelines" in section
        assert "Use the new logo." in section

    def test_utf8_boundary_truncation(self, kl_env):
        db_knowledge_libraries.promote(SRC, created_by=ADMIN_SUB, name="Lib")
        # 'α' is 2 bytes; an odd byte budget forces a split mid-character.
        text = "α" * 3000  # 6000 bytes > 4096, cut lands mid-α
        self._write_bulletin("", "Lib", text)
        section = config._render_library_bulletins(CON_A)
        assert section is not None
        assert "�" not in section          # no mojibake from the cut
        assert "truncated at 4 KB" in section
        body = section.split("## Lib", 1)[1]
        assert body.count("α") == 4096 // 2     # whole characters only

    def test_header_explains_the_mechanism(self, kl_subdir_env):
        """2026-09-01: the injected block must teach agents WHAT a bulletin
        is (auto-loaded runtime broadcast, not memory, 4 KB budget) — the
        old header was a single opaque sentence."""
        self._write_bulletin("marketing", "Marketing", "Note.")
        section = config._render_library_bulletins(CON_A)
        assert "auto-loaded into the context of every agent attached" in section
        assert "NOT a memory" in section
        assert "first 4 KB" in section

    def test_rw_attachment_gets_the_write_path_hint(self, kl_subdir_env):
        self._write_bulletin("marketing", "Marketing", "Note.")
        rw = config._render_library_bulletins(CON_B)   # marketing RW
        assert ("Writable attachment — publish updates at "
                f"`/knowledge/shared/{SRC}/marketing/bulletin/Marketing.md`"
                in rw)
        ro = config._render_library_bulletins(CON_A)   # marketing RO
        assert "Writable attachment" not in ro
        own = config._render_library_bulletins(SRC)
        assert "File: `/knowledge/marketing/bulletin/Marketing.md`" in own

    def test_near_cap_soft_warning(self, kl_env):
        db_knowledge_libraries.promote(SRC, created_by=ADMIN_SUB, name="Lib")
        self._write_bulletin("", "Lib", "x" * 3500)   # >3 KB soft, <4 KB cap
        section = config._render_library_bulletins(CON_A)
        assert "of the 4 KB injected cap" in section
        assert "truncated" not in section
        # Under the soft threshold → no warning line.
        self._write_bulletin("", "Lib", "x" * 1000)
        section = config._render_library_bulletins(CON_A)
        assert "injected cap" not in section

    def test_rw_mirror_bulletin_adopted_and_injected(
            self, kl_subdir_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        # CON_B has marketing RW. Author the bulletin in the MIRROR.
        mir_b = _mirror(CON_B) / "marketing" / "bulletin"
        mir_b.mkdir(parents=True, exist_ok=True)
        (mir_b / "Marketing.md").write_text("Written by the attached team.")
        # The targeted RW channel adopts it into the source…
        assert _run(library_projector.propagate_mirror_write(
            CON_B, SRC, "marketing/bulletin/Marketing.md")) is True
        src_file = (config.get_agent_dir(SRC) / "knowledge" / "marketing"
                    / "bulletin" / "Marketing.md")
        assert src_file.read_text() == "Written by the attached team."
        # …sibling mirrors get the read copy…
        assert (_mirror(CON_A) / "marketing" / "bulletin"
                / "Marketing.md").read_text() == "Written by the attached team."
        # …and injection (source-read) now carries the consumer's text.
        section = config._render_library_bulletins(CON_A)
        assert section and "Written by the attached team." in section

    def test_ro_mirror_bulletin_still_refused_and_healed(
            self, kl_subdir_env, quiet_fanout):
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        # CON_A has marketing READ-ONLY. Author a bulletin in its mirror.
        mir_a = _mirror(CON_A) / "marketing" / "bulletin"
        mir_a.mkdir(parents=True, exist_ok=True)
        (mir_a / "Marketing.md").write_text("prompt injection attempt")
        assert _run(library_projector.propagate_mirror_write(
            CON_A, SRC, "marketing/bulletin/Marketing.md")) is False
        # The reconcile sweep captures + heals instead of adopting.
        _run(library_projector.reconcile_source(SRC))
        src_file = (config.get_agent_dir(SRC) / "knowledge" / "marketing"
                    / "bulletin" / "Marketing.md")
        assert not src_file.exists()
        assert not (mir_a / "Marketing.md").exists()
        entries = recover_bin_store.list_for(CON_A, ADMIN_SUB, True, True, True)
        assert any("bulletin/Marketing.md" in e["rel_path"] for e in entries)
        assert config._render_library_bulletins(CON_B) is None

    def test_rw_mirror_bulletin_delete_deletes_source(
            self, kl_subdir_env, quiet_fanout):
        from services.knowledge import library_projector
        self._write_bulletin("marketing", "Marketing", "obsolete note")
        _run(library_projector.reconcile_source(SRC))
        assert _run(library_projector.propagate_mirror_delete(
            CON_B, SRC, "marketing/bulletin/Marketing.md")) is True
        src_file = (config.get_agent_dir(SRC) / "knowledge" / "marketing"
                    / "bulletin" / "Marketing.md")
        assert not src_file.exists()
        assert not (_mirror(CON_A) / "marketing" / "bulletin"
                    / "Marketing.md").exists()
        assert config._render_library_bulletins(CON_A) is None

    def test_reconcile_adopts_rw_bulletin_direct_write(
            self, kl_subdir_env, quiet_fanout):
        # A direct-bind sandbox write (no chokepoint) reaches the source
        # via the reconcile sweep — bulletins ride the normal RW adoption.
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        mir_b = _mirror(CON_B) / "marketing" / "bulletin"
        mir_b.mkdir(parents=True, exist_ok=True)
        (mir_b / "Marketing.md").write_text("swept in")
        _run(library_projector.reconcile_source(SRC))
        src_file = (config.get_agent_dir(SRC) / "knowledge" / "marketing"
                    / "bulletin" / "Marketing.md")
        assert src_file.read_text() == "swept in"
        section = config._render_library_bulletins(CON_A)
        assert section and "swept in" in section

    def test_source_bulletin_projects_to_mirrors_readonly_copy(
            self, kl_subdir_env, quiet_fanout):
        from services.knowledge import library_projector
        self._write_bulletin("marketing", "Marketing", "Read me.")
        _run(library_projector.reconcile_source(SRC))
        # Mirrors carry a browsable read copy.
        assert (_mirror(CON_A) / "marketing" / "bulletin"
                / "Marketing.md").read_text() == "Read me."

    def _promote(self, name, subdir=""):
        from api.agents.knowledge_libraries import (
            LibraryToggleRequest, set_knowledge_library,
        )
        return _run(set_knowledge_library(
            SRC, LibraryToggleRequest(enabled=True, name=name, subdir=subdir),
            user=_cookie_admin()))

    def test_rename_carries_the_bulletin_file(self, kl_subdir_env):
        old = self._write_bulletin("marketing", "Marketing", "v1")
        r = self._promote("Campaigns", subdir="marketing")
        assert r["status"] == "shared"
        assert r["has_bulletin"] is True
        assert not old.exists()
        new = (config.get_agent_dir(SRC) / "knowledge" / "marketing"
               / "bulletin" / "Campaigns.md")
        assert new.read_text() == "v1"

    def test_rename_refused_when_target_exists(self, kl_subdir_env):
        self._write_bulletin("marketing", "Marketing", "old")
        self._write_bulletin("marketing", "Campaigns", "squatter")
        with pytest.raises(HTTPException) as e:
            self._promote("Campaigns", subdir="marketing")
        assert e.value.status_code == 409
        # Atomic: the label did NOT change either.
        assert db_knowledge_libraries.library_name(
            SRC, "marketing") == "Marketing"

    def test_rename_refused_under_wopi_lock(self, kl_subdir_env):
        from api.media import wopi as _wopi
        self._write_bulletin("marketing", "Marketing", "locked")
        file_id = _wopi.encode_file_id(
            f"{SRC}/knowledge/marketing/bulletin/Marketing.md")
        _wopi._set_lock(file_id, "lock-1")
        try:
            with pytest.raises(HTTPException) as e:
                self._promote("Campaigns", subdir="marketing")
            assert e.value.status_code == 409
            assert db_knowledge_libraries.library_name(
                SRC, "marketing") == "Marketing"
        finally:
            _wopi._remove_lock(file_id, "lock-1")

    def test_rename_without_bulletin_is_clean(self, kl_subdir_env):
        r = self._promote("Campaigns", subdir="marketing")
        assert r["status"] == "shared" and r["has_bulletin"] is False
        assert db_knowledge_libraries.library_name(
            SRC, "marketing") == "Campaigns"

    def test_personal_only_consumer_gets_injection(self, kl_subdir_env):
        db_knowledge_libraries.attach(
            SRC, PERSONAL, writable=False, created_by=ADMIN_SUB,
            subdir="marketing")
        self._write_bulletin("marketing", "Marketing", "For everyone.")
        section = config._render_library_bulletins(PERSONAL)
        assert section and "For everyone." in section

    def test_get_reports_bulletin_presence(self, kl_subdir_env):
        from api.agents.knowledge_libraries import get_knowledge_attachments
        self._write_bulletin("marketing", "Marketing", "here")
        r = _run(get_knowledge_attachments(SRC, user=_cookie_admin()))
        by_sub = {l["subdir"]: l for l in r["libraries"]}
        assert by_sub["marketing"]["has_bulletin"] is True
        assert by_sub["docs"]["has_bulletin"] is False
        rb = _run(get_knowledge_attachments(CON_A, user=_cookie_admin()))
        (att,) = rb["attachments"]
        assert att["has_bulletin"] is True


class TestQuotaAccounting:
    def test_subdir_mirror_bills_the_consumers_shared_bucket(
            self, kl_subdir_env, quiet_fanout):
        # Quota scopes are directory-spanning (shared bucket = workspace +
        # knowledge + config), so mirror content must live inside the
        # CONSUMER's knowledge tree — never the source's — for a subdir
        # library exactly like a whole-folder one.
        from services.infra import storage_quota as sq
        from services.knowledge import library_projector
        _run(library_projector.reconcile_source(SRC))
        mirror_file = _mirror(CON_A) / "marketing" / "plan.md"
        assert mirror_file.is_file()
        consumer_dirs = [p.resolve() for p in sq.shared_scope_dirs(CON_A)]
        assert any(mirror_file.resolve().is_relative_to(d)
                   for d in consumer_dirs)
        source_dirs = [p.resolve() for p in sq.shared_scope_dirs(SRC)]
        assert not any(mirror_file.resolve().is_relative_to(d)
                       for d in source_dirs)
