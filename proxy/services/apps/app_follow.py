"""Apps that follow their source.

A copy another agent received keeps where it came from in
``pinned_apps.follows`` (``record``): the source row's key and id, the
release the copy took and the copy's own release number at that moment,
plus at most one marker while something is in flight (``pending``: an update
cut and waiting for approval; ``staged``: a merge the agent was asked for;
``sent``: a release of the copy waiting on the source). ``state`` settles the
markers and reads what the copy's card shows; ``take`` puts the source's
live release on the copy, ``stage_merge`` unpacks it beside the copy's files,
``send_back`` puts the copy's live release on the source, each through the
import and the deploy pipeline with the release held for a person. No tree
hashes: the two sides' trees never hash equal (the export rewrites
``app.json``, the import rewrites task ids), so the state reads numbers and
markers alone. Every sentence a person reads is a constant of this module
(the routes and the card actions import them).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

import config
from core import layout
from services.apps import app_blueprints, app_deploy, releases
from services.apps.manifest import parse_actions
from services.infra import path_confinement, safe_fs
from storage import database as task_store, db_apps
from storage.agents import agent_store
from storage.pg import run_db

logger = logging.getLogger("otodock.apps")

MODE_UPDATE = "update"
MODE_MERGE = "merge"
# The card's third state: the source's release is unpacked beside the copy
# for a merge, and the copy has not released since.
MODE_ASKED = "asked"
MODES = (MODE_UPDATE, MODE_MERGE)

NOT_FOLLOWING = "This app does not follow another agent's app."
SOURCE_GONE = "The source of this app is gone. Stop following it from the app's menu."
COPY_NEEDS_EDITOR = "Only editors and managers update the team's apps."
COPY_NEEDS_OWNER = "Only the person who owns this app updates it."
SOURCE_NEEDS_EDITOR = "Only editors and managers of {source} take its releases."
SOURCE_NEEDS_OWNER = "Only the person who owns the app on {source} moves its releases."
NO_UPDATE = "{source}'s app has no newer release."
MERGE_DUE = "This app has released on its own since it was copied. Ask the agent to merge."
MERGE_ASKED = ("Release {release} is unpacked beside this app. The card clears when the agent deploys the "
               "merged release.")
WORKING_CHANGED = "The files of this app changed since its last release. Ask the agent to merge instead."
NOTHING_TO_SEND = "This app has no release to send yet."
SENT_ALREADY = "A release of this app already waits on {source}."
PENDING_WAITS = "{source}'s app has a release waiting for approval. Approve or reject it first."
SOURCE_WORKING_CHANGED = ("The files of {source}'s app changed since its last release. Ask its agent to "
                          "take this release by hand.")
NOT_PUT_BACK = "The app's files could not be put back. Ask the agent to deploy its last release again."
NOT_READ = "The app's files could not be read before the move. Try again."

# The snapshot of a working folder taken before a release is put over it, and
# the source's note of a release a copy sent, both beside the releases.
BACKUP_DIRNAME = "update-backup"
SENT_BACK_NAME = "sent-back.json"


class FollowRefused(Exception):
    """The refusal a route turns into its status and a card into its
    sentence."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


_locks: dict[str, asyncio.Lock] = {}
_holders: dict[str, int] = {}


@contextlib.asynccontextmanager
async def _locked(*row_ids: str):
    """One move at a time on each row it writes (a send-back writes the
    source too, so two copies sending to one source wait for each other),
    the locks taken in id order so two moves never wait crosswise. Never the
    deploy lock, which deploy_folder takes itself. An entry goes once nobody
    holds or waits for it."""
    ids = sorted({i for i in row_ids if i})
    for i in ids:
        _holders[i] = _holders.get(i, 0) + 1
    taken: list[asyncio.Lock] = []
    try:
        for i in ids:
            lock = _locks.setdefault(i, asyncio.Lock())
            await lock.acquire()
            taken.append(lock)
        yield
    finally:
        for lock in reversed(taken):
            lock.release()
        for i in ids:
            _holders[i] -= 1
            if not _holders[i]:
                _holders.pop(i)
                _locks.pop(i, None)


# ── the lineage ─────────────────────────────────────────────────────────────


def parse(row: dict) -> dict | None:
    """The lineage the row keeps, or None (a bad value reads as none)."""
    raw = row.get("follows") or ""
    if not raw:
        return None
    try:
        follow = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(follow, dict) or not follow.get("agent") or not follow.get("slug"):
        return None
    return follow


def _write(row_id: str, follow: dict | None) -> None:
    db_apps.set_follows(row_id, json.dumps(follow, sort_keys=True) if follow else "")


def record(app_id: str, key: tuple[str, str, str], source: dict, copy_release: int) -> None:
    """The lineage of a copy the import just made from ``source``
    (``app_blueprints.clean_source``): written when the copy is another row
    than the source and the source row exists on this install with that id
    (a re-import of one's own export, or a bundle from another install,
    follows nothing). Synchronous."""
    if (source["agent"], source["username"], source["slug"]) == key:
        return
    row = task_store.get_app_by_slug(source["agent"], source["username"], source["slug"])
    if not row or row["id"] != source["id"]:
        return
    _write(app_id, {"id": source["id"], "agent": source["agent"], "username": source["username"],
                    "slug": source["slug"], "release": int(source["release"]), "copy_release": int(copy_release)})


def stop(row: dict) -> None:
    _write(row["id"], None)


def _display(agent: str) -> str:
    return str((agent_store.get_agent(agent) or {}).get("display_name") or agent)


def _source_row(follow: dict) -> dict | None:
    return task_store.get_app_by_slug(follow["agent"], follow.get("username") or "", follow["slug"])


def state(row: dict) -> dict | None:
    """What the copy's card shows: None when the row follows nothing, else
    the source (its key, its name, the release the copy stands at, whether
    it still exists) and ``update``: None, or the source's newer release
    with the mode ``update`` (the copy never released on its own since),
    ``merge`` (it did) or ``asked`` (the release is unpacked beside the copy,
    whose ``path`` it names, and the copy has not released since); the
    source's ``held_release`` is the copy's release an update cut and holds
    for approval (0 when none). The markers are settled first, each
    settlement one write. Synchronous: call off the loop."""
    follow = parse(row)
    if not follow:
        return None
    source = _source_row(follow)
    out: dict[str, Any] = {
        "source": {"agent": follow["agent"], "agent_name": _display(follow["agent"]), "slug": follow["slug"],
                   "release": int(follow.get("release") or 0), "exists": source is not None,
                   "held_release": 0},
        "update": None,
    }
    if source is None:
        return out
    changed = False
    copy_current = releases.current_number(row)
    source_current = releases.current_number(source)
    if source["id"] != follow.get("id"):
        # A purge and a re-pin under the same slug numbered the releases from
        # one again: the lineage starts over from where both stand now.
        follow = {"id": source["id"], "agent": follow["agent"], "username": follow.get("username") or "",
                  "slug": follow["slug"], "release": source_current, "copy_release": copy_current}
        changed = True
    pending = follow.get("pending")
    if pending:
        if copy_current >= int(pending["release"]):
            follow.update(release=int(pending["source_release"]), copy_release=int(pending["release"]))
            follow.pop("pending")
            changed = True
        elif int(row.get("pending_release") or 0) != int(pending["release"]):
            # Rejected (the reject path put the live files back), or replaced
            # by the copy's own deploy of its working folder: the offer
            # stands again either way.
            follow.pop("pending")
            changed = True
    sent = follow.get("sent")
    if sent:
        if source_current >= int(sent["release"]):
            # The copy's own number when it sent: a release it made since
            # is not in the source, so the next source release is a merge.
            follow.update(release=int(sent["release"]), copy_release=int(sent.get("copy_release") or copy_current))
            follow.pop("sent")
            _drop_note(source, int(sent["release"]))
            changed = True
        elif int(source.get("pending_release") or 0) != int(sent["release"]):
            follow.pop("sent")
            _drop_note(source, int(sent["release"]))
            changed = True
    staged = follow.get("staged")
    if staged and copy_current > int(staged["above"]):
        # The first copy release past the staging is the merge; a later one
        # is the copy's own work again.
        merged = min((n for n in releases.release_numbers(row) if n > int(staged["above"])), default=copy_current)
        follow.update(release=int(staged["release"]), copy_release=merged)
        follow.pop("staged")
        staged = None
        changed = True
    if changed:
        _write(row["id"], follow)
    out["source"]["release"] = int(follow.get("release") or 0)
    out["source"]["held_release"] = int((follow.get("pending") or {}).get("release") or 0)
    if follow.get("pending") or not source_current or source_current <= int(follow.get("release") or 0):
        return out
    if staged and source_current == int(staged["release"]):
        # Where the release is unpacked, so the card can hand the request
        # over again (nothing reached the agent by the press itself).
        out["update"] = {"release": source_current, "mode": MODE_ASKED,
                         "path": "/" + staged_dir(row, source_current)[0]}
        return out
    if staged or releases.highest_number(row) > int(follow.get("copy_release") or 0):
        mode = MODE_MERGE
    else:
        mode = MODE_UPDATE
    out["update"] = {"release": source_current, "mode": mode}
    return out


# ── who may ─────────────────────────────────────────────────────────────────


def may_manage(row: dict, u) -> bool:
    """A shared row: editor or above on its agent; a personal row: its
    owner; an admin always."""
    if u.is_admin:
        return True
    if row.get("username"):
        return (row.get("owner_sub") or "") == (u.acting_sub or u.sub)
    return bool(u.can_edit_agent(row.get("agent") or ""))


def may_take(row: dict, u) -> bool:
    """The same rule on the source side of the row's lineage; False when
    the row follows nothing or the source is gone."""
    follow = parse(row)
    source = _source_row(follow) if follow else None
    return source is not None and may_manage(source, u)


def _sides(row: dict, u) -> tuple[dict, dict]:
    follow = parse(row)
    if not follow:
        raise FollowRefused(409, NOT_FOLLOWING)
    source = _source_row(follow)
    if source is None or not agent_store.get_agent(follow["agent"]):
        raise FollowRefused(409, SOURCE_GONE)
    if not may_manage(row, u):
        raise FollowRefused(403, needs_role(row))
    if not may_manage(source, u):
        raise FollowRefused(403, needs_role(source, side=_display(follow["agent"])))
    return follow, source


def needs_role(row: dict, side: str = "") -> str:
    """The refusal for a person who may not manage ``row``: a personal row
    is its owner's alone, a shared row needs editor or above. ``side`` names
    the source's agent when the row is the source."""
    if side:
        return (SOURCE_NEEDS_OWNER if row.get("username") else SOURCE_NEEDS_EDITOR).format(source=side)
    return COPY_NEEDS_OWNER if row.get("username") else COPY_NEEDS_EDITOR


# ── the files ───────────────────────────────────────────────────────────────


def _working(row: dict) -> Path:
    return config.get_agent_dir(row["agent"]) / (row.get("rel_path") or "")


def _working_matches_latest(row: dict) -> bool:
    """Whether the working folder still is the tree the latest release took
    (both are the row's own bytes, so the hashes compare)."""
    n = releases.highest_number(row)
    if not n:
        return True
    try:
        return releases.tree_sha(releases.app_release_dir(row) / str(n)) == releases.tree_hash(_working(row))
    except (releases.ReleaseInvalid, OSError):
        return False


def _snapshot(row: dict) -> Path:
    """The working folder's walked files copied beside the releases, with a
    manifest, so a refused import puts them back by it."""
    backup = releases.app_release_dir(row) / BACKUP_DIRNAME
    releases.copy_tree(_working(row), backup, replace=True)
    return backup


async def delete_files(agent: str, folder: Path, rels: list[str]) -> bool:
    """Each file through the files' own delete (the recover bin, the
    tombstone, the push a paired machine follows), so a machine holding the
    folder drops it too: a removal on this side alone comes back at its
    sync. False when one could not be removed."""
    from services.infra import file_bookkeeping
    agent_dir = config.get_agent_dir(agent)
    ok = True
    for rel in rels:
        try:
            await file_bookkeeping.delete_platform_file(agent, agent_dir, folder / rel)
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("App %s: %s was not removed", folder.name, rel)
            ok = False
    return ok


async def _restore(row: dict, from_dir: Path) -> bool:
    """The working folder as ``from_dir``'s manifest lists it: the walked
    files it does not list deleted (``delete_files``), its files written;
    True when it is back. The folder is reached beneath the agents tree,
    never through a link."""
    manifest = await asyncio.to_thread(releases.read_manifest, from_dir)
    if manifest is None:
        return False
    wanted = set(manifest["files"])

    def _dropped() -> list[str]:
        try:
            return [rel for rel, _p in releases.walk_tree(_working(row)) if rel not in wanted]
        except releases.ReleaseInvalid:
            return []

    def _write() -> None:
        agents_root, dest_rel = releases.agents_rel(_working(row))
        src_root, src_base = releases.agents_rel(from_dir)
        for rel in sorted(wanted):
            safe_fs.copy_file_beneath(src_root, f"{src_base}/{rel}", agents_root, f"{dest_rel}/{rel}",
                                      max_size=releases.MAX_RELEASE_FILE_BYTES, mode=releases.RELEASE_FILE_MODE,
                                      mkdirs=True)

    removed = await delete_files(row["agent"], _working(row), await asyncio.to_thread(_dropped))
    try:
        await asyncio.to_thread(_write)
    except OSError:
        logger.exception("App %s: the working folder was not put back", row.get("slug"))
        return False
    return removed


async def _push(row: dict) -> None:
    from services.infra import file_bookkeeping
    try:
        await file_bookkeeping.push_tree_write(row["agent"], _working(row), config.get_agent_dir(row["agent"]),
                                               writer="app-follow")
    except Exception:
        logger.exception("App %s: workspace push failed (continuing)", row.get("slug"))


def _bound_tasks(row: dict) -> dict[str, str]:
    """The task each button of the row's current manifest fires (only a
    task-firing button carries a ``task_id``): a button the target already
    has keeps its task."""
    return {str(a.get("id")): str(a["task_id"]) for a in parse_actions(row)
            if isinstance(a, dict) and a.get("task_id")}


def _seeded_tasks(agent: str, slug: str) -> set[str]:
    mark = f"bundle:{slug}"
    return {t["id"] for t in task_store.list_dynamic_tasks(agent=agent) if t.get("community_template") == mark}


def _live_dir(row: dict) -> Path | None:
    try:
        return releases.live_release_dir(row)
    except releases.ReleaseDamaged:
        return None


def _refused_words(out: dict) -> str:
    from services.apps import app_lint
    for finding in out.get("findings") or []:
        if finding.get("severity") == app_lint.FAIL and finding.get("message"):
            return str(finding["message"])[:240]
    render = out.get("render") or {}
    return str(render.get("summary") or "the release did not pass its checks")[:240]


async def _put(target: dict, source_row: dict, release_dir: Path) -> tuple[int, list[str]]:
    """``source_row``'s release at ``release_dir`` onto ``target``'s key,
    held for a person: the target's working folder snapshotted first and put
    back (with the tasks the import seeded removed) when the deploy refuses.
    Returns the release number cut and the tasks its new buttons seeded (a
    later rejection removes them); raises ``FollowRefused`` with the
    reason."""
    agent, username, slug = target["agent"], target.get("username") or "", target["slug"]
    # A release already waiting there would be replaced by this one (the
    # deploy supersedes it) and lost with its files: the move waits for it.
    fresh = await run_db(task_store.get_app, target["id"]) or target
    if int(fresh.get("pending_release") or 0):
        raise FollowRefused(409, PENDING_WAITS.format(source=_display(agent)))
    try:
        backup = await asyncio.to_thread(_snapshot, target)
    except (OSError, releases.ReleaseInvalid) as exc:
        logger.exception("App %s: the working folder could not be snapshotted", slug)
        raise FollowRefused(409, NOT_READ) from exc
    before = await asyncio.to_thread(_seeded_tasks, agent, slug)
    keep = _bound_tasks(target)
    reason = ""
    out: dict = {}
    try:
        data, _blueprint = await asyncio.to_thread(app_blueprints.export_bundle, source_row, release_dir=release_dir)
        folder, blueprint = await asyncio.to_thread(app_blueprints.read_bundle, data)
    except (app_blueprints.BlueprintError, app_deploy.DeployError) as exc:
        await asyncio.to_thread(shutil.rmtree, backup, True)
        raise FollowRefused(409, _sentence(str(exc))) from exc
    try:
        out = await app_blueprints.import_folder(
            agent, username, target.get("owner_sub"), slug, folder,
            template_slug=f"bundle:{slug}", tasks=blueprint.get("tasks") or [],
            origin=app_blueprints.clean_origin(blueprint) or ("", ""),
            voids_approval=False, replace=False, hold=True, keep_tasks=keep)
    except (app_blueprints.BlueprintError, app_deploy.DeployError) as exc:
        reason = str(exc)
    except Exception:
        reason = "the release could not be put in place"
        logger.exception("App %s: the follow import failed", slug)
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
    status = str(out.get("status") or "")
    if reason or status != app_deploy.RESULT_PENDING_APPROVAL:
        restored = await _restore(target, backup)
        await _push(target)
        after = await asyncio.to_thread(_seeded_tasks, agent, slug)
        for task_id in after - before:
            await asyncio.to_thread(task_store.delete_dynamic_task, task_id)
        text = _sentence(reason or _refused_words(out))
        if not restored:
            # The snapshot is the only copy of the files the move replaced:
            # it stays (the next move's snapshot replaces it), named in the log.
            logger.error("App %s: the working folder was not put back; its files stay at %s", slug, backup)
            raise FollowRefused(409, f"{text} {NOT_PUT_BACK}")
        await asyncio.to_thread(shutil.rmtree, backup, True)
        raise FollowRefused(409, text)
    await asyncio.to_thread(shutil.rmtree, backup, True)
    seeded = sorted(await asyncio.to_thread(_seeded_tasks, agent, slug) - before)
    return int(out.get("release") or 0), seeded


def _sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


# ── the three moves ─────────────────────────────────────────────────────────

MOVE_SEND = "send"


def refusal(row: dict, u, move: str) -> FollowRefused | None:
    """Why ``take`` (``MODE_UPDATE``), ``stage_merge`` (``MODE_MERGE``) or
    ``send_back`` (``MOVE_SEND``) would refuse now, or None: the draw-time
    check of a card and the move's own first step. Synchronous."""
    try:
        follow, source = _sides(row, u)
    except FollowRefused as exc:
        return exc
    current = state(row)
    update = (current or {}).get("update")
    name = _display(follow["agent"])
    if move == MODE_UPDATE:
        if not update:
            return FollowRefused(409, NO_UPDATE.format(source=name))
        if update["mode"] == MODE_ASKED:
            return FollowRefused(409, MERGE_ASKED.format(release=update["release"]))
        if update["mode"] == MODE_MERGE:
            return FollowRefused(409, MERGE_DUE)
        if not _working_matches_latest(row):
            return FollowRefused(409, WORKING_CHANGED)
        if _live_dir(source) is None:
            return FollowRefused(409, NO_UPDATE.format(source=name))
    elif move == MODE_MERGE:
        if not update or _live_dir(source) is None:
            return FollowRefused(409, NO_UPDATE.format(source=name))
    else:
        fresh = parse(task_store.get_app(row["id"]) or row) or follow
        if _live_dir(row) is None:
            return FollowRefused(409, NOTHING_TO_SEND)
        if fresh.get("sent"):
            return FollowRefused(409, SENT_ALREADY.format(source=name))
        if int(source.get("pending_release") or 0):
            return FollowRefused(409, PENDING_WAITS.format(source=name))
        if not _working_matches_latest(source):
            return FollowRefused(409, SOURCE_WORKING_CHANGED.format(source=name))
    return None


async def _checked(row: dict, u, move: str) -> tuple[dict, dict, dict]:
    """The fresh row, its lineage and the source once ``refusal`` passes."""
    row = await run_db(task_store.get_app, row["id"]) or row
    why = await asyncio.to_thread(refusal, row, u, move)
    if why is not None:
        raise why
    follow, source = await asyncio.to_thread(_sides, row, u)
    return row, follow, source


async def take(row: dict, u) -> dict:
    """The source's live release onto the copy (the "Update to release N"
    click): the checks, the snapshot, the import held for approval, the
    ``pending`` marker. The answer carries ``release`` (the copy's new
    number), ``source_release`` and ``text``."""
    async with _locked(row["id"]):
        row, follow, source = await _checked(row, u, MODE_UPDATE)
        update = (await asyncio.to_thread(state, row) or {})["update"]
        live = await asyncio.to_thread(_live_dir, source)
        if live is None or not update:
            raise FollowRefused(409, NO_UPDATE.format(source=_display(follow["agent"])))
        n, seeded = await _put(row, source, live)
        follow = dict(parse(row) or follow)
        follow["pending"] = {"release": n, "source_release": update["release"], "tree": _cut_tree(row, n),
                             "tasks": seeded}
        await asyncio.to_thread(_write, row["id"], follow)
        text = (f'Release {n} of "{row.get("title") or row["slug"]}" waits for approval here. It carries release '
                f"{update['release']} of {_display(follow['agent'])}'s app. The app's data stays.")
        return {"release": n, "source_release": update["release"], "text": text}


def staged_dir(row: dict, release: int) -> tuple[str, Path]:
    """Where a merge unpacks the source's release: beside the copy's folder,
    under a name no slug can take (a dot is no slug character)."""
    rel = f"{layout.scope_workspace(row.get('username') or '')}/apps/{row['slug']}.source-r{release}"
    return rel, path_confinement.join_under(config.get_agent_dir(row["agent"]), rel)


async def stage_merge(row: dict, u) -> dict:
    """The source's live release unpacked beside the copy's files for the
    agent to merge (the "Ask the agent to merge" click); the ``staged``
    marker. The answer carries ``release``, ``path`` and ``text``."""
    async with _locked(row["id"]):
        row, follow, source = await _checked(row, u, MODE_MERGE)
        update = (await asyncio.to_thread(state, row) or {})["update"]
        live = await asyncio.to_thread(_live_dir, source)
        if live is None or not update:
            raise FollowRefused(409, NO_UPDATE.format(source=_display(follow["agent"])))
        release = int(update["release"])
        rel, target = staged_dir(row, release)

        def _unpack() -> None:
            manifest = releases.read_manifest(live)
            if manifest is None:
                raise FollowRefused(409, NO_UPDATE.format(source=_display(follow["agent"])))
            agents_root, dest_rel = releases.agents_rel(target)
            safe_fs.rmtree_beneath(agents_root, dest_rel, missing_ok=True)
            for name in sorted(manifest["files"]):
                data = releases.read_release_file(live, name)
                if data is None:
                    continue
                safe_fs.atomic_write_beneath(agents_root, f"{dest_rel}/{name}", data,
                                             mode=releases.RELEASE_FILE_MODE, mkdirs=True, fsync=False)

        try:
            await asyncio.to_thread(_unpack)
        except (OSError, releases.ReleaseDamaged) as exc:
            raise FollowRefused(409, "The source's release could not be unpacked here. Try again.") from exc
        from services.infra import file_bookkeeping
        try:
            await file_bookkeeping.push_tree_write(row["agent"], target, config.get_agent_dir(row["agent"]),
                                                   writer="app-follow")
        except Exception:
            logger.exception("App %s: workspace push failed (continuing)", row.get("slug"))
        follow = dict(parse(row) or follow)
        follow["staged"] = {"release": release, "above": releases.highest_number(row)}
        await asyncio.to_thread(_write, row["id"], follow)
        # The paths as the agent's own session names them.
        title = row.get("title") or row["slug"]
        text = (f"Release {release} of {_display(follow['agent'])}'s app is unpacked at /{rel}. Tell the agent "
                f'to merge it into the app "{title}" at /{row["rel_path"]} and deploy one release.')
        return {"release": release, "path": "/" + rel, "text": text}


async def send_back(row: dict, u) -> dict:
    """The copy's live release onto the source, held for the source's
    approval (the "Send this release to the source" click); the ``sent``
    marker on the copy and the source's note for its reject path. The
    answer carries ``release`` (the number cut on the source) and ``text``."""
    known = parse(await run_db(task_store.get_app, row["id"]) or row)
    target = await asyncio.to_thread(_source_row, known) if known else None
    async with _locked(row["id"], (target or {}).get("id") or ""):
        row, follow, source = await _checked(row, u, MOVE_SEND)
        follow = dict(parse(row) or follow)
        live = await asyncio.to_thread(_live_dir, row)
        if live is None:
            raise FollowRefused(409, NOTHING_TO_SEND)
        n, seeded = await _put(source, row, live)
        follow["sent"] = {"release": n, "copy_release": releases.current_number(row)}
        await asyncio.to_thread(_write, row["id"], follow)
        note = {"release": n, "copy": row["id"], "tree": _cut_tree(source, n), "tasks": seeded}

        def _note() -> None:
            root, rel = releases.agents_rel(_note_path(source))
            safe_fs.atomic_write_beneath(root, rel, json.dumps(note).encode("utf-8"), mkdirs=True, fsync=False)
        await asyncio.to_thread(_note)
        return {"release": n, "text": f"Release {n} of {_display(follow['agent'])}'s app waits for approval there."}


def _cut_tree(row: dict, n: int) -> str:
    """The tree hash of release ``n`` as it was cut: a marker and a note
    name the release by it too, since a number comes back after a rejection
    and a stale entry must never match another release."""
    return releases.tree_sha(releases.app_release_dir(row) / str(n))


def _note_path(row: dict) -> Path:
    return releases.app_release_dir(row) / SENT_BACK_NAME


def _note(row: dict) -> dict:
    """The source's note of a release a copy sent ({} when none)."""
    try:
        note = json.loads(_note_path(row).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return note if isinstance(note, dict) else {}


def _note_release(row: dict) -> int:
    """The release the source's note names (0 when none)."""
    try:
        return int(_note(row).get("release") or 0)
    except (TypeError, ValueError):
        return 0


def _drop_note(row: dict, n: int) -> None:
    """The source's note goes once the release it names went live, was
    rejected or was replaced; a note for another release stays."""
    if _note_release(row) == n:
        with contextlib.suppress(OSError):
            _note_path(row).unlink()


def _clear_sent(copy_id: str, n: int) -> None:
    """The copy's ``sent`` marker for release ``n``, cleared (the source
    rejected it)."""
    copy = task_store.get_app(copy_id) if copy_id else None
    follow = parse(copy) if copy else None
    if follow and int((follow.get("sent") or {}).get("release") or 0) == n:
        follow.pop("sent")
        _write(copy_id, follow)


async def on_rejected(row: dict, n: int, tree: str = "") -> str:
    """After ``reject_pending`` removed release ``n`` (whose tree hash was
    ``tree``): a release another agent's app had put here replaced the
    working folder before anything was decided, so the live release's files
    go back. The copy knows by its ``pending`` marker, the source by the
    note the send-back left; either matches by the number and the tree.
    Answers ``NOT_PUT_BACK`` when the files could not be put back, else ''."""
    follow = parse(row)
    pending = (follow or {}).get("pending") or {}
    as_copy = int(pending.get("release") or 0) == n and pending.get("tree", tree) == tree
    note = await asyncio.to_thread(_note, row)
    as_source = int(note.get("release") or 0) == n and note.get("tree", tree) == tree
    if not as_copy and not as_source:
        return ""
    warning = ""
    live = await asyncio.to_thread(_live_dir, row)
    if live is not None:
        if not await _restore(row, live):
            logger.warning("App %s: the working folder was not put back after release %d was rejected",
                           row.get("slug"), n)
            warning = NOT_PUT_BACK
        await _push(row)
    # The tasks the rejected release's new buttons seeded go with it.
    for task_id in (pending.get("tasks") if as_copy else []) or (note.get("tasks") if as_source else []) or []:
        await asyncio.to_thread(task_store.delete_dynamic_task, str(task_id))
    if as_copy and follow is not None:
        follow.pop("pending", None)
        await asyncio.to_thread(_write, row["id"], follow)
    if as_source:
        await asyncio.to_thread(_drop_note, row, n)
        await asyncio.to_thread(_clear_sent, str(note.get("copy") or ""), n)
    return warning
