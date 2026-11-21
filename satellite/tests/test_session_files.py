"""Tests for satellite/sessions/session_files.py: a session's own folders on
this machine, their wipe at the close and at the start, and the purge of the
agent tree's credential folders.
"""

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from satellite.sessions import session_files


@pytest.fixture(autouse=True)
def temp_secrets_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_files, "_secrets_root", lambda: tmp_path / "session-secrets",
    )
    yield tmp_path / "session-secrets"


def test_wipe_and_wipe_all(temp_secrets_root):
    for sid in ("sid-5", "sid-6"):
        (temp_secrets_root / sid / "ssh").mkdir(parents=True)
        (temp_secrets_root / sid / "ssh" / "k.pub").write_text("ssh-ed25519 AAAA k\n")

    session_files.wipe("sid-5")
    assert not (temp_secrets_root / "sid-5").exists()
    assert (temp_secrets_root / "sid-6").exists()

    session_files.wipe_all()
    assert not temp_secrets_root.exists()


def test_purge_agent_tree_credentials(tmp_path):
    agents = tmp_path / "agents"
    user_creds = agents / "a1" / "users" / "alice" / ".credentials" / "google-tokens"
    user_creds.mkdir(parents=True)
    (user_creds / "t.json").write_text("{}")
    svc_creds = agents / "a2" / "knowledge" / ".credentials"
    svc_creds.mkdir(parents=True)
    (svc_creds / "s.json").write_text("{}")
    keep = agents / "a1" / "users" / "alice" / "workspace"
    keep.mkdir(parents=True)
    (keep / "doc.md").write_text("hi")

    session_files.purge_agent_tree_credentials(agents)

    assert not (agents / "a1" / "users" / "alice" / ".credentials").exists()
    assert not (agents / "a2" / "knowledge" / ".credentials").exists()
    assert (keep / "doc.md").exists()
    # Tolerates absence.
    session_files.purge_agent_tree_credentials(tmp_path / "nope")
    session_files.purge_agent_tree_credentials(None)


# ---------------------------------------------------------------------------
# Interactive (PTY) sessions wipe the same folder at their close
# ---------------------------------------------------------------------------

def _pty_base(tmp_path):
    from satellite.terminal.pty_session_base import BasePtySession
    return BasePtySession("pty-sess", tmp_path / "agents" / "demo", {"env": {}}, object(), object())


def test_pty_base_close_wipes_the_sessions_folder(temp_secrets_root, tmp_path):
    import asyncio

    sess = _pty_base(tmp_path)
    folder = temp_secrets_root / "pty-sess" / "ssh"
    folder.mkdir(parents=True)
    (folder / "id_ed25519.pub").write_text("ssh-ed25519 AAAA id\n")
    asyncio.run(sess.close())
    assert not (temp_secrets_root / "pty-sess").exists()


def test_the_credentials_purge_never_removes_through_a_persons_link(tmp_path):
    """A person's own link at their folder: the purge leaves what lies
    behind it (it removes only a folder reached with no link)."""
    outside = tmp_path / "elsewhere" / "alice"
    (outside / ".credentials").mkdir(parents=True)
    (outside / ".credentials" / "t.json").write_text("{}")
    agents = tmp_path / "agents"
    (agents / "a1" / "users").mkdir(parents=True)
    (agents / "a1" / "users" / "alice").symlink_to(outside)
    session_files.purge_agent_tree_credentials(agents)
    assert (outside / ".credentials" / "t.json").exists()
