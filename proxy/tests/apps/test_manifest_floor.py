"""The signed manifest and the per-action role floor (APPS.md "The
signed manifest", "min_role").

Load-bearing: an approval recorded before the other manifest blocks
existed keeps matching (the legacy actions hash is the degenerate case of
the whole-manifest signature), a block voids it, and a floored action is
judged on the caller's real role with every bearer principal clamped to
viewer.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from api.apps import apps as apps_api
from api.apps import manifest as _mf
from app import app
from auth.providers import UserContext, get_current_user
from storage import database as task_store
from ws import artifact_interactions as _ai

client = TestClient(app)

AGENT = "floor-agent"
OWNER = "floor-owner"
EDITOR = "floor-editor"
VIEWER = "floor-viewer"


def _user(sub: str = OWNER, role: str = "member", agent_roles: dict[str, str] | None = None,
          is_api_key: bool = False, agents: tuple[str, ...] = (AGENT,)) -> UserContext:
    # A bearer here is a session token, which always names its agent.
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role,
                       agents=list(agents),
                       agent_roles={AGENT: "manager"} if agent_roles is None else agent_roles,
                       is_api_key=is_api_key, agent=AGENT if is_api_key else "")


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _people():
    for sub, name in ((OWNER, "Owner"), (EDITOR, "Editor"), (VIEWER, "Viewer")):
        task_store.upsert_user(sub, f"{sub}@test.com", name, "member")
    task_store.add_user_agent(OWNER, AGENT, "manager", "test")
    task_store.add_user_agent(EDITOR, AGENT, "editor", "test")
    task_store.add_user_agent(VIEWER, AGENT, "viewer", "test")
    apps_api._fire_rate.clear()
    _as(_user())
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _prompt(aid: str = "go", **extra) -> dict:
    return {"id": aid, "label": aid.title(), "type": "send_prompt", "prompt": "hi", **extra}


def _shared(actions: list, slug: str = "team") -> dict:
    canonical, err = _mf.validate_actions(actions, AGENT, True)
    assert err == "", err
    return task_store.upsert_app(AGENT, "", None, slug, title=slug.title(),
                                 rel_path=f"workspace/apps/{slug}.html", actions_json=canonical)


def _approve(row: dict, by: str = OWNER) -> None:
    assert task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), by)


# ───────────────────────── the signature ────────────────────────────────────


def test_legacy_actions_hash_is_the_signature_while_no_other_block_exists():
    row = _shared([_prompt()])
    legacy = task_store.actions_sig(row["actions"])
    assert task_store.canonical_manifest(row) == row["actions"]
    assert task_store.manifest_sig(row) == legacy
    # An approval stored under the old hash (every existing install) holds.
    assert task_store.approve_app_actions(row["id"], legacy, OWNER)
    assert task_store.app_actions_approved(task_store.get_app(row["id"]))
    shaped = apps_api.shape_app_rows([task_store.get_app(row["id"])], _user())[0]
    assert shaped["actions_sig"] == legacy and shaped["actions_approved"] is True


def test_a_non_empty_block_switches_to_the_object_form_and_voids_the_approval():
    row = _shared([_prompt()])
    _approve(row)
    approved = task_store.get_app(row["id"])
    assert task_store.app_actions_approved(approved)
    with_block = {**approved, "catalog": json.dumps([{"id": "sessions"}])}
    text = task_store.canonical_manifest(with_block)
    assert json.loads(text) == {"actions": json.loads(row["actions"]),
                                "catalog": [{"id": "sessions"}]}
    assert task_store.manifest_sig(with_block) != task_store.manifest_sig(approved)
    assert task_store.app_actions_approved(with_block) is False
    # Empty blocks are the legacy form; nothing at all is nothing to approve.
    assert task_store.canonical_manifest({**approved, "files": "{}", "egress": []}) == row["actions"]
    assert task_store.app_actions_approved({"actions": "[]", "catalog": None}) is True
    assert task_store.app_actions_approved({"actions": "[]", "catalog": "[1]"}) is False


# ───────────────────────── min_role in the manifest ─────────────────────────


def test_min_role_normalises_and_validates():
    canonical, err = _mf.validate_actions(
        [_prompt("a", min_role="viewer"), _prompt("b", min_role="editor"),
         _prompt("c", min_role="manager"), _prompt("d")], AGENT, True)
    assert err == ""
    parsed = {a["id"]: a for a in json.loads(canonical)}
    assert "min_role" not in parsed["a"] and "min_role" not in parsed["d"]
    assert parsed["b"]["min_role"] == "editor" and parsed["c"]["min_role"] == "manager"
    # Naming the default floor signs like omitting it.
    plain, _ = _mf.validate_actions([_prompt("a"), _prompt("d")], AGENT, True)
    named, _ = _mf.validate_actions([_prompt("a", min_role="viewer"), _prompt("d")], AGENT, True)
    assert plain == named
    _, err = _mf.validate_actions([_prompt("x", min_role="owner")], AGENT, True)
    assert "min_role" in err


def test_caller_role_matrix():
    shared = {"agent": AGENT, "username": "", "owner_sub": None}
    personal = {"agent": AGENT, "username": "owner", "owner_sub": OWNER}
    assert _mf.caller_role(shared, _user(EDITOR, agent_roles={AGENT: "editor"})) == "editor"
    assert _mf.caller_role(shared, _user(VIEWER, agent_roles={AGENT: "viewer"})) == "viewer"
    assert _mf.caller_role(shared, _user("root", role="admin")) == "admin"
    # A grantee or a non-member of the agent is a viewer of a shared app.
    assert _mf.caller_role(shared, _user("someone", agents=(), agent_roles={})) == "viewer"
    # The owner of a personal app is its manager, whatever their agent role;
    # anyone else is a viewer.
    assert _mf.caller_role(personal, _user(OWNER, agent_roles={AGENT: "viewer"})) == "manager"
    assert _mf.caller_role(personal, _user(EDITOR, agent_roles={AGENT: "editor"})) == "viewer"
    # Every bearer principal clamps to viewer, its owner's role notwithstanding.
    assert _mf.caller_role(shared, _user(OWNER, is_api_key=True)) == "viewer"
    assert _mf.caller_role(shared, _user("api-key", role="admin", is_api_key=True)) == "viewer"
    assert _mf.caller_role(shared, None) == "viewer"
    assert _mf.meets_floor({"min_role": "editor"}, "manager")
    assert not _mf.meets_floor({"min_role": "manager"}, "editor")
    assert _mf.meets_floor({}, "viewer")


# ───────────────────────── the floor at execution ───────────────────────────


def _mk_task(created_by: str = OWNER) -> str:
    tid = str(uuid.uuid4())
    task_store.create_dynamic_task(
        tid, AGENT, "task-trigger", "do the thing", "auto", "trigger", None,
        None, None, 300, created_by, scope="agent", notification_mode="none",
    )
    return tid


def test_rest_actions_refuse_below_the_floor_and_the_shape_says_the_role():
    tid = _mk_task()
    row = _shared([
        {"id": "run", "label": "Run", "type": "fire_task", "task_id": tid, "min_role": "editor"},
        {"id": "boss", "label": "Boss", "type": "fire_task", "task_id": tid, "min_role": "manager"},
    ])
    _approve(row)
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    listed = client.get(f"/v1/apps/{row['id']}").json()
    assert listed["viewer_role"] == "viewer"
    assert [a.get("min_role") for a in listed["actions"]] == ["editor", "manager"]
    r = client.post(f"/v1/apps/{row['id']}/actions/run")
    assert r.status_code == 403 and "editor role" in r.json()["detail"]
    # The batch answers per entry (one task run per batch, so one entry).
    lines = client.post(f"/v1/apps/{row['id']}/actions/batch", json={"calls": [
        {"call_id": "1", "action_id": "run"},
    ]}).text.strip().splitlines()
    results = {json.loads(line)["call_id"]: json.loads(line) for line in lines}
    assert results["1"]["status"] == "denied" and results["1"]["code"] == 403
    assert "editor role" in results["1"]["reason"]
    # An editor clears the editor floor, not the manager one.
    _as(_user(EDITOR, agent_roles={AGENT: "editor"}))
    assert client.get(f"/v1/apps/{row['id']}").json()["viewer_role"] == "editor"
    assert client.post(f"/v1/apps/{row['id']}/actions/boss").status_code == 403
    # The owner's own session token is a bearer: clamped to viewer.
    _as(_user(OWNER, is_api_key=True))
    assert client.post(f"/v1/apps/{row['id']}/actions/run").status_code == 403


def test_send_prompt_floor_on_the_chat_rail():
    row = _shared([_prompt("ask", min_role="editor")])
    _approve(row)
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, VIEWER, AGENT)
    interaction, err = _ai.validate_app_action(chat_id, AGENT, VIEWER, row["id"], "ask", None)
    assert interaction is None and "editor role" in err
    interaction, err = _ai.validate_app_action(chat_id, AGENT, EDITOR, row["id"], "ask", None)
    assert interaction is not None and err == ""


def test_contributor_floor_and_the_files_write_default():
    """``contributor`` is a floor an author may name, and a platform write
    carries it by default: writing the shared workspace is the workspace
    tier, and the file API still judges the path by the caller's own role."""
    canonical, err = _mf.validate_actions(
        [_prompt("c", min_role="contributor"),
         {"id": "w", "label": "Write", "type": "platform", "method": "files.write"}],
        AGENT, True)
    assert err == "", err
    parsed = {a["id"]: a for a in json.loads(canonical)}
    assert parsed["c"]["min_role"] == "contributor"
    assert parsed["w"]["min_role"] == "contributor"
    assert _mf.meets_floor({"min_role": "contributor"}, "contributor")
    assert _mf.meets_floor({"min_role": "contributor"}, "editor")
    assert not _mf.meets_floor({"min_role": "contributor"}, "viewer")
    assert not _mf.meets_floor({"min_role": "editor"}, "contributor")
