"""Offboarding: what a person leaves behind changes hands, and their trees
leave the agent directories.

Subscribes to ``services/agents/offboarding.py`` (``automation-transfer``, priority
20, after the session closer). A person who lost the editor tier on an
agent, the agent, or the account can no longer own automations that act as
the agent: their agent-scope scheduled and one-time tasks, trigger tasks,
triggers and scheduled notifications on that agent move to the admin or
manager who made the change (the install owner when no person did), keep
their schedule and carry a record of the transfer (``transferred_from``,
the first creator; ``transferred_at``, the last hop). Their
self-continuations on a chat that runs as the agent (a Shared-only agent's
shared chat, an agent-scope task chat: both take the editor tier to
drive) end instead: a chat's own bounded wake is not a standing
automation, and the new owner never asked to have that chat woken under
their name. A continuation a caller scheduled from a phone call is their
own wake at their own role and stays. A transferred task runs without the
knowledge-write grant until a manager adopts it by editing its prompt. The
new owner is told once, and only when something moved (ended
continuations are listed in that notice, never announced on their own).
Delegate workers, judge runs, app handlers and rows seeded under a slug
are not a person's and stay. A user-scope trigger notifies only its
creator: it is never re-aimed, only swept where its creator lost the
agent.

The person's user-scope automations on agents they can no longer reach are
deleted (a deletion sweeps every agent), and the meetings they convened on
those agents end. The wakes stored for them or scheduled by them on those
agents' chats (and on a Shared-only agent where they fell below the editor
tier) are dropped, so no later turn on such a chat replays them. The standing is read again, under the person's row lock,
before anything moves: a person re-added since the event keeps their rows.

The event is sent once and a restart can drop it, so the transfer is redone
once more after ``RESWEEP_AFTER_S`` (a request that authorised before the
change may commit a row after the first pass) and at boot, where every
agent-scope row whose creator is a live person below the editor tier there
or a person deleted since moves to the install owner. A local account's
subject (``local:<uuid>``) that no user row carries is a person deleted
before retired usernames were kept, and moves the same way. An agent slug,
``api``, the master key and a session principal are not a person's and are
left alone; any other creator no user row carries (an identity provider's
subject, which has no shape of its own) is left in place and named in the
owner's boot notice for review.

A deleted person's ``users/<username>`` directories leave every agent tree
into ``AGENTS_DIR/.offboarded/<username>/<agent>/`` (0700), by rename
only, after ``ARCHIVE_AFTER_S`` (their sessions have closed by then) and
again at boot for every retired username with a tree still present. The
username is never minted again (``retired_usernames``); a person whose row
came back with an older dump is not archived and gets their name back.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
from collections.abc import Iterable

import config
from auth import roles
from core import layout
from services.agents import offboarding
from services.scheduler import task_kinds
from storage import database as task_store
from storage.automation import notification_store
from storage.chat import meeting_status
from storage.pg import run_db

logger = logging.getLogger("claude-proxy.offboarding")

SUBSCRIBER = "automation-transfer"
RESWEEP_AFTER_S = 75.0
BOOT_RECONCILE_AFTER_S = 60.0
# The session closer's re-passes run one after the other (75 s, then 240 s
# more: the last at 315 s) and each close batch is bounded at 120 s, so the
# person's sessions are gone by 435 s.
ARCHIVE_AFTER_S = 450.0
ARCHIVE_DIRNAME = ".offboarded"
NOTIFICATION_SOURCE = "offboarding"
NOTIFICATION_HREF = "/admin/scheduled-tasks"
MEETING_END_REASON = "The person who convened this meeting no longer has access to the agent"
# The task kinds a person owns as the agent; delegate, judge and app rows
# are not theirs to keep.
KINDS = (task_kinds.SCHEDULED, task_kinds.ONE_TIME, task_kinds.TRIGGER, task_kinds.CONTINUATION)
REASON_BOOT = "boot"
_REASON_TEXT = {
    offboarding.REMOVED: "removed from the agent",
    offboarding.DEMOTED: "moved below the editor tier",
    offboarding.DELETED: "account deleted",
    REASON_BOOT: "no longer holds the editor tier",
}
_NAMED = 10
# The subject a local account is minted with (``db_users.create_local_user``).
_LOCAL_SUB = re.compile(r"local:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Creator values that name no person: an agent-scope API call with no agent
# header, the master key.
_NOT_A_PERSON = ("api", "api-key")
# The unknown creators the owner was already told about, as a JSON list of
# ``[agent, creator]``: each is named once, not at every restart.
_REPORTED_KEY = "offboarding_unknown_creators_reported"

_background: set[asyncio.Task] = set()
_repasses: dict[str, asyncio.Task] = {}


def _keep(task: asyncio.Task) -> asyncio.Task:
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def _people(subs: set[str]) -> dict[str, tuple[dict | None, dict[str, str]]]:
    """``{sub: (user row or None, agent roles)}``. Synchronous."""
    out = {}
    for sub in subs:
        user = task_store.get_user(sub)
        out[sub] = (user, task_store.get_user_agent_roles(sub) if user else {})
    return out


def _role_now(people: dict, sub: str, agent: str) -> str:
    user, agent_roles = people.get(sub) or (None, {})
    if not user:
        return roles.NO_ACCESS
    return roles.effective_role(user.get("role"), agent_roles, agent)


def _target_for(actor: str, sub: str) -> str:
    """The new owner: the actor when they are a person other than ``sub``,
    else the install owner; "" when there is no owner row."""
    if actor and actor != sub and task_store.get_user(actor):
        return actor
    return task_store.get_owner_sub() or ""


def _person_label(sub: str, names: tuple[str, str, str]) -> str:
    display_name, email, _username = names
    return display_name or email or sub


def _api_keys_minted_by(sub: str) -> list[dict]:
    from storage.identity import api_key_store
    return [k for k in api_key_store.list_agent_api_keys() if k.get("created_by") == sub]


def _unreported(unknown: dict[str, list[str]]) -> dict[str, list[str]]:
    """The unknown creators not named in an earlier boot notice; the stored
    list becomes the current set. Synchronous."""
    import json
    try:
        told = {tuple(pair) for pair in json.loads(
            task_store.get_platform_setting(_REPORTED_KEY) or "[]")}
    except (TypeError, ValueError):
        told = set()
    now = sorted((agent, creator) for agent, creators in unknown.items() for creator in creators)
    fresh: dict[str, list[str]] = {}
    for agent, creator in now:
        if (agent, creator) not in told:
            fresh.setdefault(agent, []).append(creator)
    task_store.set_platform_setting(_REPORTED_KEY, json.dumps([list(pair) for pair in now]))
    return fresh


def _not_a_person(creator: str, agents: set[str]) -> bool:
    from auth.providers import SESSION_SUB_PREFIX
    return (creator in agents or creator in _NOT_A_PERSON
            or creator.startswith(SESSION_SUB_PREFIX))


def _counts(result: dict) -> dict[str, int]:
    return {
        "tasks": len(result["tasks"]),
        "continuations": len(result["continuations"]),
        "triggers": len(result["triggers"]),
        "notifications": len(result["notifications"]),
        "retargeted": result["retargeted"],
    }


def _changed_anything(result: dict | None) -> bool:
    return bool(result) and any(_counts(result).values())


def _moved_anything(result: dict | None) -> bool:
    """Something changed hands (ended continuations alone are not worth a
    notice)."""
    return bool(result) and any(v for k, v in _counts(result).items() if k != "continuations")


def _drop_jobs(result: dict) -> None:
    """The ended continuations' clock jobs go with their rows (a fire of a
    gone row is dropped anyway; this keeps the scheduler tidy)."""
    from services.scheduler.runner import _drop_job
    for row in result["continuations"]:
        _drop_job(row["id"])


def _notification_text(sub: str, reason: str, names: tuple[str, str, str],
                       moved: dict[str, dict], keys: list[dict],
                       unknown: dict[str, list[str]] | None = None) -> tuple[str, str]:
    who = _person_label(sub, names)
    lines = []
    if any(_moved_anything(r) for r in moved.values()):
        lines.append(f"{who} ({_REASON_TEXT.get(reason, reason)}) left automations that act "
                     f"as agents you manage. They are yours now and keep their schedules; "
                     f"their chat continuations ended.")
    for agent, result in sorted(moved.items()):
        c = _counts(result)
        parts = []
        clocked = result["tasks"]
        if c["tasks"]:
            shown = ", ".join(t["name"] for t in clocked[:_NAMED])
            more = f" and {len(clocked) - _NAMED} more" if len(clocked) > _NAMED else ""
            parts.append(f"{c['tasks']} task(s): {shown}{more}")
        if c["continuations"]:
            parts.append(f"{c['continuations']} chat continuation(s) ended")
        if c["triggers"]:
            shown = ", ".join(t["name"] or t["slug"] for t in result["triggers"][:_NAMED])
            more = (f" and {len(result['triggers']) - _NAMED} more"
                    if len(result["triggers"]) > _NAMED else "")
            parts.append(f"{c['triggers']} trigger(s): {shown}{more}")
        if c["notifications"]:
            parts.append(f"{c['notifications']} scheduled notification(s)")
        if c["retargeted"]:
            parts.append(f"{c['retargeted']} trigger notification(s) now addressed to you")
        lines.append(f"{agent}: " + "; ".join(parts))
    if keys:
        lines.append(f"They minted {len(keys)} agent API key(s), still valid: "
                     + ", ".join(f"{k['agent']}/{k['name']}" for k in keys[:_NAMED])
                     + (f" and {len(keys) - _NAMED} more" if len(keys) > _NAMED else "")
                     + ". Review them under the agent's API keys.")
    if lines:
        lines.append("A transferred task runs without knowledge writes until you or another "
                     "manager edits its prompt.")
    for agent, creators in sorted((unknown or {}).items()):
        shown = ", ".join(creators[:_NAMED])
        more = f" and {len(creators) - _NAMED} more" if len(creators) > _NAMED else ""
        lines.append(f"{agent}: automations created by an account this install does not "
                     f"know ({shown}{more}) act as the agent. They were left as they are; "
                     f"review them.")
    title = ("Automations transferred to you" if any(_moved_anything(r) for r in moved.values())
             else "Automations to review")
    return title, "\n".join(lines)


async def _notify(target: str, sub: str, reason: str, names: tuple[str, str, str],
                  moved: dict[str, dict], unknown: dict[str, list[str]] | None = None) -> None:
    from services.notifications.notification_manager import fire_notification
    # The boot notice speaks for several people and names no minter.
    keys = await run_db(_api_keys_minted_by, sub) if sub else []
    title, body = _notification_text(sub, reason, names, moved, keys, unknown)
    try:
        await fire_notification(title, body, severity="info", scope="user", target=target,
                                source=NOTIFICATION_SOURCE, href=NOTIFICATION_HREF)
    except Exception:
        logger.exception("offboarding: the transfer notification to %s was not sent", target)


async def _end_meetings(sub: str, people: dict) -> int:
    """End the live and pending meetings the person convened on an agent
    they can no longer reach (a running one concludes, a pending one is
    marked failed with the reason). Returns how many."""
    import contextlib
    import json
    from services.meetings import meeting_orchestrator
    ended = 0
    for meeting in await run_db(task_store.list_live_meetings_created_by, sub):
        agents = {meeting.get("moderator") or ""}
        with contextlib.suppress(TypeError, ValueError):
            agents |= set(json.loads(meeting.get("participants") or "[]"))
        agents.discard("")
        if not any(_role_now(people, sub, a) == roles.NO_ACCESS for a in agents):
            continue
        try:
            if meeting["status"] in meeting_status.ENDABLE:
                await meeting_orchestrator.end_meeting(meeting["id"], None)
            else:
                await meeting_orchestrator.fail_meeting(meeting["id"], MEETING_END_REASON)
            ended += 1
        except Exception:
            logger.warning("offboarding: meeting %s of %s was not ended", meeting["id"], sub,
                           exc_info=True)
    if ended:
        logger.info("offboarding: ended %d meeting(s) of %s", ended, sub)
    return ended


def _drop_stored_wakes(sub: str, reason: str, people: dict) -> int:
    """The wakes stored for the person, or scheduled by them, on chats of an
    agent they lost (every agent for a deletion), and of a Shared-only agent
    where they fell below the editor tier (its chats run from the agent's
    own state). Synchronous."""
    from core.session.visibility import is_shared_only
    if reason == offboarding.DELETED:
        return task_store.drop_pending_wakes_of(sub, None)
    lost = []
    for agent in task_store.pending_wake_agents_of(sub):
        role = _role_now(people, sub, agent)
        if role == roles.NO_ACCESS or (is_shared_only(agent) and not roles.can_edit(role)):
            lost.append(agent)
    return task_store.drop_pending_wakes_of(sub, lost) if lost else 0


async def _sweep_user_scope(sub: str, reason: str, people: dict) -> list[str]:
    """The person's user-scope rows on agents they can no longer reach go
    (every agent for a deletion). Returns the agents swept."""
    if reason == offboarding.DELETED:
        await run_db(task_store.remove_user_scope_items, sub, None)
        return ["*"]
    held = await run_db(task_store.agents_with_user_scope_automations, sub)
    lost = [a for a in held if _role_now(people, sub, a) == roles.NO_ACCESS]
    if lost:
        await run_db(task_store.remove_user_scope_items, sub, lost)
    return lost


async def reconcile_person(sub: str, *, actor: str, reason: str,
                           names: tuple[str, str, str] = ("", "", "")) -> dict[str, dict]:
    """Move what the person may no longer own, end their meetings on the
    agents they lost and sweep their user-scope rows there; tell the new
    owner when something moved. Returns what moved, by agent."""
    target = await run_db(_target_for, actor, sub)
    agents = await run_db(task_store.agents_with_automations_by, sub, list(KINDS))
    moved: dict[str, dict] = {}
    if agents and not target:
        logger.error("offboarding: %s left automations on %d agent(s) but the install has no "
                     "owner row; nothing moves", sub, len(agents))
    elif agents:
        for agent in agents:
            try:
                result = await run_db(task_store.transfer_agent_scope_automations, agent, sub,
                                      target, list(KINDS), from_names=(names[2],))
            except Exception:
                logger.exception("offboarding: the automations of %s on %s were not moved",
                                 sub, agent)
                continue
            if result is None:
                logger.info("offboarding: %s holds the editor tier on %s again; nothing moves",
                            sub, agent)
            elif _changed_anything(result):
                moved[agent] = result
                _drop_jobs(result)
                logger.info("offboarding: %s -> %s on %s: %s", sub, target, agent,
                            ", ".join(f"{k}={v}" for k, v in _counts(result).items() if v))
    people = await run_db(_people, {sub})
    try:
        await _sweep_user_scope(sub, reason, people)
    except Exception:
        logger.exception("offboarding: the user-scope rows of %s were not swept", sub)
    if reason != offboarding.DELETED:
        # An app share placed in an agent they no longer hold (a demoted
        # admin held every agent) waits again in their section (SHARING.md);
        # a membership removal already did this in its own transaction.
        try:
            from storage.sharing import share_store
            unplaced = await run_db(share_store.reap_unheld_placements, sub)
            if unplaced:
                logger.info("offboarding: %d placed share(s) of %s un-placed", unplaced, sub)
        except Exception:
            logger.exception("offboarding: the placed shares of %s were not un-placed", sub)
    try:
        dropped = await run_db(_drop_stored_wakes, sub, reason, people)
        if dropped:
            logger.info("offboarding: %d stored wake(s) of %s dropped", dropped, sub)
    except Exception:
        logger.exception("offboarding: the stored wakes of %s were not dropped", sub)
    try:
        await _end_meetings(sub, people)
    except Exception:
        logger.exception("offboarding: the meetings of %s were not ended", sub)
    if any(_moved_anything(r) for r in moved.values()):
        _keep(asyncio.ensure_future(_notify(target, sub, reason, names, moved)))
    return moved


async def _later(event: offboarding.OffboardEvent, names: tuple[str, str, str]) -> None:
    try:
        await asyncio.sleep(RESWEEP_AFTER_S)
        await reconcile_person(event.sub, actor=event.actor_sub, reason=event.reason, names=names)
        if event.reason == offboarding.DELETED and event.username:
            await asyncio.sleep(max(0.0, ARCHIVE_AFTER_S - RESWEEP_AFTER_S))
            await archive_person(event.username)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("offboarding: a later pass for %s failed", event.sub)


async def on_offboard(event: offboarding.OffboardEvent) -> None:
    """The subscriber: one pass now, one later (and the archive for a
    deletion)."""
    names = (event.display_name, event.email, event.username)
    await reconcile_person(event.sub, actor=event.actor_sub, reason=event.reason, names=names)
    previous = _repasses.pop(event.sub, None)
    if previous is not None and not previous.done():
        previous.cancel()
    task = _keep(asyncio.ensure_future(_later(event, names)))
    _repasses[event.sub] = task

    def _forget(done: asyncio.Task, sub: str = event.sub) -> None:
        if _repasses.get(sub) is done:
            _repasses.pop(sub, None)

    task.add_done_callback(_forget)


# --- the archive -------------------------------------------------------------


def _agent_dirs() -> list[str]:
    try:
        with os.scandir(config.AGENTS_DIR) as entries:
            return sorted(e.name for e in entries
                          if not e.name.startswith(".") and e.is_dir(follow_symlinks=False))
    except FileNotFoundError:
        return []


def _archive_trees(username: str) -> int:
    """Rename every ``<agent>/users/<username>`` into the archive. Returns
    how many trees are still in place (a boundary the rename cannot cross,
    a destination already taken): those are logged and retried at boot.
    Synchronous, filesystem only."""
    from services.infra import safe_fs
    root = config.AGENTS_DIR
    remaining = 0
    for agent in _agent_dirs():
        rel = f"{agent}/{layout.USERS}/{username}"
        try:
            safe_fs.lstat_beneath(root, rel)
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("offboarding: %s could not be inspected", rel, exc_info=True)
            remaining += 1
            continue
        dst = f"{ARCHIVE_DIRNAME}/{username}/{agent}"
        try:
            safe_fs.mkdirs_beneath(root, f"{ARCHIVE_DIRNAME}/{username}", mode=0o700)
            safe_fs.rename_beneath(root, rel, dst)
            logger.info("offboarding: archived %s as %s", rel, dst)
        except FileExistsError:
            logger.warning("offboarding: %s already exists; %s stays in place", dst, rel)
            remaining += 1
        except OSError as e:
            if e.errno == errno.EXDEV:
                logger.warning("offboarding: %s is on another filesystem than the archive; "
                               "it stays in place", rel)
            else:
                logger.warning("offboarding: %s was not archived", rel, exc_info=True)
            remaining += 1
    return remaining


async def archive_person(username: str) -> bool:
    """Move the retired person's trees into the archive; True when none is
    left in an agent tree. A live user holding the username is the person
    back (an older dump restored): their retired row goes and nothing
    moves."""
    if not username:
        return False
    if await run_db(task_store.get_user_sub_by_username, username):
        if await run_db(task_store.drop_retired_username, username):
            logger.info("offboarding: %s is a live user again; the name is theirs", username)
        return False
    remaining = await asyncio.to_thread(_archive_trees, username)
    if remaining:
        return False
    await run_db(task_store.mark_username_archived, username)
    return True


def transferred_from_names(subs: Iterable[str]) -> dict[str, str]:
    """``{sub: name}`` for the first creators rows were transferred from, for
    the rows' "transferred from" line: a live person's display name, else a
    deleted person's retired username. Synchronous: call it off the loop."""
    wanted = [s for s in dict.fromkeys(subs) if s]
    if not wanted:
        return {}
    names = notification_store.resolve_subs_to_display_names(wanted)
    if len(names) < len(wanted):
        retired = {r["sub"]: r["username"] for r in task_store.list_retired_usernames()}
        for sub in wanted:
            if sub not in names and retired.get(sub):
                names[sub] = retired[sub]
    return names


# --- boot ----------------------------------------------------------------------


async def reconcile_at_boot() -> dict[str, dict]:
    """Every agent-scope row whose creator is a live person below the
    editor tier on the agent, or a person deleted since (a retired subject,
    or a local one no user row carries), moves to the install owner; the
    meetings of those people on agents they cannot reach end; every retired
    username with a tree still present is archived. A creator the install
    cannot place is named in the owner's notice. Returns what moved, by
    agent."""
    pairs = await run_db(task_store.list_automation_creators, list(KINDS))
    moved: dict[str, dict] = {}
    unknown: dict[str, list[str]] = {}
    creators = {creator for _agent, creator in pairs}
    people = await run_db(_people, creators) if creators else {}
    retired = await run_db(task_store.retired_subs)
    owner = await run_db(task_store.get_owner_sub) or ""
    from storage.agents import agent_store
    agents = set(await run_db(agent_store.get_agent_slugs))
    to_end: set[str] = set()
    for agent, creator in pairs:
        user, _agent_roles = people.get(creator) or (None, {})
        if user:
            if roles.can_edit(_role_now(people, creator, agent)):
                continue
        elif creator not in retired and not _LOCAL_SUB.fullmatch(creator):
            if not _not_a_person(creator, agents):
                unknown.setdefault(agent, []).append(creator)
            logger.debug("offboarding: %s on %s is not a known person's; left alone",
                         creator, agent)
            continue
        to_end.add(creator)
        if creator == owner:
            continue
        if not owner:
            logger.error("offboarding: %s left automations on %s but the install has no owner "
                         "row; nothing moves", creator, agent)
            continue
        username = (user or {}).get("username") or ""
        try:
            result = await run_db(task_store.transfer_agent_scope_automations, agent, creator,
                                  owner, list(KINDS), from_names=(username,))
        except Exception:
            logger.exception("offboarding: the automations of %s on %s were not moved at boot",
                             creator, agent)
            continue
        if _changed_anything(result):
            _drop_jobs(result)
            logger.info("offboarding: %s -> %s on %s at boot: %s", creator, owner, agent,
                        ", ".join(f"{k}={v}" for k, v in _counts(result).items() if v))
            bucket = moved.setdefault(agent, {"tasks": [], "continuations": [], "triggers": [],
                                              "notifications": [], "retargeted": 0})
            for key in ("tasks", "continuations", "triggers", "notifications"):
                bucket[key].extend(result[key])
            bucket["retargeted"] += result["retargeted"]
    for sub in sorted(to_end):
        try:
            await _end_meetings(sub, people)
        except Exception:
            logger.exception("offboarding: the meetings of %s were not ended at boot", sub)
    archived = 0
    for row in await run_db(task_store.list_retired_usernames, unarchived_only=True):
        archived += 1
        try:
            await archive_person(row["username"])
        except Exception:
            logger.exception("offboarding: the trees of %s were not archived at boot",
                             row["username"])
    try:
        from services.agents import shared_only_members
        await run_db(shared_only_members.log_legacy_rows)
    except Exception:
        logger.exception("offboarding: the Shared-only assignment check failed at boot")
    if owner:
        unknown = await run_db(_unreported, unknown)
    if owner and (unknown or any(_moved_anything(r) for r in moved.values())):
        _keep(asyncio.ensure_future(_notify(owner, "", REASON_BOOT,
                                            ("several people", "", ""), moved, unknown)))
    logger.info("offboarding: boot reconcile over %d creator row(s): %d agent(s) with rows moved, "
                "%d person(s) checked for meetings, %d retired name(s) checked for trees",
                len(pairs), len(moved), len(to_end), archived)
    return moved


async def _reconcile_after_boot() -> None:
    try:
        await asyncio.sleep(BOOT_RECONCILE_AFTER_S)
        await reconcile_at_boot()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("offboarding: the boot reconcile of automations failed")


def register() -> None:
    """Subscribe the transfer and schedule the boot reconcile. Called once
    from the lifespan (a running loop)."""
    offboarding.subscribe(SUBSCRIBER, on_offboard, priority=20)
    _keep(asyncio.ensure_future(_reconcile_after_boot()))
    logger.info("offboarding: %s subscribed (a person's automations follow their standing)",
                SUBSCRIBER)
