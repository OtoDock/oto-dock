"""What a session on a compute machine on another host receives from the
platform's preparation (``core/remote/compute_files.py``): the session's
config folder as the platform wrote it, less the runtime trees, the SSH keys
and another session's own files, plus its prompt file, keyed by the path below
the agent's
folder for its start frame, and bounded."""

from __future__ import annotations

import base64
import hashlib

import pytest

from core.remote import compute_files

SID = "0123456789abcdef-session"


def _prompt(sid: str) -> str:
    return f"system-prompt-{hashlib.sha256(sid.encode()).hexdigest()[:12]}.json"


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "agents" / "personal-assistant"
    claude = root / "users" / "alice" / ".claude"
    (claude / "bin").mkdir(parents=True)
    for name in ("settings.json", "permission_gate.py", "credential_helper.py", "stop_tracker.py",
                 ".credentials.json", ".claude.json", _prompt(SID), _prompt("another"),
                 f"personal-assistant-{'a' * 12}-{SID[:12]}.json",
                 f"personal-assistant-{'a' * 12}-{'f' * 12}.json", "mcp-config.json"):
        (claude / name).write_text(name)
    (claude / "bin" / "git").write_text("#!/bin/sh\n")
    (claude / "bin" / "git").chmod(0o755)
    for runtime in ("projects/h/s.jsonl", "ssh/id_ed25519", "tasks/x/1.json", "plugins/p/x"):
        (claude / runtime).parent.mkdir(parents=True, exist_ok=True)
        (claude / runtime).write_text("x")
    (claude / "evil").symlink_to(tmp_path)
    codex = root / "users" / "alice" / ".codex"
    (codex / "sessions").mkdir(parents=True)
    for name in ("config.toml", "AGENTS.md", "hooks.json", "auth.json", "permission_gate.py",
                 "state_5.sqlite", "state_5.sqlite-wal", "models_cache.json", "session_index.jsonl",
                 "installation_id", "history.jsonl"):
        (codex / name).write_text(name)
    (codex / "sessions" / "t.jsonl").write_text("x")
    for kept in ("skills/s/SKILL.md", "thread-writer-locks/l", "shell_snapshots/x.sh"):
        (codex / kept).parent.mkdir(parents=True, exist_ok=True)
        (codex / kept).write_text("x")
    return root


def test_a_claude_session_gets_its_prepared_files_and_nothing_else(agent):
    claude = agent / "users" / "alice" / ".claude"
    files = compute_files.collect(agent, str(claude), str(claude / _prompt(SID)), SID)
    keys = set(files)
    base = "users/alice/.claude/"
    for name in ("settings.json", "permission_gate.py", "credential_helper.py", "stop_tracker.py",
                 ".credentials.json", ".claude.json", "bin/git", _prompt(SID),
                 f"personal-assistant-{'a' * 12}-{SID[:12]}.json"):
        assert base + name in keys, name
    # Transcripts, the task store, plugins and the SSH keys never go; nor
    # another session's prompt or tool config, nor an earlier shared copy.
    for name in ("projects/h/s.jsonl", "ssh/id_ed25519", "tasks/x/1.json", "plugins/p/x",
                 _prompt("another"), f"personal-assistant-{'a' * 12}-{'f' * 12}.json", "mcp-config.json"):
        assert base + name not in keys, name
    assert not any("evil" in k for k in keys)
    assert base64.b64decode(files[base + "settings.json"]["content_b64"]) == b"settings.json"
    assert files[base + "bin/git"]["mode"] == 0o755


def test_a_codex_session_gets_its_home_without_its_threads(agent):
    codex = agent / "users" / "alice" / ".codex"
    keys = set(compute_files.collect(agent, str(codex), "", SID))
    base = "users/alice/.codex/"
    assert {base + n for n in ("config.toml", "AGENTS.md", "hooks.json", "auth.json", "permission_gate.py")} <= keys
    # The engine's own state stays the machine's (its databases, caches,
    # history, index), and the person's folders arrive through the sync.
    for name in ("sessions/t.jsonl", "state_5.sqlite", "state_5.sqlite-wal", "models_cache.json",
                 "session_index.jsonl", "installation_id", "history.jsonl", "skills/s/SKILL.md",
                 "thread-writer-locks/l", "shell_snapshots/x.sh"):
        assert base + name not in keys, name


def test_nothing_outside_the_agents_folder_goes(agent, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "settings.json").write_text("x")
    assert compute_files.collect(agent, str(outside), str(outside / "settings.json"), SID) == {}


def test_a_set_over_the_bounds_refuses_the_start(agent, monkeypatch):
    claude = agent / "users" / "alice" / ".claude"
    monkeypatch.setattr(compute_files, "MAX_FILES", 3)
    with pytest.raises(compute_files.TooMuchToSend):
        compute_files.collect(agent, str(claude), "", SID)


def _bind(rel: str, root: str = "agents") -> dict:
    return {"root": root, "rel": rel, "sandbox": "/x", "rw": False}


def test_what_the_sandbox_binds_below_the_agents_folder_goes_too(agent, tmp_path):
    # A Claude session's description binds the other engine's gate script and
    # hidden folders the sync never carries: the shim refuses a bind whose
    # source is missing, so the files go and the folders are named.
    claude = agent / "users" / "alice" / ".claude"
    (agent / "users" / "alice" / ".agents").mkdir()
    (agent / "config" / ".git").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    sandbox = {"binds": [_bind("users/alice/.codex/permission_gate.py"), _bind("users/alice/.agents"),
                         _bind("users/alice/.claude/ssh/id_ed25519"), _bind("../../outside.txt"),
                         _bind("/etc/hostname", root="abs"), _bind("")],
               "extra_binds": [_bind("config/.git")]}
    files = compute_files.collect(agent, str(claude), "", SID, sandbox)
    assert "users/alice/.codex/permission_gate.py" in files
    # The SSH keys never go, nor anything outside the agent's folder.
    assert not any("ssh/" in k or "outside" in k or "hostname" in k for k in files)
    assert compute_files.folders(agent, sandbox) == ["config/.git", "users/alice/.agents"]


def test_a_codex_session_gets_its_own_files_never_anothers(agent):
    """A session's profile and text travel to an own-copy machine; another
    session's never do, nor a thread's record (the machine keeps its own)."""
    codex = agent / "users" / "alice" / ".codex"
    for name in (f"{SID}.config.toml", f"{SID}.session.json", "other-sid.config.toml",
                 "other-sid.session.json", "thread-1.text-sha256"):
        (codex / name).write_text(name)
    keys = set(compute_files.collect(agent, str(codex), "", SID))
    base = "users/alice/.codex/"
    assert {base + f"{SID}.config.toml", base + f"{SID}.session.json"} <= keys
    for name in ("other-sid.config.toml", "other-sid.session.json", "thread-1.text-sha256"):
        assert base + name not in keys, name


def _bobs_folder_behind_a_link(agent):
    """Bob's engine folder, and a link in Alice's to it (a stand-in)."""
    bob = agent / "users" / "bob" / ".claude"
    bob.mkdir(parents=True)
    for name in ("settings.json", _prompt(SID)):
        (bob / name).write_text("bob's")
    (agent / "users" / "alice" / ".claude" / "peek").symlink_to(bob)
    return agent / "users" / "alice" / ".claude" / "peek"


def test_a_link_on_the_way_sends_nothing_from_where_it_leads(agent):
    """Every file is named and read where the plan wrote it, never where a
    link leads: a prompt file or a config folder reached through one sends
    nothing, under any name."""
    peek = _bobs_folder_behind_a_link(agent)
    claude = agent / "users" / "alice" / ".claude"
    files = compute_files.collect(agent, str(claude), str(peek / _prompt(SID)), SID)
    assert not any("bob" in k for k in files)
    assert not any(base64.b64decode(v["content_b64"]) == b"bob's" for v in files.values())
    assert compute_files.collect(agent, str(peek), "", SID) == {}
    sandbox = {"binds": [_bind("users/alice/.claude/peek/settings.json"), _bind("users/alice/.claude/peek")]}
    assert not any("bob" in k or "peek" in k for k in compute_files.collect(agent, str(claude), "", SID, sandbox))
    assert compute_files.folders(agent, sandbox) == []


def test_a_file_past_what_the_frame_has_left_is_refused_unread(agent, monkeypatch):
    claude = agent / "users" / "alice" / ".claude"
    (claude / "settings.json").write_bytes(b"x" * 4096)
    monkeypatch.setattr(compute_files, "MAX_BYTES", 1024)
    read: list = []
    real = compute_files.safe_fs.iter_fd

    def _counted(fd, start=0, length=None, **kw):
        for chunk in real(fd, start, length, **kw):
            read.append(len(chunk))
            yield chunk
    monkeypatch.setattr(compute_files.safe_fs, "iter_fd", _counted)
    with pytest.raises(compute_files.TooMuchToSend):
        compute_files.collect(agent, str(claude), "", SID)
    assert read and max(read) <= 1025


def test_an_agent_folder_moved_behind_a_link_still_sends_its_files(agent, tmp_path):
    """An operator who moved the agent's folder behind a link keeps its
    compute starts: the folder is taken by its realpath, links below it
    are still never followed."""
    moved = tmp_path / "agents" / "moved-assistant"
    moved.symlink_to(agent)
    engine = moved / "users" / "alice" / ".claude"
    files = compute_files.collect(moved, str(engine), str(engine / _prompt(SID)), SID)
    base = "users/alice/.claude/"
    assert {base + "settings.json", base + _prompt(SID)} <= set(files)
    assert not any("evil" in k for k in files)


HOME_SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


@pytest.mark.parametrize("engine, sign_in", [(".claude", ".credentials.json"), (".codex", "auth.json")])
def test_a_home_sessions_frame_carries_no_stores_sign_in(agent, engine, sign_in):
    """A session in a home reads its home's sign-in: the store's (an earlier
    version's) never rides its frame, so a machine with its own copy never
    writes it under another session's mask. A session with no home still
    gets the store's."""
    store = agent / "users" / "alice" / engine
    home = store / "own" / HOME_SID
    home.mkdir(parents=True)
    (home / sign_in).write_text("the session's own")
    base = f"users/alice/{engine}/"
    keys = set(compute_files.collect(agent, str(store), "", HOME_SID))
    assert base + sign_in not in keys
    assert f"{base}own/{HOME_SID}/{sign_in}" in keys
    assert base + sign_in in set(compute_files.collect(agent, str(store), "", SID))

