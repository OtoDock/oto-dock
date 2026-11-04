"""The team actions of the cards catalog (services/cards/actions.py holds
the shape; api/cards/cards.py registers these): hand files to the team,
promote a memory, share an app with another agent, attach a library,
update or merge an app that follows its source, send its release back to
the source. Each calls the route's own function or the service under it
with the clicker's own principal, so the click takes the route's role rule
and its sentences; the draw-time check spells the same rule for the agent,
on the session's bearer.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import config
from api.agent_data import memory as memory_api
from api.agents import knowledge_libraries as libraries_api
from api.agents.files import (
    CopyPathsRequest,
    MovePathsRequest,
    copy_agent_paths,
    move_agent_paths,
)
from auth import roles
from auth.providers import acting_role_of
from core import layout
from core.session import visibility
from services.apps import app_follow
from services.cards import status as st
from services.cards.actions import (
    SLUG,
    Action,
    Outcome,
    display,
    plain,
    quoted,
    sentence,
)
from services.infra import path_confinement
from services.knowledge import library_rules
from storage import database as task_store, db_apps
from storage.agents import agent_store, memory_store
from storage.knowledge import db_knowledge_libraries

AGENT_FIELD = {"type": "string", "maxLength": 40}
# A path inside the person's workspace, as the agent names it.
REL_PATH = {"type": "string", "minLength": 1, "maxLength": 400}
APP_SLUG = {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,62}$"}
SCOPE_WORD = {"type": "string", "enum": [visibility.SCOPE_USER, visibility.SCOPE_AGENT]}

NOT_A_PERSON = "Only a person signed in can press this card."
NO_SUCH_AGENT = "There is no agent named {agent}."
NO_PERSONAL_SPACE = "{agent} has no personal space to hand files from."
NO_SHARED_SPACE = "{agent} has no shared space to hand files to."
HAND_NEEDS_CONTRIBUTOR = "Only contributors, editors and managers hand files to the team."
NO_SUCH_FILE = "There is no file named {path} in your space."
BAD_PATH = "The path {path} is not inside your space."
BAD_DEST = "The destination must be inside the team's workspace folder."
LINKED_PATH = "That path is a link. Name the files themselves."
NAMED_TWICE = "{path} is named twice."
INSIDE_ANOTHER = "{inner} is inside {outer}. Name one of them."
NO_SUCH_APP = "There is no app named {slug} on {agent}."
APP_TAKEN = "An app named {slug} is already on {agent}."
SAME_AGENT = "The source and the target must be different agents."
NO_SHARED_APPS = "{agent} has no shared apps."
NO_PERSONAL_APPS = "{agent} has no personal apps."
SHARE_NEEDS_EDITOR = "Only editors and managers share the team's apps of {agent}."
ADD_NEEDS_EDITOR = "Only editors and managers add shared apps to {agent}."
ATTACH_NEEDS_CREATOR = "Only admins and creators attach libraries."
NOT_A_LIBRARY = "{agent} has no shared knowledge library there."
NO_MEMORY_SCOPE = "{agent} keeps no {scope} memory, so nothing can be shared."
NO_SUCH_TOPIC = "There is no memory topic named {topic} in your private memory."
SHARED_NAME_TAKEN = "The shared memory already has a topic named {topic}."
ALREADY_ATTACHED = "{target} already reads {lib}."


def _agent_of(params: dict[str, Any]) -> str:
    return str(params.get("_agent") or "")


async def _username(u) -> str:
    if u.acting_sub is None:
        return ""
    return await asyncio.to_thread(task_store.get_username_by_sub, u.acting_sub) or ""


def _scopes_of(agent: str) -> tuple[str, ...]:
    row = agent_store.get_agent(agent) or {}
    return visibility.available_scopes_for(bool(row.get("collaborative", True)),
                                           str(row.get("default_scope") or visibility.SCOPE_USER))


def _joined(words: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


# ---------------------------------------------------------------------------
# hand_files
# ---------------------------------------------------------------------------

def _plain_parts(rel: str) -> list[str] | None:
    """The segments of a path that stays inside its root and hides nothing
    (``path_confinement`` refuses the climb, a dot segment is refused here),
    or None."""
    parts = [seg for seg in rel.split("/") if seg not in ("", ".")]
    if not parts or any(seg.startswith(".") for seg in parts):
        return None
    try:
        path_confinement.normalize_rel_path("/".join(parts))
    except path_confinement.PathOutsideRoot:
        return None
    return parts


def _rel_in_space(raw: str, username: str) -> str | None:
    """A path as the agent named it, relative to the person's workspace:
    the ``users/<u>/workspace/`` form and a leading slash are accepted, a
    segment that climbs or hides is refused (None)."""
    rel = str(raw or "").strip().lstrip("/")
    prefix = layout.scope_workspace(username) + "/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    parts = _plain_parts(rel)
    return "/".join(parts) if parts else None


def _dest_rel(raw: str) -> str | None:
    parts = _plain_parts(str(raw or layout.WORKSPACE).strip().strip("/"))
    if not parts or parts[0] != layout.WORKSPACE:
        return None
    return "/".join(parts)


def _stays_put(agent_dir: Path, root_rel: str, rel: str) -> bool:
    """Whether ``rel`` (agent-relative) is what it names: no segment from
    the agent folder down is a link, and its real path sits under the real
    path of ``root_rel``. The files route resolves links, and the agent's
    own session can plant one, so a card's path is checked here, at the
    draw and again at the click. A segment that does not exist ends the
    walk (the route refuses a missing path itself)."""
    current = agent_dir
    for seg in rel.split("/"):
        current = current / seg
        try:
            if os.path.islink(current):
                return False
            os.lstat(current)
        except FileNotFoundError:
            break
        except OSError:
            return False
    root = os.path.realpath(agent_dir / root_rel)
    real = os.path.realpath(agent_dir / rel)
    return real == root or real.startswith(root + os.sep)


def _where(dest: str) -> str:
    """The destination in words: the team's workspace or a folder in it."""
    if dest == layout.WORKSPACE:
        return "the team's workspace"
    return f"the team's {quoted(dest.split('/', 1)[1])} folder"


def _items(agent_dir: Path, space: str, rels: list[str]) -> list[tuple[str, bool]]:
    """Each path's last name and whether it is a folder."""
    return [(rel.rsplit("/", 1)[-1], (agent_dir / space / rel).is_dir()) for rel in rels]


def _count(n: int, one: str, many: str) -> str:
    return f"{'a' if n == 1 else n} {one if n == 1 else many}"


async def _hand_words(params: dict[str, Any], u) -> dict[str, str]:
    agent = _agent_of(params)
    username = await _username(u)
    rels = [r for r in (_rel_in_space(str(p), username) for p in params.get("paths") or []) if r]
    items = await asyncio.to_thread(_items, config.get_agent_dir(agent), layout.scope_workspace(username), rels)
    named = [f"the folder {quoted(name)}" if folder else quoted(name) for name, folder in items]
    dest = _dest_rel(str(params.get("dest_dir") or "")) or layout.WORKSPACE
    folders = sum(1 for _name, folder in items if folder)
    files = len(items) - folders
    if len(items) == 1:
        title = f"Hand {named[0]} to the team"
    else:
        what = [w for w in (_count(files, "file", "files") if files else "",
                            _count(folders, "folder", "folders") if folders else "") if w]
        title = f"Hand {_joined(what)} to the team"
    verb = "Moves" if not params.get("copy") else "Copies"
    text = f"{verb} {_joined(named)} from your space to {_where(dest)}, where everyone on the agent reads them."
    return {"title": title, "text": text, "label": "Hand over"}


async def _hand_check(params: dict[str, Any], u) -> str | None:
    if u.acting_sub is None:
        return NOT_A_PERSON
    agent = _agent_of(params)
    if not agent_store.get_agent(agent):
        return NO_SUCH_AGENT.format(agent=agent)
    scopes = _scopes_of(agent)
    if visibility.SCOPE_USER not in scopes:
        return NO_PERSONAL_SPACE.format(agent=display(agent))
    if visibility.SCOPE_AGENT not in scopes:
        return NO_SHARED_SPACE.format(agent=display(agent))
    if not u.is_admin and not roles.can_write_workspace(u.effective_role(agent)):
        return HAND_NEEDS_CONTRIBUTOR
    username = await _username(u)
    if not username:
        return NOT_A_PERSON
    dest = _dest_rel(str(params.get("dest_dir") or ""))
    if dest is None:
        return BAD_DEST
    agent_dir = config.get_agent_dir(agent)
    if not await asyncio.to_thread(_stays_put, agent_dir, layout.WORKSPACE, dest):
        return LINKED_PATH
    space = layout.scope_workspace(username)
    seen: list[str] = []
    for raw in params.get("paths") or []:
        rel = _rel_in_space(str(raw), username)
        if rel is None:
            return BAD_PATH.format(path=quoted(str(raw)))
        if rel in seen:
            return NAMED_TWICE.format(path=quoted(rel))
        for other in seen:
            if rel.startswith(other + "/") or other.startswith(rel + "/"):
                inner, outer = (rel, other) if rel.startswith(other + "/") else (other, rel)
                return INSIDE_ANOTHER.format(inner=quoted(inner), outer=quoted(outer))
        seen.append(rel)
        if not await asyncio.to_thread(_stays_put, agent_dir, space, f"{space}/{rel}"):
            return LINKED_PATH
        if not await asyncio.to_thread(Path(agent_dir / space / rel).exists):
            return NO_SUCH_FILE.format(path=quoted(rel))
    return None


def _hand_options(params: dict[str, Any], u) -> list[dict[str, Any]]:
    return [{"id": "copy", "label": "Keep my copy", "default": bool(params.get("copy"))}]


async def _hand_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    refusal = await _hand_check(params, u)
    if refusal:
        return Outcome(st.OUTCOME_FAILED, refusal)
    agent = _agent_of(params)
    username = await _username(u)
    src = [f"{layout.scope_workspace(username)}/{_rel_in_space(str(p), username)}" for p in params["paths"]]
    dest = _dest_rel(str(params.get("dest_dir") or "")) or layout.WORKSPACE
    if options.get("copy"):
        result = await copy_agent_paths(agent, CopyPathsRequest(src_paths=src, dest_dir=dest), user=u)
    else:
        result = await move_agent_paths(agent, MovePathsRequest(src_paths=src, dest_dir=dest), user=u)
    done = list(result.get("moved") or result.get("copied") or [])
    failed = result.get("failed") or []
    lines = []
    if done:
        n = len(done)
        noun = ("copy" if n == 1 else "copies") if options.get("copy") else ("file" if n == 1 else "files")
        lines.append(f"{n} {noun} {'is' if n == 1 else 'are'} in {_where(dest)}.")
    for f in failed:
        name = quoted(str(f.get("src") or "").rsplit("/", 1)[-1])
        lines.append(sentence(f"{name} was not handed over: {f.get('reason')}"))
    status = st.OUTCOME_DONE if done and not failed else st.OUTCOME_FAILED
    return Outcome(status, " ".join(lines) or "Nothing was handed over.")


HAND_FILES = Action(
    name="hand_files",
    schema={"type": "object", "required": ["paths"], "additionalProperties": False,
            "properties": {"paths": {"type": "array", "minItems": 1, "maxItems": 20, "items": REL_PATH},
                           "dest_dir": {"type": "string", "maxLength": 400},
                           "copy": {"type": "boolean"}, "_agent": AGENT_FIELD}},
    words=_hand_words, check=_hand_check, run=_hand_run, options=_hand_options,
    personal=lambda _p: "files",
)


# ---------------------------------------------------------------------------
# promote_memory
# ---------------------------------------------------------------------------

def _topic_rel(raw: str) -> str:
    from services.memory import memory_file
    text = str(raw or "").strip()
    scope, rel = memory_file.split_virtual_path(text) if text.startswith("/") else (visibility.SCOPE_USER, text)
    if scope != visibility.SCOPE_USER:
        raise memory_file.MemoryOpError(memory_api._NOT_PERSONAL_PATH)
    return memory_file.validate_rel(rel, mutating=True, require_ext=True)


def _shared_rel(raw: str, rel: str) -> str:
    from services.memory import memory_file
    text = str(raw or "").strip()
    if not text:
        return rel
    if text.startswith("/"):
        scope, new_rel = memory_file.split_virtual_path(text)
        if scope != visibility.SCOPE_AGENT:
            raise memory_file.MemoryOpError(memory_api._NOT_SHARED_PATH)
        text = new_rel
    return memory_file.validate_rel(text, mutating=True, require_ext=True)


def _promotion_refusal(agent: str, username: str, rel: str, dst_rel: str) -> str | None:
    """``promote()``'s own refusals, run at the draw so a card never
    fails only at the click: both memory scopes offered by the agent's mode
    and turned on, the topic there, the shared name free."""
    from services.memory import memory_file, memory_promote
    row = agent_store.get_agent(agent) or {}
    modes = visibility.available_scopes_for(bool(row.get("collaborative", True)),
                                            str(row.get("default_scope") or visibility.SCOPE_USER))
    settings = memory_store.get_settings()
    toggles = memory_store.get_agent_toggles(agent)
    for scope, words in ((visibility.SCOPE_USER, "personal"), (visibility.SCOPE_AGENT, "shared")):
        if scope not in modes or not memory_promote._enabled(scope, settings, toggles):
            return NO_MEMORY_SCOPE.format(agent=display(agent), scope=words)
    agent_dir = config.get_agent_dir(agent)
    if not (memory_file.scope_root(agent_dir, visibility.SCOPE_USER, username) / rel).is_file():
        return NO_SUCH_TOPIC.format(topic=quoted(rel))
    if (memory_file.scope_root(agent_dir, visibility.SCOPE_AGENT) / dst_rel).exists():
        return SHARED_NAME_TAKEN.format(topic=quoted(dst_rel))
    return None


def _shown_topic(raw: str) -> str:
    """The topic as the card names it: the scope's prefix off, the words
    left as the agent wrote them when they do not parse."""
    from services.memory import memory_file
    try:
        return _topic_rel(raw)
    except memory_file.MemoryOpError:
        return str(raw or "").strip()


async def _promote_words(params: dict[str, Any], u) -> dict[str, str]:
    topic = quoted(_shown_topic(str(params.get("topic") or "")))
    new_name = str(params.get("new_name") or "")
    text = (f"Moves the topic {topic} from your private memory to the shared memory of "
            f"{display(_agent_of(params))}, where every user of the agent reads it.")
    if new_name:
        text += f" Its shared name will be {quoted(new_name)}."
    return {"title": f"Share the memory topic {topic} with the team", "text": text, "label": "Share"}


async def _promote_check(params: dict[str, Any], u) -> str | None:
    from services.memory import memory_file
    if u.acting_sub is None:
        return NOT_A_PERSON
    agent = _agent_of(params)
    if not agent_store.get_agent(agent):
        return memory_api._AGENT_GONE
    if not u.is_admin and not u.can_access_agent(agent):
        return NO_SUCH_AGENT.format(agent=agent)
    role = await asyncio.to_thread(acting_role_of, u.acting_sub, agent)
    if not roles.can_edit(role):
        return memory_api._NOT_YOUR_TIER
    username = await _username(u)
    if not username:
        return NOT_A_PERSON
    try:
        rel = _topic_rel(str(params.get("topic") or ""))
        dst_rel = _shared_rel(str(params.get("new_name") or ""), rel)
    except memory_file.MemoryOpError as exc:
        return plain(str(exc))
    return await asyncio.to_thread(_promotion_refusal, agent, username, rel, dst_rel)


async def _promote_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    from services.memory import memory_file, memory_promote
    refusal = await _promote_check(params, u)
    if refusal:
        return Outcome(st.OUTCOME_FAILED, refusal)
    agent = _agent_of(params)
    username = await _username(u)
    role = await asyncio.to_thread(acting_role_of, u.acting_sub, agent)
    rel = _topic_rel(str(params.get("topic") or ""))
    dst_rel = _shared_rel(str(params.get("new_name") or ""), rel)
    settings = await asyncio.to_thread(memory_store.get_settings)
    try:
        await memory_promote.promote(agent, username, rel, role=role, user_sub=u.acting_sub or "",
                                     dst_rel=dst_rel, caps=memory_file.Caps.from_settings(settings))
    except memory_promote.MemoryRoleError:
        return Outcome(st.OUTCOME_FAILED, memory_api._NOT_YOUR_TIER)
    except memory_file.MemoryOpError as exc:
        return Outcome(st.OUTCOME_FAILED, plain(str(exc)))
    return Outcome(st.OUTCOME_DONE, f"The topic {quoted(dst_rel)} is now shared with everyone on {display(agent)}.")


PROMOTE_MEMORY = Action(
    name="promote_memory",
    schema={"type": "object", "required": ["topic"], "additionalProperties": False,
            "properties": {"topic": REL_PATH, "new_name": {"type": "string", "maxLength": 400},
                           "_agent": AGENT_FIELD}},
    words=_promote_words, check=_promote_check, run=_promote_run,
    personal=lambda _p: "memory",
)


# ---------------------------------------------------------------------------
# share_app
# ---------------------------------------------------------------------------

async def _share_sides(params: dict[str, Any], u) -> tuple[str, str, str, str, str]:
    """The source agent, its scope's username, the target agent, its
    scope's username and the slug, as the import's arguments."""
    source = _agent_of(params)
    target = str(params.get("target_agent") or "")
    username = await _username(u)
    src_user = username if str(params.get("source_visibility") or visibility.SCOPE_AGENT) == visibility.SCOPE_USER else ""
    dst_user = username if str(params.get("visibility") or visibility.SCOPE_AGENT) == visibility.SCOPE_USER else ""
    return source, src_user, target, dst_user, str(params.get("app_slug") or "")


def _copy_waits(row: dict) -> bool:
    """Whether a copy of ``row`` on another agent parks for approval: a
    manifest to approve (a new row holds no approval), the per-app switch,
    or a required secret, since secrets never travel with a copy."""
    from services.apps import app_secrets
    if not row:
        return True
    if db_apps.canonical_manifest(row) != "[]" or row.get("deploy_requires_approval"):
        return True
    return any(s.get("required") for s in app_secrets.declared(row))


async def _share_words(params: dict[str, Any], u) -> dict[str, str]:
    source, src_user, target, dst_user, slug = await _share_sides(params, u)
    row = await asyncio.to_thread(task_store.get_app_by_slug, source, src_user, slug) or {}
    title = str(row.get("title") or slug)
    where = "shared apps" if not dst_user else "personal apps"
    then = ("Its first release waits for approval there." if await asyncio.to_thread(_copy_waits, row)
            else "It goes live there once its checks pass.")
    return {"title": f"Share the app {quoted(title)} with {display(target)}",
            "text": (f"Copies the app {quoted(slug)} from {display(source)} to the {where} of {display(target)}. "
                     f"{then} The app's data stays here."),
            "label": "Share"}


async def _share_check(params: dict[str, Any], u) -> str | None:
    if u.acting_sub is None:
        return NOT_A_PERSON
    source, src_user, target, dst_user, slug = await _share_sides(params, u)
    for agent in (source, target):
        if not agent_store.get_agent(agent):
            return NO_SUCH_AGENT.format(agent=agent)
        if not u.is_admin and not u.can_access_agent(agent):
            return NO_SUCH_AGENT.format(agent=agent)
    if source == target:
        return SAME_AGENT
    if not src_user and not u.is_admin and not u.can_edit_agent(source):
        return SHARE_NEEDS_EDITOR.format(agent=display(source))
    row = await asyncio.to_thread(task_store.get_app_by_slug, source, src_user, slug)
    if not row or not db_apps.app_kind_of(row).serves_tree:
        return NO_SUCH_APP.format(slug=quoted(slug), agent=display(source))
    scopes = _scopes_of(target)
    if dst_user and visibility.SCOPE_USER not in scopes:
        return NO_PERSONAL_APPS.format(agent=display(target))
    if not dst_user and visibility.SCOPE_AGENT not in scopes:
        return NO_SHARED_APPS.format(agent=display(target))
    if not dst_user and not u.is_admin and not u.can_edit_agent(target):
        return ADD_NEEDS_EDITOR.format(agent=display(target))
    taken = await asyncio.to_thread(task_store.get_app_by_slug, target, dst_user, slug)
    folder = config.get_agent_dir(target) / layout.scope_workspace(dst_user) / "apps" / slug if dst_user \
        else config.get_agent_dir(target) / layout.WORKSPACE / "apps" / slug
    folder_there = await asyncio.to_thread(folder.exists)
    if taken or folder_there:
        return APP_TAKEN.format(slug=quoted(slug), agent=display(target))
    return None


def _bundle_tasks(agent: str, slug: str) -> dict[str, str]:
    """The agent's tasks a copy of ``slug`` seeded, id to name."""
    mark = f"bundle:{slug}"
    return {t["id"]: str(t.get("name") or t["id"]) for t in task_store.list_dynamic_tasks(agent=agent)
            if t.get("community_template") == mark}


async def _undo_copy(target: str, dst_user: str, slug: str, tasks_before: dict[str, str]) -> list[str]:
    """Remove what a refused copy left on the target: its row with its
    folder, else the folder alone, and the tasks it seeded. Answers what
    could not be removed, in words."""
    from services.apps import app_lifecycle
    left: list[str] = []
    row = await asyncio.to_thread(task_store.get_app_by_slug, target, dst_user, slug)
    rel_dir = f"{layout.scope_workspace(dst_user)}/apps/{slug}"
    agent_dir = config.get_agent_dir(target)
    try:
        folder = path_confinement.join_under(agent_dir, rel_dir)
        if row:
            await app_lifecycle.purge(row)
        else:
            # The files' own delete: a machine that took the copy drops it
            # too, where a removal on this side alone comes back at its sync.
            await app_lifecycle.remove_workspace_folder(target, agent_dir, folder)
        if await asyncio.to_thread(folder.exists):
            left.append(f"the folder {quoted('apps/' + slug)}")
    except Exception:  # noqa: BLE001  # a failed removal is named to the person, never raised
        left.append(f"the folder {quoted('apps/' + slug)}")
    seeded = await asyncio.to_thread(_bundle_tasks, target, slug)
    for task_id, name in seeded.items():
        if task_id in tasks_before:
            continue
        try:
            await asyncio.to_thread(task_store.delete_dynamic_task, task_id)
        except Exception:  # noqa: BLE001  # the same: named, never raised
            left.append(f"the task {quoted(name)}")
    return left


def _refused_words(out: dict) -> str:
    """The deploy's own reason for refusing the copy: its first problem,
    or the rendered check's summary."""
    from services.apps import app_lint
    for finding in out.get("findings") or []:
        if finding.get("severity") == app_lint.FAIL and finding.get("message"):
            return str(finding["message"])[:240]
    render = out.get("render") or {}
    return str(render.get("summary") or "the copy did not pass its checks")[:240]


async def _share_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    from services.apps import app_blueprints, app_deploy
    refusal = await _share_check(params, u)
    if refusal:
        return Outcome(st.OUTCOME_FAILED, refusal)
    source, src_user, target, dst_user, slug = await _share_sides(params, u)
    row = await asyncio.to_thread(task_store.get_app_by_slug, source, src_user, slug) or {}
    # The live release when there is one, so the copy follows the release
    # its lineage names; the working folder otherwise (release 0).
    from services.apps import releases
    try:
        live = await asyncio.to_thread(releases.live_release_dir, row)
    except releases.ReleaseDamaged:
        live = None
    data, _blueprint = await asyncio.to_thread(lambda: app_blueprints.export_bundle(row, release_dir=live))
    folder, blueprint = await asyncio.to_thread(app_blueprints.read_bundle, data)
    owner_sub = u.acting_sub if dst_user else None
    tasks_before = await asyncio.to_thread(_bundle_tasks, target, slug)
    reason = ""
    out: dict = {}
    try:
        out = await app_blueprints.import_folder(
            target, dst_user, owner_sub, slug, folder,
            template_slug=f"bundle:{slug}", tasks=blueprint.get("tasks") or [],
            origin=app_blueprints.clean_origin(blueprint) or ("", ""),
            follows=app_blueprints.clean_source(blueprint))
    except (app_blueprints.BlueprintError, app_deploy.DeployError) as exc:
        reason = str(exc)
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
    status = str(out.get("status") or "")
    if not reason and status == app_deploy.RESULT_PENDING_APPROVAL:
        return Outcome(st.OUTCOME_DONE,
                       f"The app {quoted(slug)} is on {display(target)}. Its first release waits for approval there.")
    if not reason and status == app_deploy.RESULT_OK:
        return Outcome(st.OUTCOME_DONE, f"The app {quoted(slug)} is live on {display(target)}.")
    left = await _undo_copy(target, dst_user, slug, tasks_before)
    text = f"The app {quoted(slug)} was not shared. {sentence(reason or _refused_words(out))}"
    if left:
        text += f" These stay on {display(target)}: {', '.join(left)}."
    return Outcome(st.OUTCOME_FAILED, text)


SHARE_APP = Action(
    name="share_app",
    schema={"type": "object", "required": ["app_slug", "target_agent"], "additionalProperties": False,
            "properties": {"app_slug": APP_SLUG, "source_visibility": SCOPE_WORD, "target_agent": SLUG,
                           "visibility": SCOPE_WORD, "_agent": AGENT_FIELD}},
    words=_share_words, check=_share_check, run=_share_run,
    personal=lambda p: "apps" if visibility.SCOPE_USER in (p.get("source_visibility"), p.get("visibility")) else "",
)


# ---------------------------------------------------------------------------
# attach_library
# ---------------------------------------------------------------------------

def _attachment(source: str, target: str, subdir: str) -> dict | None:
    """The target's attachment of the source's library, if there is one."""
    return next((a for a in db_knowledge_libraries.attachments_for_consumer(target)
                 if a["source_agent"] == source and (a.get("subdir") or "") == subdir), None)


def _attach_sides(params: dict[str, Any]) -> tuple[str, str, str, str]:
    """The source, the target, the library's folder and the library in words."""
    source, target = str(params["source_agent"]), str(params["target_agent"])
    subdir = library_rules.normalize_subdir(str(params.get("subdir") or ""))
    lib = (f"the {quoted(subdir)} library of {display(source)}" if subdir
           else f"the shared knowledge library of {display(source)}")
    return source, target, subdir, lib


async def _attach_words(params: dict[str, Any], u) -> dict[str, str]:
    source, target, subdir, lib = _attach_sides(params)
    existing = await asyncio.to_thread(_attachment, source, target, subdir)
    if existing is not None and params.get("writable") is not None:
        # The card changes an attachment that exists: it says which way.
        if params["writable"]:
            return {"title": f"Let {display(target)} write to the library of {display(source)}",
                    "text": f"{display(target)} already reads {lib}. This card lets it write there too.",
                    "label": "Allow writing"}
        return {"title": f"Make the library of {display(source)} read-only for {display(target)}",
                "text": f"{display(target)} already reads {lib}. This card makes it read-only.",
                "label": "Make read-only"}
    text = f"{display(target)} reads {lib}"
    text += " and may write to it." if params.get("writable") else "."
    text += " Its sessions see the files at their next start."
    return {"title": f"Attach the library of {display(source)} to {display(target)}", "text": text,
            "label": "Attach"}


async def _attach_check(params: dict[str, Any], u) -> str | None:
    if u.acting_sub is None:
        return NOT_A_PERSON
    if not roles.is_creator_or_above(u.role):
        return ATTACH_NEEDS_CREATOR
    source, target = str(params["source_agent"]), str(params["target_agent"])
    for agent in (source, target):
        if not agent_store.get_agent(agent):
            return NO_SUCH_AGENT.format(agent=agent)
    if source == target:
        return SAME_AGENT
    if not library_rules.may_wire_library(u, target):
        return library_rules.NOT_YOUR_LIBRARY_AGENT
    if not u.is_admin and not u.can_access_agent(source):
        return libraries_api.NO_SOURCE_ACCESS
    if params.get("writable") and not u.is_admin and not u.can_edit_agent(source):
        return ("Writable attachment requires editor, manager, or admin role on the source agent. "
                "Attach read-only instead.")
    subdir = library_rules.normalize_subdir(str(params.get("subdir") or ""))
    if not await asyncio.to_thread(db_knowledge_libraries.is_promoted, source, subdir):
        return NOT_A_LIBRARY.format(agent=display(source))
    existing = await asyncio.to_thread(_attachment, source, target, subdir)
    wanted = params.get("writable")
    if existing is not None and (wanted is None or bool(wanted) == bool(existing.get("writable"))):
        # Nothing would change: a card that leaves the flag alone does nothing.
        return ALREADY_ATTACHED.format(target=display(target), lib=_attach_sides(params)[3])
    return None


async def _attach_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    refusal = await _attach_check(params, u)
    if refusal:
        return Outcome(st.OUTCOME_FAILED, refusal)
    source, target, subdir, _lib = _attach_sides(params)
    existing = await asyncio.to_thread(_attachment, source, target, subdir)
    wanted = params.get("writable")
    # A card that names no flag leaves an existing attachment's flag alone.
    writable = bool(wanted) if wanted is not None else bool((existing or {}).get("writable"))
    req = libraries_api.AttachRequest(source_agent=source, subdir=str(params.get("subdir") or ""),
                                      writable=writable)
    try:
        result = await libraries_api.attach_knowledge_library(target, req, user=u)
    except HTTPException as exc:
        return Outcome(st.OUTCOME_FAILED, sentence(str(exc.detail)))
    if result.get("created"):
        return Outcome(st.OUTCOME_DONE, f"{display(target)} now reads the library of {display(source)}.")
    if writable:
        return Outcome(st.OUTCOME_DONE, f"{display(target)} can now write to the library of {display(source)}.")
    return Outcome(st.OUTCOME_DONE, f"{display(target)} now reads the library of {display(source)} without "
                                    "writing to it.")


ATTACH_LIBRARY = Action(
    name="attach_library",
    schema={"type": "object", "required": ["source_agent", "target_agent"], "additionalProperties": False,
            "properties": {"source_agent": SLUG, "target_agent": SLUG, "writable": {"type": "boolean"},
                           "subdir": {"type": "string", "maxLength": 200}, "_agent": AGENT_FIELD}},
    words=_attach_words, check=_attach_check, run=_attach_run,
)


# ---------------------------------------------------------------------------
# update_followed_app, send_release_to_source (services/apps/app_follow.py)
# ---------------------------------------------------------------------------

async def _followed_row(params: dict[str, Any], u) -> tuple[str, dict | None]:
    """The agent and the row the card names: the agent's shared app, or the
    person's own when the card says ``visibility: user``."""
    agent = _agent_of(params)
    personal = str(params.get("visibility") or visibility.SCOPE_AGENT) == visibility.SCOPE_USER
    username = await _username(u) if personal else ""
    row = await asyncio.to_thread(task_store.get_app_by_slug, agent, username, str(params.get("app_slug") or ""))
    return agent, row


async def _followed_check(params: dict[str, Any], u, move: str) -> str | None:
    if u.acting_sub is None:
        return NOT_A_PERSON
    agent, row = await _followed_row(params, u)
    if not agent_store.get_agent(agent):
        return NO_SUCH_AGENT.format(agent=agent)
    if not row:
        return NO_SUCH_APP.format(slug=quoted(str(params.get("app_slug") or "")), agent=display(agent))
    why = await asyncio.to_thread(app_follow.refusal, row, u, move)
    return why.detail if why else None


def _followed_words(row: dict | None) -> tuple[str, str, str, int]:
    """The app's title, the source agent's name and slug, the release the
    card speaks of (0 when the state offers none)."""
    if not row:
        return "", "", "", 0
    follow = app_follow.parse(row) or {}
    current = app_follow.state(row) or {}
    update = current.get("update") or {}
    source = current.get("source") or {}
    return (str(row.get("title") or row["slug"]), str(source.get("agent_name") or follow.get("agent") or ""),
            str(follow.get("slug") or ""), int(update.get("release") or 0))


async def _update_words(params: dict[str, Any], u) -> dict[str, str]:
    _agent, row = await _followed_row(params, u)
    title, source, _slug, release = await asyncio.to_thread(_followed_words, row)
    slug = str(params.get("app_slug") or "")
    shown = f"release {release}" if release else "its newer release"
    if params.get("mode") == app_follow.MODE_MERGE:
        return {"title": f"Ask the agent to merge {shown} into {quoted(title or slug)}",
                "text": (f"Unpacks {shown} of {source}'s app beside this app's files. The agent merges the two "
                         "into one release and deploys it as always."),
                "label": "Merge"}
    return {"title": f"Update the app {quoted(title or slug)} to {shown}",
            "text": (f"Replaces the files of {quoted(slug)} with {shown} of {source}'s app. The app's data stays. "
                     "The new release waits for approval here."),
            "label": "Update"}


async def _update_check(params: dict[str, Any], u) -> str | None:
    move = app_follow.MODE_MERGE if params.get("mode") == app_follow.MODE_MERGE else app_follow.MODE_UPDATE
    return await _followed_check(params, u, move)


async def _update_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    refusal = await _update_check(params, u)
    if refusal:
        return Outcome(st.OUTCOME_FAILED, refusal)
    _agent, row = await _followed_row(params, u)
    try:
        if params.get("mode") == app_follow.MODE_MERGE:
            out = await app_follow.stage_merge(row or {}, u)
        else:
            out = await app_follow.take(row or {}, u)
    except app_follow.FollowRefused as exc:
        return Outcome(st.OUTCOME_FAILED, exc.detail)
    return Outcome(st.OUTCOME_DONE, out["text"], extra={"release": out["release"]})


def _personal_app(params: dict[str, Any]) -> str:
    """A followed app in the asking person's own space is theirs to move."""
    return "apps" if params.get("visibility") == visibility.SCOPE_USER else ""


UPDATE_FOLLOWED_APP = Action(
    name="update_followed_app",
    schema={"type": "object", "required": ["app_slug", "mode"], "additionalProperties": False,
            "properties": {"app_slug": APP_SLUG, "visibility": SCOPE_WORD,
                           "mode": {"type": "string", "enum": list(app_follow.MODES)}, "_agent": AGENT_FIELD}},
    words=_update_words, check=_update_check, run=_update_run,
    personal=_personal_app,
)


async def _send_words(params: dict[str, Any], u) -> dict[str, str]:
    from services.apps import releases
    _agent, row = await _followed_row(params, u)
    title, source, source_slug, _release = await asyncio.to_thread(_followed_words, row)
    slug = str(params.get("app_slug") or "")
    n = await asyncio.to_thread(releases.current_number, row) if row else 0
    return {"title": f"Send release {n} of {quoted(title or slug)} to {source}",
            "text": (f"Puts this app's release {n} into {source}'s app {quoted(source_slug or slug)} as a new "
                     "release, which waits for approval there. The data of neither app moves."),
            "label": "Send"}


async def _send_check(params: dict[str, Any], u) -> str | None:
    return await _followed_check(params, u, app_follow.MOVE_SEND)


async def _send_run(params: dict[str, Any], options: dict[str, bool], u) -> Outcome:
    refusal = await _send_check(params, u)
    if refusal:
        return Outcome(st.OUTCOME_FAILED, refusal)
    _agent, row = await _followed_row(params, u)
    try:
        out = await app_follow.send_back(row or {}, u)
    except app_follow.FollowRefused as exc:
        return Outcome(st.OUTCOME_FAILED, exc.detail)
    return Outcome(st.OUTCOME_DONE, out["text"], extra={"release": out["release"]})


SEND_RELEASE_TO_SOURCE = Action(
    name="send_release_to_source",
    schema={"type": "object", "required": ["app_slug"], "additionalProperties": False,
            "properties": {"app_slug": APP_SLUG, "visibility": SCOPE_WORD, "_agent": AGENT_FIELD}},
    words=_send_words, check=_send_check, run=_send_run,
    personal=_personal_app,
)


ACTIONS: tuple[Action, ...] = (HAND_FILES, PROMOTE_MEMORY, SHARE_APP, ATTACH_LIBRARY, UPDATE_FOLLOWED_APP,
                               SEND_RELEASE_TO_SOURCE)
