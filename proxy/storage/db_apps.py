"""Pinned mini-apps registry queries.

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
from datetime import datetime, timezone

from storage.pg import get_conn

MAX_APPS_PER_SCOPE = 24


def actions_sig(actions_json: str) -> str:
    """sha256 over the canonical manifest serialization. Callers must pass
    the SAME canonical form they persist (json.dumps sort_keys/compact)."""
    return hashlib.sha256(actions_json.encode("utf-8")).hexdigest()


def canonical_actions_json(actions: list) -> str:
    return json.dumps(actions, sort_keys=True, separators=(",", ":"))


def app_actions_approved(row: dict) -> bool:
    actions = row.get("actions") or "[]"
    if json.loads(actions) == []:
        return True  # nothing to approve
    return (row.get("actions_approved_sig") or "") == actions_sig(actions)


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
) -> dict:
    """Create or update the (agent, username, slug) row. ``title`` /
    ``actions_json`` are only written when not None (metadata-only updates
    keep the rest). New rows append at the end of their scope-list;
    ``make_default`` moves the row to position 0 of ITS scope-list only.
    Updating a hidden row UNHIDES it — pin_app on an unpinned slug is the
    restore path (approval intact when the manifest is unchanged)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pinned_apps WHERE agent=%s AND username=%s AND slug=%s",
            (agent, username, slug),
        ).fetchone()
        if row:
            app = dict(row)
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
                    actions, position, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (app_id, agent, owner_sub, username, slug, title or "",
                 rel_path, actions_json or "[]", nxt, now, now),
            )
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
    manifest). A different identity (new slug/agent-scope) replaces the old
    row; the approval sig carries over iff the stored canonical manifest is
    byte-identical (operator decision: replace-on-pin, approval resets iff
    the manifest changed)."""
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
        else:
            new_actions = actions_json if actions_json is not None else "[]"
            carried_sig, carried_by = "", ""
            if old and new_actions == (old.get("actions") or "[]"):
                carried_sig = old.get("actions_approved_sig") or ""
                carried_by = old.get("approved_by") or ""
            if old:
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
    rows always carry ``hidden_for_me=False``."""
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


def set_app_hidden(app_id: str, hidden: bool) -> bool:
    """Dashboard soft-unpin (True) / restore (False). Hiding also prunes the
    scope's OLDEST hidden rows past ``MAX_APPS_PER_SCOPE`` — the parked set
    is bounded by the same number the visible set is."""
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
            conn.execute(
                """DELETE FROM pinned_apps WHERE id IN (
                       SELECT id FROM pinned_apps
                       WHERE agent=%s AND username=%s AND hidden
                       ORDER BY updated_at DESC
                       OFFSET %s
                   )""",
                (row["agent"], row["username"], MAX_APPS_PER_SCOPE),
            )
        conn.commit()
        return True


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
            "SELECT actions FROM pinned_apps WHERE id=%s", (app_id,),
        ).fetchone()
        if not row or actions_sig(row["actions"] or "[]") != sig:
            return False
        conn.execute(
            "UPDATE pinned_apps SET actions_approved_sig=%s, approved_by=%s "
            "WHERE id=%s",
            (sig, approved_by, app_id),
        )
        conn.commit()
        return True


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
