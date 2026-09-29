"""Pinned apps registry queries.

Part of the ``storage.database`` facade; import names from
``storage.database`` rather than this module directly. All functions are
synchronous (called via ``asyncio.to_thread`` from async code).

A row is the REGISTRY entry for a standing agent-authored dashboard; the
HTML itself is a workspace file at ``rel_path``. ``username == ""`` /
``owner_sub IS NULL`` marks a shared row (see ``schema.init_pinned_apps``
for why NULL, not ``''``). Approval state is derived, never stored as a
flag: actions are approved iff ``actions_approved_sig`` equals the sha256
of the CURRENT canonical manifest — editing the manifest silently breaks
the sig, which is the intended kill-switch.

``hidden`` rows are the dashboard's SOFT unpin: invisible to viewers (and
to the per-scope cap) but the manifest + approval survive, so an agent
re-pin of the same slug restores the app exactly as approved. Any upsert
unhides. Hidden rows per scope are bounded (oldest hard-deleted) so an
X-happy user can't grow the table without bound.

SCOPED rows (``scope_chat_id`` / ``scope_project_id`` set — the Dock) are
per-chat / per-project dashboards: excluded from the standing app list and
the standing cap (they're one-per-scope by the partial unique indexes), and
for them the SCOPE is the identity — a re-pin on an occupied scope REPLACES
the row even under a new slug (approval carries iff the manifest is
byte-identical). Everything else (serve route, approval sig, soft-hide)
rides the same row shape.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from auth import roles
from storage.pg import get_conn

# ---------------------------------------------------------------------------
# The app kind (core-seams phase 9): ``pinned_apps.kind`` is ``file`` (one
# html document, the column's default) or ``folder`` (a tree with releases
# addressed by hash, a database and maybe a server — APPS.md). Generic code
# asks the capability it means of ``app_kind_of(row)``; the dashboard
# mirror is ``dashboard/src/lib/kinds/app.ts`` (``tests/core/test_kinds.py``
# binds it). The names are distinctive because ``storage.database``
# star-imports this module.
# ---------------------------------------------------------------------------

APP_KIND_FILE = "file"
APP_KIND_FOLDER = "folder"


@dataclass(frozen=True)
class AppKind:
    """One app kind and what a row of it can do."""
    name: str
    serves_tree: bool        # releases addressed by tree hash, a preview copy, a document by hash
    keeps_data: bool         # a database that rolls back and is deleted with the app
    may_serve: bool          # may run a server: inbound routes, logs, handlers, triggers, the viewer token
    has_settings: bool       # secrets set by a person (the Settings panel)
    has_preview_build: bool  # the working copy can be viewed as a preview build
    deletable: bool          # "Delete app and its data": the app, its releases and its database


APP_KINDS: dict[str, AppKind] = {k.name: k for k in (
    AppKind(APP_KIND_FILE, serves_tree=False, keeps_data=False, may_serve=False, has_settings=False,
            has_preview_build=False, deletable=False),
    AppKind(APP_KIND_FOLDER, serves_tree=True, keeps_data=True, may_serve=True, has_settings=True,
            has_preview_build=True, deletable=True),
)}


def app_kind_of(row: dict | None) -> AppKind:
    """The kind's facts for an app row; a missing, empty or unknown ``kind``
    is the column's default, ``file``."""
    kind = (row or {}).get("kind") or APP_KIND_FILE
    return APP_KINDS.get(kind, APP_KINDS[APP_KIND_FILE])


# An app row's scope, as the API and the claims spell it: a personal app
# lives under its owner's username, a shared one under the agent alone.
APP_SCOPE_PERSONAL = "personal"
APP_SCOPE_SHARED = "shared"


def app_scope(username: str | None) -> str:
    """The scope label of an app row (or a pin, a usage row) by the
    ``username`` it carries."""
    return APP_SCOPE_PERSONAL if username else APP_SCOPE_SHARED

MAX_APPS_PER_SCOPE = 24


def actions_sig(actions_json: str) -> str:
    """sha256 over the canonical manifest serialization. Callers must pass
    the SAME canonical form they persist (json.dumps sort_keys/compact)."""
    return hashlib.sha256(actions_json.encode("utf-8")).hexdigest()


def canonical_actions_json(actions: list) -> str:
    return json.dumps(actions, sort_keys=True, separators=(",", ":"))


# The blocks of the whole manifest besides ``actions`` (APPS.md "The
# signed manifest"), signed from the day a row holds one. ``requires``
# (what the app needs to work) is a column too and grants a page nothing,
# so it is written with the blocks and left out of the signature — except
# on a row with ``steps``: a step script receives the tokens of the
# providers it names (``app_steps.credential_env_for``). ``steps``
# (APPS.md "Steps", 2026-09-17) carries each script's sha256, so the
# signature covers the script's content, not only its name. ``secrets``
# (APPS.md "Secrets") carries names, whether each is required and where
# the platform sends it — never a value — so a renamed or newly required
# secret voids the approval and a value change never touches it. ``inbound``
# (APPS.md "Inbound hooks") names the public routes a vendor may call, the
# scheme and the secret each is verified with, and the handler it wakes.
# ``external`` (APPS.md "External links") says what a link may do: the
# hosts the host page may open, the paths that need a challenge, the
# session cookie's lifetime.
MANIFEST_BLOCKS = ("catalog", "files", "egress", "handlers", "exports", "bindings", "steps",
                   "secrets", "inbound", "external")
UNSIGNED_BLOCKS = ("requires",)


def _manifest_blocks(row: dict) -> dict:
    blocks: dict = {}
    for name in (*MANIFEST_BLOCKS, "requires"):
        if name == "requires" and "steps" not in blocks:
            continue
        raw = row.get(name)
        if raw in (None, "", "[]", "{}", [], {}):
            continue
        blocks[name] = json.loads(raw) if isinstance(raw, str) else raw
    return blocks


def canonical_manifest(row: dict) -> str:
    """The signed text of a row's manifest: the canonical actions list alone
    while every other block is empty (so an approval recorded before the
    other blocks existed keeps matching), else the canonical object
    ``{actions, catalog, files, egress, handlers, exports, bindings}`` with
    the non-empty blocks. Adding a block therefore voids the approval, and
    removing every block returns to the list form, which never equals an
    object's signature."""
    actions = row.get("actions") or "[]"
    if not isinstance(actions, str):
        actions = canonical_actions_json(actions)
    blocks = _manifest_blocks(row)
    if not blocks:
        return actions
    return json.dumps({"actions": json.loads(actions), **blocks},
                      sort_keys=True, separators=(",", ":"))


def manifest_sig(row: dict) -> str:
    return actions_sig(canonical_manifest(row))


def app_actions_approved(row: dict) -> bool:
    if canonical_manifest(row) == "[]":
        return True  # nothing to approve
    return (row.get("actions_approved_sig") or "") == manifest_sig(row)


def upsert_app(
    agent: str,
    username: str,
    owner_sub: str | None,
    slug: str,
    *,
    title: str | None = None,
    rel_path: str = "",
    actions_json: str | None = None,
    make_default: bool = False,
    kind: str | None = None,
    blocks: dict[str, str] | None = None,
    template_ref: str | None = None,
) -> dict:
    """Create or update the (agent, username, slug) row. ``title`` /
    ``actions_json`` are only written when not None (metadata-only updates
    keep the rest). New rows append at the end of their scope-list;
    ``make_default`` moves the row to position 0 of ITS scope-list only.
    Updating a hidden row UNHIDES it — pin_app on an unpinned slug is the
    restore path (approval intact when the manifest is unchanged) — and
    clears a template row's hidden reason with it. ``kind`` (``file`` |
    ``folder``), ``blocks`` (the signed manifest blocks besides the
    actions, canonical JSON text per column name) and ``template_ref`` (the
    template a seeded row came from) are written only when given."""
    now = datetime.now(timezone.utc).isoformat()
    block_sets = [(k, v) for k, v in (blocks or {}).items()
                  if k in MANIFEST_BLOCKS or k in UNSIGNED_BLOCKS]
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pinned_apps WHERE agent=%s AND username=%s AND slug=%s",
            (agent, username, slug),
        ).fetchone()
        if row:
            app = dict(row)
            sets = ["updated_at=%s", "hidden=FALSE", "template_state=''"]
            vals: list = [now]
            if title is not None:
                sets.append("title=%s")
                vals.append(title)
            if actions_json is not None:
                sets.append("actions=%s")
                vals.append(actions_json)
            if rel_path:
                sets.append("rel_path=%s")
                vals.append(rel_path)
            if kind:
                sets.append("kind=%s")
                vals.append(kind)
            if template_ref is not None:
                sets.append("template_ref=%s")
                vals.append(template_ref)
            for col, text in block_sets:
                sets.append(f"{col}=%s")
                vals.append(text)
            vals.append(app["id"])
            conn.execute(
                f"UPDATE pinned_apps SET {', '.join(sets)} WHERE id=%s", vals,
            )
            app_id = app["id"]
        else:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(position)+1, 0) AS p FROM pinned_apps "
                "WHERE agent=%s AND username=%s",
                (agent, username),
            ).fetchone()["p"]
            app_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO pinned_apps
                   (id, agent, owner_sub, username, slug, title, rel_path,
                    actions, position, kind, template_ref, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (app_id, agent, owner_sub, username, slug, title or "",
                 rel_path, actions_json or "[]", nxt, kind or "file",
                 template_ref or "", now, now),
            )
            for col, text in block_sets:
                conn.execute(f"UPDATE pinned_apps SET {col}=%s WHERE id=%s", (text, app_id))
        if make_default:
            conn.execute(
                "UPDATE pinned_apps SET position = position + 1 "
                "WHERE agent=%s AND username=%s AND id != %s",
                (agent, username, app_id),
            )
            conn.execute(
                "UPDATE pinned_apps SET position = 0 WHERE id=%s", (app_id,),
            )
        out = dict(conn.execute(
            "SELECT * FROM pinned_apps WHERE id=%s", (app_id,),
        ).fetchone())
        conn.commit()
        return out


def get_scoped_app(*, chat_id: str = "", project_id: str = "") -> dict | None:
    """The scope's pin row (exactly one of chat_id/project_id). Hidden rows
    included — the caller decides whether a soft-hidden pin counts (the pin
    hook replaces it; viewer surfaces skip it)."""
    col = "scope_chat_id" if chat_id else "scope_project_id"
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM pinned_apps WHERE {col}=%s",
            (chat_id or project_id,),
        ).fetchone()
        return dict(row) if row else None


def upsert_scoped_app(
    agent: str,
    username: str,
    owner_sub: str | None,
    slug: str,
    *,
    scope_chat_id: str = "",
    scope_project_id: str = "",
    title: str | None = None,
    rel_path: str = "",
    actions_json: str | None = None,
) -> dict:
    """Create or REPLACE the scope's pin (scope is the identity, slug is
    cosmetic). Same-identity re-pin updates in place like ``upsert_app``
    (None fields keep current values; approval survives an unchanged
    manifest). A new slug for the same owner re-points the row in place;
    another owner or agent replaces it with a new row, so nothing of the
    old owner's (the state document, hides, shares) passes to the new one.
    Either way the approval sig carries over iff the stored canonical
    manifest is byte-identical (operator decision: replace-on-pin, approval
    resets iff the manifest changed)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        col = "scope_chat_id" if scope_chat_id else "scope_project_id"
        scope_id = scope_chat_id or scope_project_id
        old = conn.execute(
            f"SELECT * FROM pinned_apps WHERE {col}=%s", (scope_id,),
        ).fetchone()
        old = dict(old) if old else None
        if old and old["agent"] == agent and old["username"] == username \
                and old["slug"] == slug:
            sets = ["updated_at=%s", "hidden=FALSE"]
            vals: list = [now]
            if title is not None:
                sets.append("title=%s")
                vals.append(title)
            if actions_json is not None:
                sets.append("actions=%s")
                vals.append(actions_json)
            if rel_path:
                sets.append("rel_path=%s")
                vals.append(rel_path)
            vals.append(old["id"])
            conn.execute(
                f"UPDATE pinned_apps SET {', '.join(sets)} WHERE id=%s", vals,
            )
            app_id = old["id"]
        elif old and old["agent"] == agent and old["username"] == username:
            # A new slug from the same owner on an occupied scope: the row
            # is re-pointed IN PLACE (the id, the state document, the
            # per-user hides and the shares survive a slug change); the
            # approval carries iff the stored canonical manifest is
            # byte-identical.
            new_actions = actions_json if actions_json is not None else "[]"
            carried_sig, carried_by = "", ""
            if new_actions == (old.get("actions") or "[]"):
                carried_sig = old.get("actions_approved_sig") or ""
                carried_by = old.get("approved_by") or ""
            conn.execute(
                """UPDATE pinned_apps SET agent=%s, owner_sub=%s, username=%s,
                   slug=%s, title=%s, rel_path=%s, actions=%s,
                   actions_approved_sig=%s, approved_by=%s, hidden=FALSE,
                   updated_at=%s WHERE id=%s""",
                (agent, owner_sub, username, slug, title or slug, rel_path,
                 new_actions, carried_sig, carried_by, now, old["id"]),
            )
            app_id = old["id"]
        else:
            new_actions = actions_json if actions_json is not None else "[]"
            carried_sig, carried_by = "", ""
            if old:
                # Another owner or agent takes the scope: a new row, the
                # old one and everything keyed on its id deleted.
                if new_actions == (old.get("actions") or "[]"):
                    carried_sig = old.get("actions_approved_sig") or ""
                    carried_by = old.get("approved_by") or ""
                conn.execute("DELETE FROM pinned_apps WHERE id=%s", (old["id"],))
            app_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO pinned_apps
                   (id, agent, owner_sub, username, slug, title, rel_path,
                    actions, actions_approved_sig, approved_by,
                    scope_chat_id, scope_project_id,
                    position, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)""",
                (app_id, agent, owner_sub, username, slug, title or slug,
                 rel_path, new_actions, carried_sig, carried_by,
                 scope_chat_id or None, scope_project_id or None, now, now),
            )
        out = dict(conn.execute(
            "SELECT * FROM pinned_apps WHERE id=%s", (app_id,),
        ).fetchone())
        conn.commit()
        return out


def app_is_scoped(row: dict) -> bool:
    return bool(row.get("scope_chat_id") or row.get("scope_project_id"))


def owner_holds_agent(agent: str, owner_sub: str) -> bool:
    """Whether a personal app's owner still stands on its agent: a platform
    admin, or a membership row (a row whose role is NULL is a viewer, as
    ``get_user_agent_roles`` reads it). Fail-closed: no sub or no user is
    False."""
    if not owner_sub or not agent:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.role, ua.sub IS NOT NULL AS member, "
            "COALESCE(ua.agent_role, 'viewer') AS agent_role "
            "FROM users u LEFT JOIN user_agents ua ON ua.sub = u.sub AND ua.agent = %s "
            "WHERE u.sub = %s",
            (agent, owner_sub),
        ).fetchone()
    if not row:
        return False
    rows = {agent: row["agent_role"]} if row["member"] else {}
    return bool(roles.effective_role(row["role"], rows, agent))


def personal_row_dormant(row: dict) -> bool:
    """A personal app whose owner lost its agent (APPS.md "Lifecycle"): every
    surface answers as for a missing app until the owner is back."""
    return bool(row.get("username")) and not owner_holds_agent(
        row.get("agent") or "", row.get("owner_sub") or "")


def get_app(app_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pinned_apps WHERE id=%s", (app_id,),
        ).fetchone()
        return dict(row) if row else None


def get_app_by_slug(agent: str, username: str, slug: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pinned_apps WHERE agent=%s AND username=%s AND slug=%s",
            (agent, username, slug),
        ).fetchone()
        return dict(row) if row else None


_UNSCOPED = "scope_chat_id IS NULL AND scope_project_id IS NULL"


def list_apps(agent: str, username: str = "", include_hidden: bool = False,
              viewer_sub: str = "") -> list[dict]:
    """The viewer's merged STANDING list: shared rows first, then the
    viewer's own personal rows, each group by position. ``username=""``
    returns only the shared group (agent-scope callers). order[0] is the
    default tab. Hidden (soft-unpinned) rows are excluded unless
    ``include_hidden`` — the agent-facing list hook passes True so
    re-pinnable slugs surface. Scoped (chat/project) rows never appear here
    — they surface on their scope's Dock (``list_scoped_apps`` for the
    agent-facing merged view).

    ``viewer_sub`` (S2): stamp each SHARED row with ``hidden_for_me`` from
    ``pinned_app_user_hides`` — the rows still return (the dashboard's
    hidden-affordance needs them); the API layer/client segregate. Personal
    rows always carry ``hidden_for_me=False``. With ``viewer_sub`` and a
    ``username`` the list ends with a third group: other users' personal
    apps the viewer holds a live internal share on (``granted`` set,
    ``hidden_for_me`` from the share row; SHARING.md)."""
    hid = "" if include_hidden else "AND NOT hidden "
    hide_col = (", EXISTS(SELECT 1 FROM pinned_app_user_hides h "
                "WHERE h.app_id = pinned_apps.id AND h.user_sub = %s) "
                "AS hidden_for_me" if viewer_sub else "")
    with get_conn() as conn:
        shared_params: list = ([viewer_sub] if viewer_sub else []) + [agent]
        shared = conn.execute(
            f"SELECT *{hide_col} FROM pinned_apps "
            f"WHERE agent=%s AND username='' {hid}"
            f"AND {_UNSCOPED} ORDER BY position, created_at",
            shared_params,
        ).fetchall()
        rows = [dict(r) for r in shared]
        if not viewer_sub:
            for r in rows:
                r["hidden_for_me"] = False
        if username:
            own = conn.execute(
                f"SELECT * FROM pinned_apps WHERE agent=%s AND username=%s {hid}"
                f"AND {_UNSCOPED} ORDER BY position, created_at",
                (agent, username),
            ).fetchall()
            for r in own:
                d = dict(r)
                d["hidden_for_me"] = False
                rows.append(d)
    if username and viewer_sub and not include_hidden:
        from storage.sharing import share_store
        rows += share_store.granted_app_rows(agent, viewer_sub)
    return rows


def list_scoped_apps(agent: str, username: str = "") -> list[dict]:
    """The caller-scope CHAT/PROJECT pins (shared + the viewer's personal
    ones), newest first — the agent-facing list hook appends these under the
    standing list so slugs are reused deliberately across scopes too."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pinned_apps WHERE agent=%s "
            "AND (username='' OR username=%s) "
            f"AND NOT ({_UNSCOPED}) ORDER BY updated_at DESC",
            (agent, username),
        ).fetchall()
        return [dict(r) for r in rows]


def count_apps(agent: str, username: str) -> int:
    """VISIBLE STANDING rows only — this feeds the per-scope pin cap, and
    the cap's "unpin one first" advice must actually free a slot (dashboard
    unpin hides; hidden rows have their own bound in ``set_app_hidden``).
    Scoped pins don't count: they're one-per-scope by construction."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM pinned_apps "
            f"WHERE agent=%s AND username=%s AND NOT hidden AND {_UNSCOPED}",
            (agent, username),
        ).fetchone()["c"]


def delete_app(app_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM pinned_apps WHERE id=%s", (app_id,))
        conn.commit()
        return cur.rowcount > 0


def list_template_rows(agent: str, username: str) -> list[dict]:
    """The rows a template seeded for one scope (hidden ones included)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pinned_apps WHERE agent=%s AND username=%s AND template_ref <> ''",
            (agent, username),
        ).fetchall()
        return [dict(r) for r in rows]


def list_rows_by_template_ref(agent: str, template_ref: str) -> list[dict]:
    """Every copy one template app seeded on an agent, across the members
    (hidden ones included): what a template update walks."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pinned_apps WHERE agent=%s AND template_ref=%s ORDER BY username",
            (agent, template_ref),
        ).fetchall()
        return [dict(r) for r in rows]


def set_template_state(app_id: str, state: str, *, hidden: bool | None = None) -> dict | None:
    """A template row's hidden reason (COMMUNITY-AGENTS-REGISTRY.md
    "Per-user template apps"): ``opted_out`` (the owner purged or unpinned
    it; no seed, reseed or update recreates it), ``removed`` (a membership
    removal; re-attach restores it), ``''`` (in use). ``hidden`` moves with
    it when given; an opted-out row also loses its release pointer and its
    pending copy, since both are gone from disk."""
    sets = ["template_state=%s"]
    vals: list = [state]
    if hidden is not None:
        sets.append("hidden=%s")
        vals.append(hidden)
    if state == "opted_out":
        sets += ["release_path=''", "release_sha256=''", "deploy_state=%s", "pending_release=0"]
        vals.append(DEPLOY_IDLE)
    with get_conn() as conn:
        conn.execute(f"UPDATE pinned_apps SET {', '.join(sets)} WHERE id=%s", [*vals, app_id])
        row = conn.execute("SELECT * FROM pinned_apps WHERE id=%s", (app_id,)).fetchone()
        conn.commit()
        return dict(row) if row else None


def set_app_hidden(app_id: str, hidden: bool, *, pruned: list[dict] | None = None) -> bool:
    """Dashboard soft-unpin (True) / restore (False). Hiding also prunes the
    scope's OLDEST hidden rows past ``MAX_APPS_PER_SCOPE`` — the parked set
    is bounded by the same number the visible set is. The pruned rows are
    appended to ``pruned`` when given, so the caller can remove what the
    database does not own (their release directories)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT agent, username FROM pinned_apps WHERE id=%s", (app_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE pinned_apps SET hidden=%s WHERE id=%s", (hidden, app_id),
        )
        if hidden:
            # The app's shares pause with it and stay paused until the sharer
            # resumes them (a re-pin restores the row, not the shares).
            from storage.sharing import share_store
            share_store.suspend_app_shares(conn, app_id)
            # A template's copy is never pruned: its row is the member's
            # opt-out (or their parked copy), and a missing row is re-seeded.
            gone = conn.execute(
                """DELETE FROM pinned_apps WHERE id IN (
                       SELECT id FROM pinned_apps
                       WHERE agent=%s AND username=%s AND hidden AND template_ref=''
                       ORDER BY updated_at DESC
                       OFFSET %s
                   ) RETURNING id, agent, username, slug, release_path""",
                (row["agent"], row["username"], MAX_APPS_PER_SCOPE),
            ).fetchall()
            if pruned is not None:
                pruned.extend(dict(g) for g in gone)
        conn.commit()
        return True


# ── Releases (APPS.md "Releases and rollback") ────────────────────────


def set_app_release(app_id: str, release_path: str, release_sha256: str) -> dict | None:
    """Point the row at a release copy; returns the fresh row."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE pinned_apps SET release_path=%s, release_sha256=%s, updated_at=%s "
            "WHERE id=%s",
            (release_path, release_sha256, now, app_id),
        )
        row = conn.execute("SELECT * FROM pinned_apps WHERE id=%s", (app_id,)).fetchone()
        conn.commit()
        return dict(row) if row else None


# The persisted deploy state (``pinned_apps.deploy_state``, named once —
# core-seams phase 8; the dashboard mirror is ``lib/status/appDeploy.ts``):
# ``idle`` (nothing parked; the column default) or ``pending`` (a release
# waits on the card for approval). The deploy ANSWER (``app_deploy.RESULT_*``)
# is a different vocabulary. ``set_deploy_state`` refuses another word.
DEPLOY_IDLE = "idle"
DEPLOY_PENDING = "pending"
DEPLOY_STATES: frozenset[str] = frozenset({DEPLOY_IDLE, DEPLOY_PENDING})


def set_deploy_state(app_id: str, *, deploy_state: str | None = None,
                     pending_release: int | None = None,
                     requires_approval: bool | None = None) -> dict | None:
    """The folder-app deploy columns (APPS.md): only the given ones move.
    Returns the fresh row."""
    sets: list[str] = []
    vals: list = []
    if deploy_state is not None:
        if deploy_state not in DEPLOY_STATES:
            raise ValueError(f"invalid deploy state: {deploy_state!r}")
        sets.append("deploy_state=%s")
        vals.append(deploy_state)
    if pending_release is not None:
        sets.append("pending_release=%s")
        vals.append(int(pending_release))
    if requires_approval is not None:
        sets.append("deploy_requires_approval=%s")
        vals.append(bool(requires_approval))
    with get_conn() as conn:
        if sets:
            conn.execute(f"UPDATE pinned_apps SET {', '.join(sets)} WHERE id=%s", [*vals, app_id])
        row = conn.execute("SELECT * FROM pinned_apps WHERE id=%s", (app_id,)).fetchone()
        conn.commit()
        return dict(row) if row else None


def clear_app_release(app_id: str) -> None:
    """Back to the working file (the boot reconcile, a release gone from disk)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pinned_apps SET release_path='', release_sha256='' WHERE id=%s",
            (app_id,),
        )
        conn.commit()


def list_released_apps() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, agent, username, slug, kind, release_path, release_sha256 "
            "FROM pinned_apps WHERE release_path <> ''",
        ).fetchall()
        return [dict(r) for r in rows]


def list_apps_with_handlers() -> list[dict]:
    """Every visible row that declares handlers (the event index of
    services/apps/app_handlers.py; approval is judged by the caller)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pinned_apps WHERE handlers <> '' AND NOT hidden",
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_app_ids() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT id FROM pinned_apps").fetchall()]


def hidden_for_users(app_id: str) -> set[str]:
    """The users who parked this shared app off their own strip (S2)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_sub FROM pinned_app_user_hides WHERE app_id=%s", (app_id,),
        ).fetchall()
    return {r["user_sub"] for r in rows}


def is_hidden_for_user(app_id: str, user_sub: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM pinned_app_user_hides WHERE app_id=%s AND user_sub=%s",
            (app_id, user_sub),
        ).fetchone()
    return row is not None


def hide_app_for_user(app_id: str, user_sub: str) -> None:
    """S2 hide-for-me: park a SHARED app off THIS user's strip only. The
    row/manifest/approval and every other user's view are untouched.
    Idempotent (re-hide is a no-op). The caller validates the app is a
    shared standing row."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO pinned_app_user_hides (app_id, user_sub, created_at)
               VALUES (%s, %s, %s) ON CONFLICT (app_id, user_sub) DO NOTHING""",
            (app_id, user_sub, now),
        )
        conn.commit()


def unhide_app_for_user(app_id: str, user_sub: str) -> bool:
    """Restore a hide-for-me. Returns True if a hide row existed."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM pinned_app_user_hides WHERE app_id=%s AND user_sub=%s",
            (app_id, user_sub),
        )
        conn.commit()
        return cur.rowcount > 0


def approve_app_actions(app_id: str, sig: str, approved_by: str) -> bool:
    """Set the approval sig iff it matches the CURRENT manifest — the caller
    sends the sig it rendered, so a manifest mutated after the approval card
    was shown is refused (the approve-then-mutate race)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pinned_apps WHERE id=%s", (app_id,),
        ).fetchone()
        if not row or manifest_sig(dict(row)) != sig:
            return False
        conn.execute(
            "UPDATE pinned_apps SET actions_approved_sig=%s, approved_by=%s "
            "WHERE id=%s",
            (sig, approved_by, app_id),
        )
        conn.commit()
        return True


def clear_app_approval(app_id: str) -> None:
    """Void the approval outright (an imported blueprint is never
    pre-approved, APPS.md "Blueprints"): the column is ``NOT NULL DEFAULT
    ''``, so "no approval" is the empty signature, and ``upsert_app`` never
    touches it on its own."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE pinned_apps SET actions_approved_sig='', approved_by='' WHERE id=%s",
            (app_id,),
        )
        conn.commit()


def set_app_positions(updates: list[tuple[str, int]]) -> None:
    """Apply (app_id, position) pairs — the API layer computes per-scope
    numbering; concurrent reorders are last-write-wins by design."""
    if not updates:
        return
    with get_conn() as conn:
        for app_id, pos in updates:
            conn.execute(
                "UPDATE pinned_apps SET position=%s WHERE id=%s", (pos, app_id),
            )
        conn.commit()


# ── The state document (APPS.md "Live apps") ──────────────────────────
# One JSON object per app row, written by the agent, read by every viewer's
# page. The limits keep a document deliverable in one WS frame and one
# postMessage; the merge is RFC 7386 so a patch never has to carry the parts
# it leaves alone.

STATE_DOC_MAX_BYTES = 64 * 1024
STATE_DOC_MAX_DEPTH = 16
STATE_KEY_MAX_CHARS = 128


class AppStateError(ValueError):
    """A state write the platform refuses; the message is user-facing."""


def canonical_state_json(doc: dict) -> str:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def merge_patch(target, patch):
    """RFC 7386: an object patch merges key by key, ``null`` deletes the
    key, anything else replaces the value."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def check_state_doc(doc) -> None:
    """Raise AppStateError unless ``doc`` is an object within the limits."""
    if not isinstance(doc, dict):
        raise AppStateError("the state document must be a JSON object")

    def _walk(node, depth: int) -> None:
        if depth > STATE_DOC_MAX_DEPTH:
            raise AppStateError(
                f"the state document nests deeper than {STATE_DOC_MAX_DEPTH} levels")
        if isinstance(node, dict):
            for key, value in node.items():
                if len(str(key)) > STATE_KEY_MAX_CHARS:
                    raise AppStateError(
                        f"a state key is longer than {STATE_KEY_MAX_CHARS} characters")
                _walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                _walk(value, depth + 1)

    _walk(doc, 1)
    if len(canonical_state_json(doc).encode("utf-8")) > STATE_DOC_MAX_BYTES:
        raise AppStateError(
            f"the state document exceeds {STATE_DOC_MAX_BYTES // 1024} KB")


def _state_doc(raw) -> dict:
    try:
        doc = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def get_app_state(app_id: str) -> tuple[dict, int]:
    """``(doc, rev)``; ``({}, 0)`` for an app that was never written."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT doc, rev FROM app_state WHERE app_id=%s", (app_id,),
        ).fetchone()
    if not row:
        return {}, 0
    return _state_doc(row["doc"]), int(row["rev"] or 0)


def write_app_state(app_id: str, patch, *, replace: bool = False,
                    updated_by: str = "") -> tuple[dict, int]:
    """Merge ``patch`` into the app's document (or replace it whole) under
    a row lock, so two writes a moment apart never lose each other's keys.
    Returns the new document and its rev. Raises AppStateError for a
    document the platform refuses and LookupError when the app row is
    gone."""
    from psycopg import errors as pg_errors
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        # The lock row first, so two first writes serialise on it instead of
        # racing the insert; the FK refuses a row for a deleted app.
        try:
            conn.execute(
                "INSERT INTO app_state (app_id, doc, rev, updated_at, updated_by) "
                "VALUES (%s, '{}', 0, %s, %s) ON CONFLICT (app_id) DO NOTHING",
                (app_id, now, updated_by),
            )
        except pg_errors.ForeignKeyViolation:
            raise LookupError("app not found") from None
        row = conn.execute(
            "SELECT doc, rev FROM app_state WHERE app_id=%s FOR UPDATE", (app_id,),
        ).fetchone()
        if row is None:
            raise LookupError("app not found")
        current = _state_doc(row["doc"])
        new_doc = patch if replace else merge_patch(current, patch)
        check_state_doc(new_doc)
        rev = int(row["rev"] or 0) + 1
        conn.execute(
            "UPDATE app_state SET doc=%s, rev=%s, updated_at=%s, updated_by=%s "
            "WHERE app_id=%s",
            (canonical_state_json(new_doc), rev, now, updated_by, app_id),
        )
        conn.commit()
        return new_doc, rev
