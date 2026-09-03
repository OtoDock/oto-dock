"""POST /v1/chats/{chat_id}/resolve-path — the clickable file-chip resolver.

Covers the audited path-shape matrix (sandbox-virtual, platform-host,
satellite linux/windows/tilde folds, relative candidate order), confinement
(backslash-smuggled ``..``, other-agent absolute), the no-oracle contract
(role/OAuth denials, directories, deleted agents → 200 ``{found: false}``),
the chat 404/403 prologue, and the ``previewable`` flag matrix.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.providers import UserContext, get_current_user
from storage import agent_store, remote_store
from storage import database as task_store
from storage.pg import get_conn

client = TestClient(app)

AGENT = "agent-files"
OTHER_AGENT = "agent-other"


def _user(sub="user-alice", role="member", agents=(AGENT,), agent_role="editor"):
    return UserContext(
        sub=sub, email=f"{sub}@test.com", name=sub, role=role,
        agents=list(agents), agent_roles={a: agent_role for a in agents},
    )


@pytest.fixture
def _as():
    """Authenticate the TestClient as a given UserContext."""
    def setup(user: UserContext):
        app.dependency_overrides[get_current_user] = lambda: user
    yield setup
    app.dependency_overrides.pop(get_current_user, None)


def _ensure_user(sub: str, username: str = "") -> None:
    """Insert/refresh a users row with a username slug (conftest seeds only
    the four default subs, none with a username)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login, username) "
            "VALUES (%s, %s, %s, 'member', %s, %s, %s) "
            "ON CONFLICT (sub) DO UPDATE SET username = EXCLUDED.username",
            (sub, f"{sub}@test.com", sub, now, now, username),
        )
        conn.commit()


def _mk_agent(slug: str = AGENT):
    """Real agent row + directory tree (conftest points AGENTS_DIR at a
    per-run temp root and wipes it between tests)."""
    agent_store.create_agent(slug, slug)
    d = config.AGENTS_DIR / slug
    (d / "workspace").mkdir(parents=True, exist_ok=True)
    return d


def _mk_file(agent_dir, rel: str, content: str = "hello") -> None:
    p = agent_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _mk_chat(owner: str = "user-alice", agent: str = AGENT,
             execution_target: str = "") -> str:
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, owner, agent)
    if execution_target:
        task_store.update_chat(cid, execution_target=execution_target)
    return cid


def _mk_machine(machine_id: str, *, agents_dir: str, home_dir: str = "",
                os_name: str = "linux") -> str:
    remote_store.create_remote_machine(machine_id, f"name-{machine_id}", "user-admin")
    remote_store.update_machine_capabilities(machine_id, {
        "agents_dir": agents_dir, "home_dir": home_dir, "os": os_name,
    })
    return machine_id


def _resolve(cid: str, path: str):
    return client.post(f"/v1/chats/{cid}/resolve-path", json={"path": path})


# ---------------------------------------------------------------------------
# Path-shape matrix
# ---------------------------------------------------------------------------

def test_sandbox_virtual_hit(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "workspace/report.md", "hi")
    cid = _mk_chat()
    _as(_user())
    body = _resolve(cid, "/workspace/report.md").json()
    assert body["found"] is True
    assert body["agent"] == AGENT
    assert body["path"] == "workspace/report.md"
    assert body["filename"] == "report.md"
    assert body["size"] == 2
    assert body["previewable"] is False  # .md is not a Collabora doc type


def test_platform_host_absolute_hit(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "workspace/report.md")
    cid = _mk_chat()
    _as(_user())
    host = str(config.AGENTS_DIR / AGENT / "workspace" / "report.md")
    body = _resolve(cid, host).json()
    assert body["found"] is True
    assert body["path"] == "workspace/report.md"


def test_other_agent_absolute_no_match_even_for_admin(temp_db, _as):
    _mk_agent()
    other = _mk_agent(OTHER_AGENT)
    _mk_file(other, "workspace/secret.md")
    cid = _mk_chat(owner="user-admin")
    _as(_user(sub="user-admin", role="admin", agents=(AGENT, OTHER_AGENT)))
    host = str(config.AGENTS_DIR / OTHER_AGENT / "workspace" / "secret.md")
    resp = _resolve(cid, host)
    assert resp.status_code == 200
    assert resp.json() == {"found": False}


def test_backslash_smuggled_dotdot_is_found_false_not_4xx(temp_db, _as):
    # ``workspace\..\config\x`` — segment checks on the raw string miss the
    # backslash-carried ``..``; normalize-first makes it visible. The target
    # EXISTS and the caller is a manager (config/ readable), so only the
    # ``..`` reject can produce found:false here.
    d = _mk_agent()
    _mk_file(d, "config/x", "cfg")
    cid = _mk_chat()
    _as(_user(agent_role="manager"))
    _ensure_user("user-alice", "alice")
    resp = _resolve(cid, "workspace\\..\\config\\x")
    assert resp.status_code == 200
    assert resp.json() == {"found": False}


# ---------------------------------------------------------------------------
# Satellite-prefix fold
# ---------------------------------------------------------------------------

def test_satellite_linux_prefix_fold(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "workspace/report.md")
    m = _mk_machine("m-linux", agents_dir="/home/bob/.oto-dock/agents")
    cid = _mk_chat(execution_target=m)
    _as(_user())
    body = _resolve(
        cid, f"/home/bob/.oto-dock/agents/{AGENT}/workspace/report.md").json()
    assert body["found"] is True
    assert body["path"] == "workspace/report.md"


def test_satellite_windows_prefix_fold_case_insensitive(temp_db, _as):
    # Capability value is raw backslashed with an uppercase drive; the input
    # differs in prefix CASE (windows compare is case-insensitive) while the
    # agent-tree remainder stays case-exact.
    d = _mk_agent()
    _mk_file(d, "workspace/report.md")
    m = _mk_machine("m-win", agents_dir="C:\\Users\\Bob\\.oto-dock\\agents",
                    os_name="windows")
    cid = _mk_chat(execution_target=m)
    _as(_user())
    body = _resolve(
        cid, f"C:\\USERS\\BOB\\.oto-dock\\agents\\{AGENT}\\workspace\\report.md",
    ).json()
    assert body["found"] is True
    assert body["path"] == "workspace/report.md"


def test_satellite_tilde_fold(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "workspace/report.md")
    m = _mk_machine("m-tilde", agents_dir="/home/bob/.oto-dock/agents",
                    home_dir="/home/bob")
    cid = _mk_chat(execution_target=m)
    _as(_user())
    body = _resolve(cid, f"~/.oto-dock/agents/{AGENT}/workspace/report.md").json()
    assert body["found"] is True
    assert body["path"] == "workspace/report.md"


def test_satellite_empty_agents_dir_skips_rule(temp_db, _as):
    # Legacy satellites report agents_dir="" — an empty prefix would
    # over-match ANY /{agent}/… path; the rule must be skipped entirely.
    d = _mk_agent()
    _mk_file(d, "workspace/report.md")
    m = _mk_machine("m-legacy", agents_dir="", home_dir="/home/bob")
    cid = _mk_chat(execution_target=m)
    _as(_user())
    resp = _resolve(cid, f"/{AGENT}/workspace/report.md")
    assert resp.status_code == 200
    assert resp.json() == {"found": False}


# ---------------------------------------------------------------------------
# Relative candidates
# ---------------------------------------------------------------------------

def test_relative_candidate_order_workspace_first(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "workspace/dup.md", "shared")
    _mk_file(d, "users/alice/workspace/dup.md", "personal")
    _ensure_user("user-alice", "alice")
    cid = _mk_chat()
    _as(_user())
    body = _resolve(cid, "dup.md").json()
    assert body["found"] is True
    assert body["path"] == "workspace/dup.md"


def test_relative_falls_back_to_user_workspace(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "users/alice/workspace/mine.docx")
    _ensure_user("user-alice", "alice")
    cid = _mk_chat()
    _as(_user())
    body = _resolve(cid, "mine.docx").json()
    assert body["found"] is True
    assert body["path"] == "users/alice/workspace/mine.docx"
    assert body["previewable"] is True  # users/ first segment + doc extension


def test_relative_user_candidate_skipped_when_username_empty(temp_db, _as):
    # user-bob has no username slug → the users/{username}/workspace
    # candidate is skipped (never a users//workspace probe).
    d = _mk_agent()
    _mk_file(d, "users/alice/workspace/mine.md")
    cid = _mk_chat(owner="user-bob")
    _as(_user(sub="user-bob"))
    resp = _resolve(cid, "mine.md")
    assert resp.status_code == 200
    assert resp.json() == {"found": False}


# ---------------------------------------------------------------------------
# No-oracle contract: denials and non-files are 200 {found: false}
# ---------------------------------------------------------------------------

def test_oauth_protected_path_found_false(temp_db, _as, monkeypatch):
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_protected_credentials_subpaths",
                        lambda: frozenset({"google-tokens"}))
    d = _mk_agent()
    _mk_file(d, "workspace/google-tokens/token.json", "{}")
    cid = _mk_chat()
    _as(_user())
    resp = _resolve(cid, "workspace/google-tokens/token.json")
    assert resp.status_code == 200
    assert resp.json() == {"found": False}


def test_other_users_dir_denied_for_viewer(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "users/bob/workspace/secret.md")
    _ensure_user("user-alice", "alice")
    cid = _mk_chat()
    _as(_user(agent_role="viewer"))
    resp = _resolve(cid, "users/bob/workspace/secret.md")
    assert resp.status_code == 200
    assert resp.json() == {"found": False}


def test_directory_found_false(temp_db, _as):
    d = _mk_agent()
    (d / "workspace" / "sub").mkdir(parents=True)
    cid = _mk_chat()
    _as(_user())
    assert _resolve(cid, "workspace/sub").json() == {"found": False}


def test_deleted_agent_found_false(temp_db, _as):
    # Chat row survives its agent: _get_agent_dir's 404 must not escape.
    cid = _mk_chat(agent="ghost-agent")
    _as(_user(agents=("ghost-agent",)))
    resp = _resolve(cid, "workspace/report.md")
    assert resp.status_code == 200
    assert resp.json() == {"found": False}


# ---------------------------------------------------------------------------
# Chat prologue — the only status-code escapes
# ---------------------------------------------------------------------------

def test_chat_access_denied_403(temp_db, _as):
    _mk_agent()
    cid = _mk_chat(owner="user-bob")  # someone else's personal chat
    _as(_user(sub="user-alice"))
    assert _resolve(cid, "workspace/report.md").status_code == 403


def test_chat_missing_404(temp_db, _as):
    _as(_user())
    assert _resolve("no-such-chat", "workspace/report.md").status_code == 404


# ---------------------------------------------------------------------------
# previewable flag matrix
# ---------------------------------------------------------------------------

def test_previewable_true_for_workspace_docx(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "workspace/report.docx")
    cid = _mk_chat()
    _as(_user())
    body = _resolve(cid, "workspace/report.docx").json()
    assert body["found"] is True
    assert body["previewable"] is True


def test_previewable_false_for_knowledge_docx(temp_db, _as):
    # Readable (knowledge/ is in every role's read scope) but outside the
    # wopi-url confinement (workspace/ + users/ only) → not previewable.
    d = _mk_agent()
    _mk_file(d, "knowledge/report.docx")
    cid = _mk_chat()
    _as(_user())
    body = _resolve(cid, "knowledge/report.docx").json()
    assert body["found"] is True
    assert body["path"] == "knowledge/report.docx"
    assert body["previewable"] is False


def test_previewable_false_for_workspace_png(temp_db, _as):
    d = _mk_agent()
    _mk_file(d, "workspace/pic.png")
    cid = _mk_chat()
    _as(_user())
    body = _resolve(cid, "workspace/pic.png").json()
    assert body["found"] is True
    assert body["previewable"] is False
