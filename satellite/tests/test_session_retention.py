"""``host/session_retention``: the platform's keep list applied to this
machine's transcripts (0.5.150)."""

import os
import time
import uuid
from pathlib import Path

from satellite.host import session_retention as sr


def _claude(home: Path, sid: str, *, sibling: bool = False, project: str = "-users-alice") -> Path:
    d = home / ".claude" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_bytes(b"x" * 100)
    if sibling:
        (d / sid / "tool-results").mkdir(parents=True)
        (d / sid / "tool-results" / "r.txt").write_bytes(b"y" * 10)
    return f


def _rollout(home: Path, tid: str) -> Path:
    d = home / ".codex" / "sessions" / "2026" / "01" / "01"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-2026-01-01T00-00-00-{tid}.jsonl"
    f.write_bytes(b"z" * 50)
    return f


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _uuid() -> str:
    return str(uuid.uuid4())


def test_the_walk_names_each_transcripts_id_under_every_home(tmp_path):
    agents = tmp_path / "agents"
    a, b, c = _uuid(), _uuid(), _uuid()
    fa = _claude(agents / "a1" / "users" / "alice", a, sibling=True)
    fb = _rollout(agents / "a1" / "workspace", b)
    fc = _claude(agents / "a2" / "workspace", c)
    (agents / "a1" / "users" / "alice" / ".claude" / "projects" / "p" ).mkdir(parents=True)
    (agents / "a1" / "users" / "alice" / "notes.jsonl").write_bytes(b"not a transcript")
    found = {ref: (f, sib) for f, ref, sib in sr.iter_session_files(agents)}
    assert set(found) == {a, b, c}
    assert found[a] == (fa, fa.with_suffix(""))
    assert found[b] == (fb, None) and found[c] == (fc, None)


def test_kept_live_young_and_odd_ids_stay_and_the_rest_go(tmp_path):
    agents = tmp_path / "agents"
    home = agents / "a1" / "users" / "alice"
    kept, live, young, gone, gone_tid = (_uuid() for _ in range(5))
    files = {
        "kept": _claude(home, kept), "live": _claude(home, live),
        "young": _claude(home, young), "gone": _claude(home, gone, sibling=True, project="-p2"),
        "odd": _claude(home, "not-a-uuid"), "gone_tid": _rollout(home, gone_tid),
    }
    for name, f in files.items():
        if name != "young":
            _age(f, 10 * 86400)
    stats = sr.apply(agents, {kept}, {live}, 7 * 86400, False)
    assert stats == {
        "files": 4, "bytes": 400, "deleted": 2, "deleted_bytes": 150,
        "candidates": [files["gone"].relative_to(agents).as_posix(),
                       files["gone_tid"].relative_to(agents).as_posix()],
    }
    for name in ("kept", "live", "young", "odd"):
        assert files[name].exists(), name
    assert not files["gone"].exists() and not files["gone"].with_suffix("").exists()
    assert not files["gone_tid"].exists()
    # The emptied project folder is pruned; one still holding files stays.
    assert not (home / ".claude" / "projects" / "-p2").exists()
    assert (home / ".claude" / "projects" / "-users-alice").is_dir()


def test_a_dry_run_counts_what_would_go_removes_nothing_and_caps_the_list(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "CANDIDATES_MAX", 2)
    agents = tmp_path / "agents"
    home = agents / "a1" / "workspace"
    files = [_claude(home, _uuid()) for _ in range(3)]
    for f in files:
        _age(f, 10 * 86400)
    stats = sr.apply(agents, set(), set(), 7 * 86400, True)
    # The would-go count is the dry run's answer (the card's "N session
    # files on M machines" reads it); what stays is what is held.
    assert stats["files"] == 0 and stats["bytes"] == 0
    assert stats["deleted"] == 3 and stats["deleted_bytes"] == 300
    assert len(stats["candidates"]) == 2
    assert all(f.exists() for f in files)
    # A clock the caller supplies decides the age.
    stats = sr.apply(agents, set(), set(), 7 * 86400, False, now=time.time() - 5 * 86400)
    assert stats["deleted"] == 0 and all(f.exists() for f in files)


def test_nothing_on_the_way_is_followed_and_nothing_is_deleted_through_a_link(tmp_path):
    """A linked project folder, a linked ``.codex/sessions`` and a linked
    user home point outside the agents folder: nothing under them is listed
    or deleted, and a transcript whose own name is a link is left alone;
    the real transcripts beside them go as before."""
    agents = tmp_path / "agents"
    outside = tmp_path / "outside"
    home = agents / "a1" / "users" / "alice"
    real_gone = _claude(home, _uuid(), sibling=True)
    _age(real_gone, 10 * 86400)
    # A project folder that is a link to a personal history elsewhere.
    personal = outside / "personal-project"
    personal.mkdir(parents=True)
    linked_file = personal / f"{_uuid()}.jsonl"
    linked_file.write_bytes(b"p" * 30)
    _age(linked_file, 10 * 86400)
    (home / ".claude" / "projects" / "-linked").symlink_to(personal, target_is_directory=True)
    # A sessions folder that is a link.
    rollouts = outside / "rollouts" / "2026" / "01" / "01"
    rollouts.mkdir(parents=True)
    linked_rollout = rollouts / f"rollout-2026-01-01T00-00-00-{_uuid()}.jsonl"
    linked_rollout.write_bytes(b"r" * 20)
    _age(linked_rollout, 10 * 86400)
    (home / ".codex").mkdir()
    (home / ".codex" / "sessions").symlink_to(outside / "rollouts", target_is_directory=True)
    # A user home that is a link, and a transcript that is itself a link.
    bob = outside / "bob-home"
    bob_file = _claude(bob, _uuid())
    _age(bob_file, 10 * 86400)
    (agents / "a1" / "users" / "bob").symlink_to(bob, target_is_directory=True)
    direct = outside / f"{_uuid()}.jsonl"
    direct.write_bytes(b"d" * 10)
    _age(direct, 10 * 86400)
    (home / ".claude" / "projects" / "-users-alice" / f"{_uuid()}.jsonl").symlink_to(direct)
    # A sibling folder that is a link beside a real transcript.
    sib_target = outside / "sibling-target"
    (sib_target / "tool-results").mkdir(parents=True)
    linked_sibling_owner = _claude(home, _uuid(), project="-p3")
    _age(linked_sibling_owner, 10 * 86400)
    linked_sibling_owner.with_suffix("").symlink_to(sib_target, target_is_directory=True)

    listed = {f for f, _ref, _sib in sr.iter_session_files(agents)}
    assert listed == {real_gone, linked_sibling_owner}
    stats = sr.apply(agents, set(), set(), 7 * 86400, False)
    assert stats["deleted"] == 2 and stats["files"] == 0
    assert not real_gone.exists() and not real_gone.with_suffix("").exists()
    assert not linked_sibling_owner.exists()
    for survivor in (linked_file, linked_rollout, bob_file, direct, sib_target / "tool-results"):
        assert survivor.exists(), survivor


def test_an_empty_or_missing_agents_folder_is_nothing(tmp_path):
    assert sr.apply(tmp_path / "absent", set(), set(), 3600, False) == {
        "files": 0, "bytes": 0, "deleted": 0, "deleted_bytes": 0, "candidates": []}
    (tmp_path / "agents" / "a1").mkdir(parents=True)
    assert sr.apply(tmp_path / "agents", set(), set(), 3600, False)["files"] == 0
