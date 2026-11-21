"""The external identities picks of an agent (``agent_outside``,
``agent_outside_tools``, ``agent_outside_apps``): what its managers picked
for a caller on its phone line or another person who is not a member, and
what happens after a conversation with one. The tool rows also hold the
agent's tool limits (``services/mcp/tool_limits.py``): ``governs`` says
which sessions a row is for. Synchronous (the DB executor); the access
words are ``services/mcp/outside_picks.py``'s.
"""

import json
from datetime import datetime, timezone

from storage.pg import get_conn

#: Which sessions a tool row governs: an outside person's (the pick that
#: opens a tool to them), the agent's own runs (no person behind the
#: session) or people's chats (a person behind it, their tasks included).
OUTSIDE = "outside"
RUNS = "runs"
CHATS = "chats"
GOVERNS = (OUTSIDE, RUNS, CHATS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _names(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def get_tool_picks(agent_name: str, *, governs: str = OUTSIDE) -> dict[str, dict]:
    """``{mcp_name: {access, functions, granted, changed_at}}`` of the rows
    that govern ``governs`` sessions."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, access, functions, granted, changed_at "
            "FROM agent_outside_tools WHERE agent_name = %s AND governs = %s ORDER BY mcp_name",
            (agent_name, governs),
        ).fetchall()
    return {
        r["mcp_name"]: {
            "access": r["access"],
            "functions": _names(r["functions"]),
            "granted": _names(r["granted"]),
            "changed_at": r["changed_at"] or "",
        }
        for r in rows
    }


def get_app_picks(agent_name: str) -> dict[str, dict]:
    """``{app_id: {methods, changed_at}}``: the apps opened to outside
    people, with the methods they opened when picked."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT app_id, methods, changed_at FROM agent_outside_apps WHERE agent_name = %s ORDER BY app_id",
            (agent_name,),
        ).fetchall()
    return {r["app_id"]: {"methods": _names(r["methods"]), "changed_at": r["changed_at"] or ""} for r in rows}


def agents_with_app(app_id: str) -> list[str]:
    """The agents whose picks open ``app_id`` to outside people."""
    with get_conn() as conn:
        rows = conn.execute("SELECT agent_name FROM agent_outside_apps WHERE app_id = %s ORDER BY agent_name",
                            (app_id,)).fetchall()
    return [r["agent_name"] for r in rows]


def mark_app_changed(agent_name: str, app_id: str) -> None:
    """A later release changed the methods a picked app opens to outside
    people: the row keeps the methods seen at the pick and the time."""
    with get_conn() as conn:
        conn.execute("UPDATE agent_outside_apps SET changed_at = %s WHERE agent_name = %s AND app_id = %s",
                     (_now(), agent_name, app_id))
        conn.commit()


def get_settings(agent_name: str) -> dict:
    """``{files_access, web, knowledge}``; the defaults when no row exists
    (the person's files read only, no web, no knowledge folder)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT files_access, web, knowledge FROM agent_outside WHERE agent_name = %s",
            (agent_name,),
        ).fetchone()
    if not row:
        return {"files_access": "read_only", "web": False, "knowledge": []}
    return {"files_access": row["files_access"], "web": bool(row["web"]),
            "knowledge": _names(row["knowledge"])}


def get_wrapup(agent_name: str) -> dict:
    """``{wrapup_enabled, shared_notes, wrapup_model}``: whether the agent
    writes down what it learned after a conversation (on when no row
    exists), whether that keeps a note for the team (off) and its model
    ('' = the agent's)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT wrapup_enabled, shared_notes, wrapup_model FROM agent_outside WHERE agent_name = %s",
            (agent_name,),
        ).fetchone()
    if not row:
        return {"wrapup_enabled": True, "shared_notes": False, "wrapup_model": ""}
    return {"wrapup_enabled": bool(row["wrapup_enabled"]), "shared_notes": bool(row["shared_notes"]),
            "wrapup_model": row["wrapup_model"] or ""}


def _covered(pick: str, by: str) -> bool:
    """Whether folder ``pick`` lies inside folder ``by``, segment by segment."""
    return pick.startswith(by + "/")


def _merged(current: list[str], add: list[str], remove: list[str]) -> list[str]:
    """The stored folder list after a change: sorted, each once, and no
    folder inside another (a folder added over its subfolders replaces
    them; one added under a folder already there adds nothing)."""
    kept = set(current) - set(remove)
    kept |= set(add)
    return sorted(k for k in kept if not any(_covered(k, other) for other in kept if other != k))


def update(
    agent_name: str, *,
    upsert_tools: list[dict] | None = None,
    remove_tools: list | None = None,
    files_access: str | None = None,
    web: bool | None = None,
    add_knowledge: list[str] | None = None,
    remove_knowledge: list[str] | None = None,
    shared_notes: bool | None = None,
    wrapup_model: str | None = None,
    wrapup_enabled: bool | None = None,
    add_apps: list[dict] | None = None,
    remove_apps: list[str] | None = None,
) -> None:
    """Change the picks, in one transaction; None leaves a field as it is.
    ``upsert_tools`` rows are ``{mcp_name, access, functions, granted,
    governs}`` (``governs`` absent is an outside pick): a row whose access
    and functions are unchanged stays as it is (what Read only gave and when
    a later listing changed it), any other is written afresh with its stamp
    cleared. ``remove_tools`` names an outside pick by its MCP name, or any
    row as ``(mcp_name, governs)``. ``add_apps`` rows are ``{app_id,
    methods}``: an app added again is written afresh, its stamp cleared.
    The caller has checked what it adds."""
    settings = {"files_access": files_access, "web": None if web is None else bool(web),
                "shared_notes": None if shared_notes is None else bool(shared_notes),
                "wrapup_model": wrapup_model,
                "wrapup_enabled": None if wrapup_enabled is None else bool(wrapup_enabled)}
    with get_conn() as conn:
        for item in remove_tools or ():
            name, governs = item if isinstance(item, tuple) else (item, OUTSIDE)
            conn.execute(
                "DELETE FROM agent_outside_tools WHERE agent_name = %s AND mcp_name = %s AND governs = %s",
                (agent_name, name, governs))
        for app_id in remove_apps or ():
            conn.execute("DELETE FROM agent_outside_apps WHERE agent_name = %s AND app_id = %s",
                         (agent_name, app_id))
        for app in add_apps or ():
            conn.execute(
                "INSERT INTO agent_outside_apps (agent_name, app_id, methods, changed_at) "
                "VALUES (%s, %s, %s, '') ON CONFLICT (agent_name, app_id) DO UPDATE SET "
                "methods = EXCLUDED.methods, changed_at = ''",
                (agent_name, app["app_id"], json.dumps(sorted(app.get("methods") or []))),
            )
        for pick in upsert_tools or ():
            functions = json.dumps(list(pick.get("functions") or []))
            conn.execute(
                "INSERT INTO agent_outside_tools "
                "(agent_name, mcp_name, governs, access, functions, granted, changed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, '') "
                "ON CONFLICT (agent_name, mcp_name, governs) DO UPDATE SET access = EXCLUDED.access, "
                "functions = EXCLUDED.functions, granted = EXCLUDED.granted, changed_at = '' "
                "WHERE agent_outside_tools.access <> EXCLUDED.access "
                "OR agent_outside_tools.functions <> EXCLUDED.functions",
                (agent_name, pick["mcp_name"], pick.get("governs") or OUTSIDE, pick["access"], functions,
                 json.dumps(sorted(pick.get("granted") or []))),
            )
        conn.execute("INSERT INTO agent_outside (agent_name) VALUES (%s) ON CONFLICT (agent_name) DO NOTHING",
                     (agent_name,))
        row = conn.execute("SELECT knowledge FROM agent_outside WHERE agent_name = %s FOR UPDATE",
                           (agent_name,)).fetchone()
        if add_knowledge or remove_knowledge:
            settings["knowledge"] = json.dumps(_merged(_names(row["knowledge"]), list(add_knowledge or ()),
                                                       list(remove_knowledge or ())))
        sets = [(column, value) for column, value in settings.items() if value is not None]
        if sets:
            conn.execute(
                "UPDATE agent_outside SET " + ", ".join(f"{column} = %s" for column, _ in sets)
                + " WHERE agent_name = %s",
                (*[value for _, value in sets], agent_name),
            )
        conn.commit()


def agents_with_pick(mcp_name: str) -> list[str]:
    """The agents whose tool rows (a pick or a limit) name ``mcp_name``."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT agent_name FROM agent_outside_tools WHERE mcp_name = %s ORDER BY agent_name",
            (mcp_name,),
        ).fetchall()
    return [r["agent_name"] for r in rows]


def picked_mcp_names() -> list[str]:
    """Every MCP some agent's tool rows (a pick or a limit) name."""
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT mcp_name FROM agent_outside_tools ORDER BY mcp_name").fetchall()
    return [r["mcp_name"] for r in rows]


def read_only_picks_of(mcp_name: str) -> list[tuple[str, str, list[str], str]]:
    """``(agent_name, governs, granted, changed_at)`` for every Read only
    row of ``mcp_name``, in every column: the rows a label change may move."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, governs, granted, changed_at FROM agent_outside_tools "
            "WHERE mcp_name = %s AND access = 'read_only' ORDER BY agent_name, governs",
            (mcp_name,),
        ).fetchall()
    return [(r["agent_name"], r["governs"], _names(r["granted"]), r["changed_at"] or "") for r in rows]


def mark_pick_changed(agent_name: str, mcp_name: str, governs: str, granted: list[str]) -> None:
    """A later listing changed what Read only gives: the row keeps the new
    set and the time it changed."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE agent_outside_tools SET granted = %s, changed_at = %s "
            "WHERE agent_name = %s AND mcp_name = %s AND governs = %s",
            (json.dumps(sorted(granted)), _now(), agent_name, mcp_name, governs),
        )
        conn.commit()


def set_granted(agent_name: str, mcp_name: str, governs: str, granted: list[str]) -> None:
    """The first listing of a server fills what a Read only row saved
    before it gives: the set, no stamp (nothing changed under a manager)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE agent_outside_tools SET granted = %s "
            "WHERE agent_name = %s AND mcp_name = %s AND governs = %s",
            (json.dumps(sorted(granted)), agent_name, mcp_name, governs),
        )
        conn.commit()


def delete_agent_picks(agent_name: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM agent_outside_tools WHERE agent_name = %s", (agent_name,))
        conn.execute("DELETE FROM agent_outside_apps WHERE agent_name = %s", (agent_name,))
        conn.execute("DELETE FROM agent_outside WHERE agent_name = %s", (agent_name,))
        conn.commit()
