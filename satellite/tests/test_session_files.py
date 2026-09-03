"""Tests for satellite/sessions/session_files.py — per-session secret files.

Materialize (fetch stubbed), env mapping, traversal guard, wipe semantics.
The fetch itself is a plain POST with the payload's capability token — the
proxy-side endpoint/auth contract is covered in the proxy suite.
"""

import base64
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


def _payload(files):
    return {
        "session_files_token": "tok",
        "env": {"PROXY_URL": "http://127.0.0.1:9"},
        "session_files_env": {"OTO_SSH_KEY_DIR": "ssh"},
        "_files": files,  # consumed by the fetch stub below
    }


@pytest.fixture
def stub_fetch(monkeypatch):
    def _stub(proxy_url, token):
        return _stub.files
    _stub.files = None
    monkeypatch.setattr(session_files, "_fetch", _stub)
    return _stub


def test_materialize_writes_0600_and_maps_env(stub_fetch, temp_secrets_root):
    stub_fetch.files = {
        "ssh/prod_key": {
            "content_b64": base64.b64encode(b"PRIVATE").decode(),
            "mode": 0o600,
        },
    }
    env = session_files.materialize(_payload(stub_fetch.files), "sid-1")

    dest = temp_secrets_root / "sid-1" / "ssh" / "prod_key"
    assert dest.read_bytes() == b"PRIVATE"
    assert dest.stat().st_mode & 0o777 == 0o600
    assert dest.parent.stat().st_mode & 0o777 == 0o700
    assert env == {"OTO_SSH_KEY_DIR": str(temp_secrets_root / "sid-1" / "ssh")}


def test_materialize_refuses_traversal_paths(stub_fetch, temp_secrets_root):
    stub_fetch.files = {
        "../outside": {"content_b64": base64.b64encode(b"X").decode()},
        "ssh/ok": {"content_b64": base64.b64encode(b"OK").decode()},
    }
    env = session_files.materialize(_payload(stub_fetch.files), "sid-2")

    assert not (temp_secrets_root.parent / "outside").exists()
    assert (temp_secrets_root / "sid-2" / "ssh" / "ok").exists()
    assert env  # the legit file still landed


def test_materialize_noop_without_token(stub_fetch):
    assert session_files.materialize({"env": {}}, "sid-3") == {}


def test_materialize_failed_fetch_is_empty(stub_fetch, temp_secrets_root):
    stub_fetch.files = None  # fetch error path
    assert session_files.materialize(_payload(None), "sid-4") == {}
    assert not (temp_secrets_root / "sid-4").exists()


def test_wipe_and_wipe_all(stub_fetch, temp_secrets_root):
    stub_fetch.files = {
        "ssh/k": {"content_b64": base64.b64encode(b"K").decode()},
    }
    session_files.materialize(_payload(stub_fetch.files), "sid-5")
    session_files.materialize(_payload(stub_fetch.files), "sid-6")

    session_files.wipe("sid-5")
    assert not (temp_secrets_root / "sid-5").exists()
    assert (temp_secrets_root / "sid-6").exists()

    session_files.wipe_all()
    assert not temp_secrets_root.exists()


# ── agent-tree targets (OAuth credentials_dir token files) ─────────────────

_TOKEN = base64.b64encode(b'{"access_token":"x"}').decode()
_VPATH = "/users/alice/.credentials/google-tokens/alice@gmail.com.json"


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agents" / "agentx"
    (d / "users" / "alice" / "workspace").mkdir(parents=True)
    return d


def _cred_payload(files):
    p = _payload(files)
    p["cwd_relative"] = "users/alice"
    return p


def test_virtual_path_lands_in_agent_tree(stub_fetch, temp_secrets_root, agent_dir):
    stub_fetch.files = {_VPATH: {"content_b64": _TOKEN}}
    env = session_files.materialize(
        _cred_payload(stub_fetch.files), "sid-v1", agent_dir,
    )

    dest = (
        agent_dir / "users" / "alice" / ".credentials" / "google-tokens"
        / "alice@gmail.com.json"
    )
    assert dest.read_bytes() == b'{"access_token":"x"}'
    assert dest.stat().st_mode & 0o777 == 0o600
    assert dest.parent.stat().st_mode & 0o777 == 0o700
    # ssh env var still maps (blindly, from session_files_env) but no agent-
    # tree path leaks into env — the MCP's own env already carries it.
    assert list(env) == ["OTO_SSH_KEY_DIR"]
    # The write is recorded for wipe().
    assert session_files._agent_tree_files(
        temp_secrets_root / "sid-v1"
    ) == [str(dest)]


def test_virtual_path_outside_agent_tree_refused(stub_fetch, temp_secrets_root, agent_dir, tmp_path):
    stub_fetch.files = {
        # Unknown prefix → translate_path passes it through → outside root.
        "/etc/evil": {"content_b64": _TOKEN},
        # Known prefix + traversal out of the agent dir.
        "/users/../../evil.json": {"content_b64": _TOKEN},
        "ssh/ok": {"content_b64": base64.b64encode(b"OK").decode()},
    }
    session_files.materialize(_cred_payload(stub_fetch.files), "sid-v2", agent_dir)

    assert not (tmp_path / "evil.json").exists()
    assert not (tmp_path / "agents" / "evil.json").exists()
    assert (temp_secrets_root / "sid-v2" / "ssh" / "ok").exists()
    assert session_files._agent_tree_files(temp_secrets_root / "sid-v2") == []


def test_virtual_path_skipped_without_agent_dir(stub_fetch, temp_secrets_root):
    stub_fetch.files = {_VPATH: {"content_b64": _TOKEN}}
    assert session_files.materialize(
        _cred_payload(stub_fetch.files), "sid-v3",
    ) == {}


def test_stale_sibling_token_files_removed(stub_fetch, temp_secrets_root, agent_dir):
    stale_dir = agent_dir / "users" / "alice" / ".credentials" / "google-tokens"
    stale_dir.mkdir(parents=True)
    (stale_dir / "old-account@gmail.com.json").write_text("{}")

    stub_fetch.files = {_VPATH: {"content_b64": _TOKEN}}
    session_files.materialize(_cred_payload(stub_fetch.files), "sid-v4", agent_dir)

    # Single-user-mode MCPs pick the first file in the dir — only the
    # session's bound account's token may remain.
    assert not (stale_dir / "old-account@gmail.com.json").exists()
    assert (stale_dir / "alice@gmail.com.json").exists()


def test_wipe_removes_agent_tree_files_and_prunes_dirs(stub_fetch, temp_secrets_root, agent_dir):
    stub_fetch.files = {_VPATH: {"content_b64": _TOKEN}}
    session_files.materialize(_cred_payload(stub_fetch.files), "sid-v5", agent_dir)

    session_files.wipe("sid-v5")
    creds_root = agent_dir / "users" / "alice" / ".credentials"
    assert not creds_root.exists()  # file removed + empty dirs pruned
    assert (agent_dir / "users" / "alice").is_dir()  # tree above untouched
    assert not (temp_secrets_root / "sid-v5").exists()


def test_wipe_refcounts_shared_target_across_sessions(stub_fetch, temp_secrets_root, agent_dir):
    stub_fetch.files = {_VPATH: {"content_b64": _TOKEN}}
    session_files.materialize(_cred_payload(stub_fetch.files), "sid-a", agent_dir)
    session_files.materialize(_cred_payload(stub_fetch.files), "sid-b", agent_dir)

    dest = (
        agent_dir / "users" / "alice" / ".credentials" / "google-tokens"
        / "alice@gmail.com.json"
    )
    session_files.wipe("sid-a")
    assert dest.exists()  # sid-b still references it
    session_files.wipe("sid-b")
    assert not dest.exists()


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
# Interactive (PTY) sessions run the same materialize/wipe lifecycle
# ---------------------------------------------------------------------------

def _pty_base(tmp_path):
    from satellite.terminal.pty_session_base import BasePtySession
    return BasePtySession(
        "pty-sess", tmp_path / "agents" / "demo", _payload(None),
        object(), object(),
    )


def test_pty_base_materializes_session_files(stub_fetch, temp_secrets_root, tmp_path):
    # Regression: interactive sessions skipped the pre-spawn session-files
    # step the headless paths run — a PTY session's bash had no ssh-hosts
    # keys (no OTO_SSH_KEY_DIR) and credentials_dir MCPs had no token files.
    import asyncio

    stub_fetch.files = {
        "ssh/id_ed25519": {"content_b64": base64.b64encode(b"KEY").decode()},
    }
    env = asyncio.run(_pty_base(tmp_path)._materialize_session_files())
    key = Path(env["OTO_SSH_KEY_DIR"]) / "id_ed25519"
    assert key.read_bytes() == b"KEY"


def test_pty_base_close_wipes_session_files(stub_fetch, temp_secrets_root, tmp_path):
    import asyncio

    stub_fetch.files = {
        "ssh/id_ed25519": {"content_b64": base64.b64encode(b"KEY").decode()},
    }
    sess = _pty_base(tmp_path)
    env = asyncio.run(sess._materialize_session_files())
    secrets_dir = Path(env["OTO_SSH_KEY_DIR"]).parent
    assert secrets_dir.is_dir()
    asyncio.run(sess.close())
    assert not secrets_dir.exists()
