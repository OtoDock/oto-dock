"""The app and Dock tools of display-mcp (APPS.md): pinned apps, the
live-app hooks, releases and Dock file pins. Thin by design — the proxy
hook owns scope resolution, path confinement, manifest validation, the
satellite push and the live-reload broadcast; this module builds the
request and words the answer. Kept apart from ``display_server.py`` (the
media and artifact server) so each side stays within the platform's
file-size rule.
"""

import os

import httpx
from mcp.types import ImageContent, TextContent, Tool

PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
SESSION_ID = os.environ.get("OTO_SESSION_ID", "")

# visibility-modes: the agent's mode scopes filter pin_app's `visibility`
# enum so the LLM can't pick an ownership this agent's mode lacks; the
# default mirrors OTO_DEFAULT_SCOPE (the platform's effective default —
# viewer-clamped server-side too). Same pattern as schedules-mcp.
AVAILABLE_SCOPES = [
    s for s in (os.environ.get("OTO_AVAILABLE_SCOPES", "") or "").split(":")
    if s in ("user", "agent")
] or ["user", "agent"]
_DEFAULT_SCOPE = (
    os.environ.get("OTO_DEFAULT_SCOPE")
    or os.environ.get("PROXY_TASK_SCOPE")
    or os.environ.get("OTO_SCOPE")
    or "user"
)
SCOPE_DEFAULT = _DEFAULT_SCOPE if _DEFAULT_SCOPE in AVAILABLE_SCOPES else AVAILABLE_SCOPES[0]

# UI artifact content cap (mirrors the /v1/hooks/ui limit — checked here too
# so an oversized artifact fails with a clear message before the POST).
MAX_UI_HTML_BYTES = 2 * 1024 * 1024

_VISIBILITY_ARG = {
    "type": "string",
    "enum": AVAILABLE_SCOPES,
    "description": (
        "Disambiguate a slug pinned both shared ('agent') "
        "and personal ('user'). Omit to auto-detect."
    ),
}


def _artifact_read_candidates(raw: str) -> list[str]:
    """Local filesystem candidates for an existing artifact at ``raw``.

    Only used for the html-less re-display flow, and only in THIS process's
    namespace — where the CLI's own Edit writes land (the local sandbox
    mounts the virtual paths for real; on a remote machine the env dirs are
    rewritten to machine-absolute paths). Two forms, tried in order:

    1. Absolute (``/workspace/…``, ``/users/<u>/workspace/…``): as-is first
       (real inside the local sandbox), then anchored to the agent root
       derived from ``OTO_WORKSPACE_DIR`` — the same ``agent_dir + virtual``
       rule the satellite path translator applies, so scope semantics match
       the hook's ``_sandbox_to_host`` (a user-scope session re-displaying a
       shared ``/workspace/…`` artifact reads the SHARED file).
    2. Relative: joined to ``OTO_WORKSPACE_DIR`` (the hook's documented
       workspace-relative form).

    Not a security boundary: the process can only read what the session
    itself can read (mount namespace / machine scope enforce that).
    """
    workspace = os.environ.get("OTO_WORKSPACE_DIR", "").rstrip("/")
    username = os.environ.get("OTO_USERNAME", "")
    bases: list[str] = []
    if raw.startswith("/"):
        bases.append(raw)
        # Agent root = OTO_WORKSPACE_DIR minus its scope suffix.
        agent_root = ""
        user_suffix = f"/users/{username}/workspace" if username else ""
        if user_suffix and workspace.endswith(user_suffix):
            agent_root = workspace[: -len(user_suffix)]
        elif workspace.endswith("/workspace"):
            agent_root = workspace[: -len("/workspace")]
        if agent_root:
            bases.append(agent_root + raw)
    elif workspace:
        bases.append(os.path.join(workspace, raw))
    # The hook forces .html on save — accept an extensionless echo of the path.
    out: list[str] = []
    for p in bases:
        for cand in (p, p if p.lower().endswith(".html") else p + ".html"):
            if cand not in out:
                out.append(cand)
    return out


def _read_artifact_file(raw: str) -> tuple[str | None, str]:
    """Read an existing artifact's current content; returns (html, error)."""
    for candidate in _artifact_read_candidates(raw):
        try:
            with open(candidate, "rb") as f:
                data = f.read(MAX_UI_HTML_BYTES + 1)
        except OSError:
            continue
        if len(data) > MAX_UI_HTML_BYTES:
            return None, (
                f"Error: the artifact file at {raw} exceeds the 2MB cap."
            )
        if not data.strip():
            return None, (
                f"Error: the artifact file at {raw} is empty — pass 'html'."
            )
        return data.decode("utf-8", errors="replace"), ""
    return None, (
        f"Error: no artifact file found at '{raw}' — pass 'html' to create "
        f"it, or use the exact path a previous display_ui ack returned."
    )


APP_TOOLS: list[Tool] = [
    Tool(
        name="pin_app",
        description=(
            "Pin (or update — same slug upserts) a standing APP: an "
            "HTML dashboard the user opens any time from the chat page's "
            "apps button, outliving every chat. Use for recurring surfaces "
            "(morning brief, project status, home dashboard) — for a "
            "one-off visual in THIS conversation use display_ui instead. "
            "Same sandboxed rendering as display_ui (kit + Tailwind "
            "available; Tailwind + mobile-responsive layout are REQUIRED "
            "for apps — see the skill). Buttons may invoke DECLARED "
            "actions only, via otodock.action('<id>', args): declare them "
            "in `actions`; the user approves the manifest before any "
            "button works (the ack tells you the approval state — relay "
            "it). Re-pinning with new html live-reloads open tabs: that "
            "is how a scheduled task refreshes an app. If the user "
            "unpinned an app from their dashboard, pin_app(slug) alone "
            "RESTORES it — file, actions and approval intact (list_apps "
            "marks such slugs 'unpinned'); never rebuild what still "
            "exists. `scope` pins a Dock dashboard instead: "
            "scope='chat' binds it to THIS chat (opened from the chat's "
            "Dock button), scope='project' to this chat's delegation "
            "project (shown beside the live lane cards) — one dashboard "
            "per chat/project, and a scoped re-pin REPLACES it (see the "
            "skill's Scoped dashboards section)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": (
                        "Stable identity, 1-40 chars [a-z0-9-]. Reuse to "
                        "update; check list_apps before inventing one."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Tab title shown to the user.",
                },
                "html": {
                    "type": "string",
                    "description": (
                        "HTML body fragment (markup + <style>/<script>), "
                        "max 2MB. Required on first pin UNLESS "
                        "apps/<slug>.html already exists in your scope "
                        "(re-pin reuses it) or apps/<slug>/app.json does "
                        "(a folder app: the pin is a deploy_app, and "
                        "`actions` is ignored — app.json carries them); "
                        "omit to update only metadata/actions. Saved to "
                        "apps/<slug>.html in your scope workspace."
                    ),
                },
                "actions": {
                    "type": "array",
                    "description": (
                        "Declared-actions manifest (≤16). Each: {id, label, "
                        "type: 'fire_task'|'send_prompt'|'mcp_tool'|'data_feed'|'platform', "
                        "task_id? (fire_task — must be a scheduled or trigger task of this agent), "
                        "prompt? (send_prompt — may use {{arg}} placeholders filled from the "
                        "otodock.action args), mcp?/tool?/fixed_args? (mcp_tool — calls ONE tool "
                        "on one of this agent's MCPs directly, no agent turn), feed? (data_feed — "
                        "subscribe the page to a read-only live platform feed via otodock.feed: "
                        "'active_chats', 'project_lanes', 'sessions', 'tasks', 'notifications', "
                        "'trigger_fires', 'file_changes'), method? (platform — a request the page "
                        "makes via otodock.platform: 'viewer.me', 'integrations.status', "
                        "'tasks.run_result', 'notifications.create', 'app.audience' (who uses the "
                        "app: its members, the agents it is placed in and the people it is shared "
                        "with; on a page, editors and up of this agent, the owner of a personal "
                        "app or an admin), "
                        "'viewer.data.read' / 'viewer.data.write' (a small document per viewer, "
                        "each page its own; single-file apps only)), min_role? "
                        "('contributor'|'editor'|'manager' "
                        "— the lowest agent role that may use the action; everyone otherwise), "
                        "args_schema? (fire_task/mcp_tool/platform — flat JSON-Schema object of "
                        "scalar props gating page-supplied args; strings need maxLength or enum; "
                        "see the skill; 'viewer.data.write' takes its doc or patch object with "
                        "none). Every viewer reads their own slice of a feed or method. "
                        "Omit to keep the current manifest; [] clears it. Changing it requires "
                        "user re-approval."
                    ),
                    "items": {"type": "object"},
                },
                "make_default": {
                    "type": "boolean",
                    "description": "Make this the default (first) tab.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["standing", "chat", "project"],
                    "description": (
                        "Where the app lives. 'standing' (default): the "
                        "apps strip, outliving every chat. 'chat': THIS "
                        "chat's Dock dashboard (progress boards for "
                        "plan-scale work). 'project': this chat's "
                        "delegation project Dock (plan overview + live "
                        "lanes; errors if the chat has no project). Ids "
                        "resolve from your session — never passed."
                    ),
                },
                "visibility": {
                    "type": "string",
                    "enum": AVAILABLE_SCOPES,
                    "default": SCOPE_DEFAULT,
                    "description": (
                        f"WHO sees it (default for this agent: "
                        f"`{SCOPE_DEFAULT}`; orthogonal to `scope`). "
                        "'agent' = one shared dashboard for every user "
                        "of this agent (needs editor+ when a human "
                        "drives the session); 'user' = the current "
                        "user's personal dashboard. Omit for the "
                        "agent's default; override only when the "
                        "intent is explicit (e.g. \"pin this for the "
                        "whole team\" / \"just for me\")."
                    ),
                },
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="unpin_app",
        description=(
            "Retire a pinned app by slug (your scope) — removes the "
            "registration, its actions manifest AND its approval; the "
            "apps/<slug>.html workspace file stays. (The dashboard's X "
            "only hides an app — pin_app(slug) restores those.) A slug "
            "pinned both shared and personal needs `visibility` to pick "
            "one; otherwise it auto-detects."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="list_apps",
        description=(
            "List the pinned apps in your scope (shared + the "
            "session user's personal ones) with slug, title, path, "
            "declared actions, and approval state, plus the apps a share "
            "placed in this agent (another agent's, named by their home "
            "agent and slug with their id, the role you act at and their "
            "exported methods: call those with your session token by id; "
            "never re-pin them). Entries marked "
            "'unpinned' were removed from the user's dashboard — "
            "pin_app(slug) restores one with approval intact. Check "
            "before pin_app so slugs are reused deliberately."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="app_push",
        description=(
            "Change what an OPEN app shows right now, without re-pinning: "
            "relays a small JSON payload to every open frame of the app; "
            "the page receives it as an `otodock:push` window event "
            "(`otodock.onPush(cb)`). For \"look here\", \"point at lane "
            "three\", \"the value just changed\". Nothing is stored: a "
            "viewer who opens the app later sees nothing of it, so "
            "durable data belongs in app_state. Same authority as "
            "pin_app, no approval. Up to 32 KB, ten per second per app."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "payload": {
                    "description": "Any JSON the page understands (at most 32 KB).",
                },
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug", "payload"],
        },
    ),
    Tool(
        name="app_state",
        description=(
            "Write the app's STATE DOCUMENT: one JSON object per app that "
            "the platform stores and every viewer's page reads "
            "(`otodock.state`, delivered live as the `otodock:state` "
            "window event). The data of a dashboard whose layout is the "
            "HTML: a description on a card, a status, a list. Pass `patch` "
            "(a merge patch: objects merge key by key, null deletes a key, "
            "anything else replaces) or `doc` (the whole new document). "
            "Survives reloads, needs no re-pin and no server. Up to 64 KB, "
            "two writes per second per app. Same authority as pin_app, no "
            "approval. Only you write it: when a viewer should change "
            "something, give them a button and update the state from the "
            "action."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "patch": {
                    "type": "object",
                    "description": "A merge patch onto the current document.",
                },
                "doc": {
                    "type": "object",
                    "description": "The whole new document (replaces everything).",
                },
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="open_app",
        description=(
            "Put one of your apps on the user's screen: the \"have a look "
            "at this\" moment after you built or changed it. In the chat "
            "the user is in, the app opens right away (the overlay, or "
            "the Dock for a chat-scoped one); on another page they get a "
            "notice with an Open button. An app a share placed in this "
            "agent opens too (pass agent, its home agent, when one of "
            "this agent's own apps or another placed app shares the slug): it arrives as the "
            "notice with Open, never in this agent's overlay. Only for a "
            "session with a human, only on a screen they are using, a few "
            "times a minute. The result says opened, no_screen, hidden "
            "(they parked the app off their strip; for a placed app, off "
            "this agent's strip) or off (they turned this off in "
            "settings): say so instead of retrying."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "agent": {
                    "type": "string",
                    "description": "The home agent of an app a share placed in this agent "
                                   "(as list_apps names it); omit for this agent's own apps.",
                },
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="rollback_app",
        description=(
            "Point an app back at its previous release. Every pin_app "
            "keeps a copy of what viewers were served (the last three); "
            "this switches the app to the one before the current, at "
            "once, on every open screen. The working file apps/<slug>.html "
            "is untouched, so the next pin_app cuts a fresh release from "
            "it. For an app with a server the database is rolled back "
            "too: app.db is restored from the copy taken before the "
            "current release went live (writes since then are lost; a "
            "snapshot of them is kept beside the releases), and it is "
            "refused while a release waits for approval. Use it when a "
            "deploy broke something and the fix will take a moment. 'no "
            "previous release' means there is nothing to go back to."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="deploy_app",
        description=(
            "Deploy a FOLDER app: apps/<slug>/ with app.json (title, actions, "
            "files, egress, and the blocks handlers / exports / bindings / "
            "requires / steps / secrets / inbound / external — see the skill's "
            "'Apps with a server'; a secret is a NAME a person sets a value for "
            "after the deploy, never a value in the folder; an inbound hook is a "
            "public route a vendor's signed events wake a handler through; "
            "external says what a shared link may do — the sites it may open, "
            "the routes that need a bot check, how long a visitor stays "
            "logged in), "
            "client/index.html (+ assets) and an optional "
            "server/index.ts run on Bun in its own sandbox with its own SQLite "
            "at /app/data (see the skill's 'Apps with a server'). Copies the "
            "folder into the next release, starts the new server next to the "
            "old one and switches only when it answers /_health — a broken "
            "server leaves the previous release running and the result says "
            "why. A changed app.json (or the app's 'deploys need approval' "
            "switch, or a required secret with no value yet) lands as a PENDING "
            "release until a person approves it on the card: relay 'pending "
            "approval' and the `waiting` reason to the user. Run check_app "
            "first. pin_app(slug) on such a folder does the same thing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug (the folder name under apps/)."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="check_app",
        description=(
            "Validate a folder app before deploying: app.json (schema, action "
            "targets, file prefixes, egress hosts, handler names and crons, "
            "exports, bindings, requires, steps, secrets, inbound, external — the "
            "reply's `manifest` says which blocks parsed), the caps, client/index.html, "
            "and a SMOKE start of the server on a scratch port with scratch "
            "data (the live database is never touched). Returns ok, or the "
            "reason and the server's first log lines."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="deploy_status",
        description=(
            "The deploy state of an app: the live release, a release waiting "
            "for approval and what it changes, the server's state (up, "
            "starting, backoff with the error, quota_full, static, secrets), "
            "whether the manifest is approved, each declared secret and "
            "whether a person has set it (never the value — ask a manager or "
            "the owner to set a missing one in the app's settings), and the "
            "app's last ten wakes with their verdicts (read them when a "
            "handler is not firing)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="describe_app",
        description=(
            "What another app OFFERS to apps that bind to it (APPS.md 'Bindings'): "
            "its exported methods, snapshots and events with their descriptions "
            "and role floors, so you can build against it without reading its "
            "code. Reachable: this agent's own apps, the apps of agents this "
            "agent delegates to, the apps of agents the current user is a "
            "member of, and the apps a share placed in this agent (list_apps "
            "names them); anything else is 'no app to bind to'. To use one "
            "from your server, declare it in your app.json: \"bindings\": "
            "[{\"name\": \"crm\", \"agent\": \"<agent>\", \"app\": \"<slug>\"}] "
            "— the user approves the binding on your card, the target's editor "
            "approved its exports. An app placed here you may also call from "
            "this session: POST $PROXY_URL/v1/apps/<app_id>/api/<method> with "
            "your session token, at your role here as the share gives it; its "
            "other routes stay out of reach, and a placement opens no binding: "
            "your server reaches it only over the usual edge (this agent's "
            "delegation for a shared app, your membership for your personal app)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "The agent that owns the app (default: this agent)."},
                "slug": {"type": "string", "description": "The app's slug."},
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="export_app",
        description=(
            "Pack a folder app's WORKING folder as apps/<slug>.otoapp in the same "
            "scope (APPS.md 'Blueprints'): the files a release would take, the "
            "app.json with its buttons by slug, and the tasks those buttons point "
            "at — never the database, never node_modules. Send it with send_file, "
            "or drop it in another agent's workspace and import_app it there."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="import_app",
        description=(
            "Install a .otoapp bundle from the workspace as an app of this agent: "
            "the folder lands at apps/<slug>/, the bundle's tasks are created for "
            "this scope and the buttons re-pointed at them, and release 1 WAITS FOR "
            "THE USER'S APPROVAL on the card — an import is never pre-approved, "
            "say so. Refused when a button names an MCP this agent lacks (the "
            "message names it: ask an admin to assign it, then import again)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "The bundle in the workspace, e.g. apps/board.otoapp."},
                "visibility": _VISIBILITY_ARG,
                "slug": {"type": "string", "description": "Install under another slug (default: the bundle's)."},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="preview_app",
        description=(
            "Start the WORKING TREE of a folder app as a preview for the owner "
            "or an editor, with its own copy of the data (seeded from the live "
            "database; changes there are thrown away), and return the URL "
            "they open. Everyone else keeps seeing the live release. Use it to "
            "let the user try a change before deploy_app."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="screenshot_app",
        description=(
            "SEE a deployed folder app the way a viewer does, without deploying: "
            "the platform loads the page in its own headless browser at phone, "
            "tablet and desktop widths plus the other theme and returns the "
            "pictures with the verdict (page errors, blocked requests, a page "
            "that never became ready). Default: your WORKING folder on scratch "
            "data (the live database is never touched); source 'live' looks at "
            "the release viewers see. deploy_app and check_app run the same "
            "render by themselves and attach the pictures, so call this to look "
            "again after an edit, not after every deploy. Takes up to a minute."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "visibility": _VISIBILITY_ARG,
                "source": {"type": "string", "enum": ["working", "live"],
                           "description": "The working folder (default) or the live release."},
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="app_logs",
        description=(
            "The last lines of a folder app's server log (stdout and stderr, "
            "timestamped) and its state — read them when a deploy failed or "
            "the user says the app is down."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "tail": {"type": "integer", "description": "How many lines (default 200, max 2000)."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="purge_app",
        description=(
            "DELETE a folder app with everything it owns: the registration, "
            "the releases, the DATABASE and the apps/<slug>/ folder. Not the "
            "unpin (which keeps the folder and the data): this is for an app "
            "that is done for good. Only on the user's explicit word, and pass "
            "confirm=<slug> to prove it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "confirm": {"type": "string", "description": "The slug again."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug", "confirm"],
        },
    ),
    Tool(
        name="restart_app",
        description=(
            "Stop and start a folder app's server (the live release). Open "
            "pages reconnect by themselves."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The app's slug."},
                "visibility": _VISIBILITY_ARG,
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="pin_file",
        description=(
            "Pin an EXISTING workspace text/markdown file to the chat or "
            "project Dock as a read-only row: collapsed by default, the "
            "user expands it to a rich markdown render that live-updates "
            "as the file changes — zero upkeep from you. The right tool "
            "for living documents (a plan file on the project Dock, a "
            "spec, meeting notes): NEVER build an app just to show a "
            "file. Re-pinning the same path updates the title. On a "
            "remote machine the platform mirror is what renders — edits "
            "appear after the end-of-turn sync."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace-relative path of an existing text "
                        "file (e.g. projects/hero-video/plan.md). "
                        "Renderable types: .md (rich), plus plain-text "
                        "code/config types."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Row title (default: the filename).",
                },
                "scope": {
                    "type": "string",
                    "enum": ["chat", "project"],
                    "description": (
                        "'chat' (default): THIS chat's Dock. 'project': "
                        "this chat's delegation project Dock (errors if "
                        "the chat has no project). Ids resolve from your "
                        "session — never passed."
                    ),
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="unpin_file",
        description=(
            "Remove a Dock file pin by path (the file itself stays). "
            "Omit 'path' to clear every file pin of the scope."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "The pinned file's path."},
                "scope": {"type": "string", "enum": ["chat", "project"],
                          "description": "Which Dock (default 'chat')."},
            },
        },
    ),
]


def _findings_text(slug: str, data: dict, again: str) -> str:
    """The static checks' answer in words (APPS.md "Deploy pipeline"): the
    problems that refused the step with file, line and fix, or the warnings
    worth fixing after a step that went through."""
    rows = data.get("findings") or []
    fails = [f for f in rows if f.get("severity") == "fail"]
    warns = [f for f in rows if f.get("severity") != "fail"]
    line = lambda f: f"  {f.get('file')}:{f.get('line')} {f.get('rule')}: {f.get('message')}"  # noqa: E731
    out = ""
    if fails:
        out = (f"'{slug}' is NOT ready: {len(fails)} problem(s) the sandbox would hit:\n"
               + "\n".join(line(f) for f in fails) + f"\nFix them and {again}.")
    if warns:
        out += ("\n" if out else "") + "Worth fixing:\n" + "\n".join(line(f) for f in warns)
    return out


def _render_parts(data: dict) -> tuple[str, list[ImageContent]]:
    """The rendered check's verdict in words and its pictures (APPS.md
    "Deploy pipeline"): what the proxy's headless browser saw of the page,
    handed to the agent as images so it can judge the layout itself."""
    render = data.get("render") or {}
    if not render:
        return "", []
    images = [
        ImageContent(type="image", data=im["jpeg_b64"], mimeType="image/jpeg")
        for im in render.get("images") or [] if im.get("jpeg_b64")
    ]
    text = str(render.get("summary") or "")
    if images:
        names = ", ".join(f"{im.get('name')} ({im.get('width')}px)" for im in render.get("images") or [])
        text += f"\nPictures, in order: {names}."
    for key, label in (("errors", "Page errors"), ("csp", "Blocked by the policy"),
                       ("failed_requests", "Failed requests")):
        rows = render.get(key) or []
        if rows:
            text += f"\n{label}:\n" + "\n".join(f"  {_render_row(r)}" for r in rows[:10])
    return text, images


def _render_row(r) -> str:
    if not isinstance(r, dict):
        return str(r)[:200]
    return " ".join(str(v)[:120] for v in r.values() if v not in (None, ""))


async def _post_hook(path: str, payload: dict, *, read: float = 60.0) -> tuple[dict | None, str]:
    """POST a hook with the session JWT; returns (json, "") or (None, error).
    ``read`` stretches for the hooks that render the page (a browser start,
    three widths, a server that may take its 20 s to answer)."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=read, write=30.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{PROXY_URL}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {PROXY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            return resp.json() or {}, ""
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code} — {e.response.text[:300]}"
    except httpx.TimeoutException:
        return None, (f"no answer within {int(read)} s — the step may still finish on the "
                      "platform; read deploy_status before trying again")
    except Exception as e:
        return None, str(e) or type(e).__name__


async def _handle_pin_app(arguments: dict) -> list[TextContent]:
    """Pin/update a standing app. Thin like display_ui: the proxy hook
    owns scope resolution, path anchoring, manifest validation, the
    satellite push, and the live-reload broadcast."""
    slug = (arguments.get("slug") or "").strip()
    if not slug:
        return [TextContent(type="text", text="Error: 'slug' is required.")]
    html_content = arguments.get("html") or ""
    if not html_content.strip():
        # Opportunistic freshness on html-less re-pin: if the app file is
        # readable HERE (where the CLI's Edit writes land), forward its
        # current content so a satellite-side edit goes live NOW instead of
        # at turn-end sync. Unreadable/missing → keep the classic semantics
        # (empty html = the hook keeps the existing platform-side file), so
        # restore-after-unpin still works from any machine.
        refreshed, err = _read_artifact_file(f"apps/{slug}.html")
        if not err and refreshed:
            html_content = refreshed
    if html_content and len(html_content.encode("utf-8")) > MAX_UI_HTML_BYTES:
        return [TextContent(
            type="text",
            text="Error: html exceeds the 2MB app cap — trim embedded assets.",
        )]
    payload = {
        "session_id": SESSION_ID,
        "slug": slug,
        "title": (arguments.get("title") or "").strip(),
        "html": html_content,
        "actions": arguments.get("actions"),
        "make_default": bool(arguments.get("make_default", False)),
        "scope": (arguments.get("scope") or "standing").strip().lower(),
        # Ownership (S1). Empty = the proxy resolves the session's effective
        # default — same value SCOPE_DEFAULT advertises, kept server-side so
        # the two can't drift.
        "visibility": (arguments.get("visibility") or "").strip().lower(),
    }
    # A folder pin is a deploy (the page is rendered before it goes live).
    data, err = await _post_hook("/v1/hooks/apps/pin", payload, read=240.0)
    if data is None:
        return [TextContent(type="text", text=f"Error pinning app: {err}")]
    if "approval" not in data:
        # A single-file pin's answer names its approval; a refusal and a
        # folder's deploy answer do not.
        return _deploy_reply(slug, data, "pin_app again")
    warned = _findings_text(slug, data, "") if data.get("findings") else ""
    approval = data.get("approval", "none")
    note = {
        "approved": "Actions are approved and live.",
        "pending user approval": (
            "Actions are PENDING USER APPROVAL — tell the user to open the "
            "apps panel and approve them before the buttons work."
        ),
        "none": "No actions declared.",
    }.get(approval, "")
    restored = (data.get("replaced") or data.get("restored")
                or data.get("reused_file") or "")
    pin_scope = data.get("pin_scope", "standing")
    where = {
        "chat": "this chat's Dock",
        "project": "the project's Dock",
    }.get(pin_scope, "the apps strip")
    release = data.get("release")
    return [TextContent(
        type="text",
        text=(
            f"Pinned app '{slug}' ({data.get('scope', '')}, on {where}, "
            f"saved at {data.get('path', '')})."
            + (f" Release {release} is what viewers see now." if release else "")
            + (f" NOTE: {restored}." if restored else "")
            + f" {note} Re-pin with the same slug to update; open tabs "
            f"live-reload."
            + (f"\n{warned}" if warned else "")
        ),
    )]


async def _handle_file_pin_hook(op: str, arguments: dict) -> list[TextContent]:
    """Dock file pins — thin like the app hooks: the proxy owns scope
    resolution, path confinement, and the pins-refresh broadcast."""
    path = (arguments.get("path") or "").strip()
    if op == "pin" and not path:
        return [TextContent(type="text", text="Error: 'path' is required.")]
    payload = {
        "session_id": SESSION_ID,
        "path": path,
        "title": (arguments.get("title") or "").strip(),
        "scope": (arguments.get("scope") or "chat").strip().lower(),
    }
    data, err = await _post_hook(f"/v1/hooks/files/{op}", payload)
    if data is None:
        return [TextContent(type="text", text=f"Error ({op} file): {err}")]
    if op == "unpin":
        return [TextContent(
            type="text",
            text=(f"Removed {data.get('removed', 0)} file pin(s) from the "
                  f"{payload['scope']} Dock (files kept)."),
        )]
    return [TextContent(
        type="text",
        text=(
            f"Pinned '{data.get('title', '')}' ({data.get('path', '')}) to "
            f"{'this chat' if data.get('pin_scope') == 'chat' else 'the project'}'s "
            f"Dock. {data.get('note', '')}"
        ),
    )]


async def _handle_live_hook(op: str, arguments: dict) -> list[TextContent]:
    """app_push / app_state / open_app (APPS.md "Live apps"): one hook
    each; the reply tells the agent what reached a screen and, for open,
    why nothing did."""
    payload: dict = {"session_id": SESSION_ID, "slug": (arguments.get("slug") or "").strip()}
    if arguments.get("visibility"):
        payload["visibility"] = arguments["visibility"].strip().lower()
    if op == "open" and arguments.get("agent"):
        payload["agent"] = str(arguments["agent"]).strip().lower()
    if op == "push":
        payload["payload"] = arguments.get("payload")
    elif op == "state":
        if arguments.get("doc") is not None:
            payload["doc"] = arguments["doc"]
        else:
            payload["patch"] = arguments.get("patch")
    data, err = await _post_hook(f"/v1/hooks/apps/{op}", payload)
    if data is None:
        return [TextContent(type="text", text=f"Error ({op}): {err}")]
    slug = payload["slug"]
    screens = int(data.get("screens") or 0)
    if op == "push":
        note = "" if screens else " (nobody has it open right now; a push is not stored)"
        return [TextContent(
            type="text",
            text=f"Pushed to '{slug}': {screens} open screen(s) received it{note}.",
        )]
    if op == "state":
        return [TextContent(
            type="text",
            text=(f"State of '{slug}' is now rev {data.get('rev')}; {screens} open "
                  "screen(s) updated. Pages read it from otodock.state on load."),
        )]
    status = data.get("status") or ""
    if status == "opened":
        return [TextContent(type="text", text=f"Opened '{slug}' on {screens} screen(s).")]
    return [TextContent(
        type="text",
        text=f"'{slug}' was not opened ({status}): {data.get('reason', '')}".rstrip(": "),
    )]


async def _handle_app_hook(op: str, arguments: dict) -> list[TextContent]:
    payload = {"session_id": SESSION_ID, "slug": (arguments.get("slug") or "").strip()}
    if arguments.get("visibility"):
        payload["visibility"] = arguments["visibility"].strip().lower()
    data, err = await _post_hook(f"/v1/hooks/apps/{op}", payload)
    if data is None:
        return [TextContent(type="text", text=f"Error ({op}): {err}")]
    if op == "unpin":
        kept = f"file {data.get('kept_file', '')} kept"
        if data.get("kept_data"):
            kept += f"; its database stays at {data['kept_data']} — a later pin_app of the slug finds it"
        return [TextContent(type="text", text=f"Unpinned '{payload['slug']}' ({kept}).")]
    if op == "rollback":
        # Three outcomes: a folder app whose database went back, a folder
        # app with no copy from before the release it left (the data stays),
        # and a single-file app (no database at all).
        if "db_restored" not in data:
            tail = "The working file is unchanged; the next pin_app cuts a new release."
        elif data.get("db_restored"):
            tail = ("The database was restored to its state before the release you left"
                    f" (a snapshot of the newer writes: {data.get('snapshot')}).")
        else:
            tail = ("No copy of the database from before the release you left exists, so "
                    "the app keeps its current data.")
        return [TextContent(
            type="text",
            text=(f"'{payload['slug']}' now serves release {data.get('release')} again "
                  f"({data.get('screens', 0)} open screen(s) reloaded). {tail}"),
        )]
    apps = data.get("apps", [])
    placed = data.get("placed", [])
    if not apps and not placed:
        return [TextContent(type="text", text="No pinned apps in your scope.")]
    lines = []
    for a in apps:
        acts = ", ".join(
            f"{x.get('id')}({x.get('type')})" for x in a.get("actions", [])
        ) or "none"
        approved = "approved" if a.get("actions_approved") else "PENDING APPROVAL"
        pin_scope = a.get("pin_scope", "standing")
        scope_tag = (f", {pin_scope}-scoped" if pin_scope != "standing" else "")
        lines.append(
            f"- {a.get('slug')} [{a.get('scope')}{scope_tag}{_kind_tag(a)}] \"{a.get('title')}\" — "
            f"id {a.get('id')}, path {a.get('path')}, actions: {acts}"
            + ("" if acts == "none" else f" ({approved})")
            + (f" — waiting: {a['waiting']}" if a.get("waiting") else "")
            + (" — UNPINNED by the user; pin_app(slug) restores it"
               if a.get("unpinned") else "")
        )
    text = ("Pinned apps:\n" + "\n".join(lines)) if lines else "No pinned apps of this agent's own."
    if placed:
        text += ("\nPlaced here by a share (another agent's apps: call their exported methods "
                 "by id with your session token, never re-pin them):\n"
                 + "\n".join(_placed_line(p) for p in placed))
    return [TextContent(type="text", text=text)]


def _kind_tag(a: dict) -> str:
    return ", server app" if a.get("kind") == "folder" else ""


_SHARE_WORDS = {"agent": "an agent share", "department": "a department share",
                "person": "your own accepted share"}


def _placed_line(p: dict) -> str:
    """One line of ``list_apps`` for an app a share placed in this agent
    (SHARING.md "Agents use a placed app"): the home pair and the id, how it
    got here, the role this session acts at, and the calls it answers."""
    pl = p.get("placement") or {}
    methods = (p.get("exports") or {}).get("methods") or {}
    calls = ", ".join(
        f"{name}" + (f" ({spec.get('min_role')} and up)" if spec.get("min_role") else "")
        for name, spec in methods.items()
    )
    role = p.get("role") or ""
    acting = (f"as {role}" if role and role != "agent"
              else "with no person behind this session (methods without a role floor only)")
    return (
        f"- {p.get('slug')} of agent {p.get('agent')} [placed by "
        f"{_SHARE_WORDS.get(pl.get('kind'), 'a share')} at {pl.get('role_cap')}, {acting}"
        f"{_kind_tag(p)}] \"{p.get('title')}\" — id {p.get('id')}; "
        + (f"calls: {calls} → POST $PROXY_URL/v1/apps/{p.get('id')}/api/<method>" if calls
           else "exports no method (nothing for a session to call)")
        + ("" if p.get("actions_approved") else
           " — its manifest waits for approval on its home agent; calls answer 404 until then")
        + (" — hidden by the user in this agent" if p.get("hidden_for_me") else "")
    )


def _deploy_reply(slug: str, data: dict, again: str) -> list[TextContent | ImageContent]:
    """A folder deploy's answer in words and pictures — ``deploy_app``, and
    ``pin_app`` on a folder, whose hook runs the same pipeline: refused (the
    static checks, or the rendered check alone), waiting for approval, an
    unknown word reported as it came, or live."""
    rendered, pictures = _render_parts(data)
    if data.get("status") == "refused":
        text = _findings_text(slug, data, again)
        if not any(f.get("severity") == "fail" for f in data.get("findings") or []):
            # Refused by the rendered check alone: the page failed in the
            # browser with no static problem (warnings may still ride).
            text = (f"'{slug}' is NOT ready: {rendered}\nNothing was deployed. Fix it and {again}."
                    + (f"\n{text}" if text else ""))
        elif rendered:
            text += f"\n{rendered}"
        return [TextContent(type="text", text=text), *pictures]
    warned = _findings_text(slug, data, "") if data.get("findings") else ""
    tail = (f"\n{warned}" if warned else "") + (f"\n{rendered}" if rendered else "")
    if data.get("status") == "pending approval":
        why = ("its app.json changed (or is new)" if data.get("manifest_changed")
               else "this app's deploys need approval")
        # A required secret without a value parks the release too (APPS.md
        # "Secrets"): say which, and who sets it — never ask for the value.
        waiting = (f" waiting: {data['waiting']} — a manager (the owner of a personal app) "
                   "sets it from the app's menu → Settings or the card's Set button, then "
                   "approves; never ask for the value in the chat." if data.get("waiting") else "")
        # A deploy never lowers the per-app switch; the person's approval of
        # the release that says so does. Read as a boolean, like the card.
        lowers = (" This release turns approval off for every later deploy; a person "
                  "approves it on the card." if data.get("lowers_approval") is True else "")
        return [TextContent(type="text", text=(
            f"Release {data.get('release')} of '{slug}' is WAITING FOR APPROVAL because {why}: "
            f"tell the user to open the app (app_id {data.get('app_id')}, page "
            f"/apps/{data.get('app_id')}) and approve it on the card. Viewers keep "
            f"release {data.get('live_release') or 'none'} meanwhile." + lowers + waiting + tail)),
            *pictures]
    if data.get("status") != "ok":
        # The deploy answer's words (proxy services/apps/app_deploy.py
        # RESULT_*) are read as strings over the API; a word this MCP
        # does not know is reported as it came, never as a deploy.
        return [TextContent(type="text", text=(
            f"The platform answered {data.get('status')!r} for '{slug}' — nothing was "
            f"deployed. Check the app's card (app_id {data.get('app_id')})." + tail)), *pictures]
    return [TextContent(type="text", text=(
        f"Deployed '{slug}' ({data.get('scope', '')}): release {data.get('release')} is what "
        f"viewers see now ({data.get('screens', 0)} open screen(s) reloaded). "
        f"app_id {data.get('app_id')} (page /apps/{data.get('app_id')}; its API is "
        f"$PROXY_URL/v1/apps/{data.get('app_id')}/api/... with your session bearer). "
        "The previous release and a copy of the database taken before this one went "
        "live stay for rollback_app." + tail)), *pictures]


async def _handle_deploy_hook(op: str, arguments: dict) -> list[TextContent | ImageContent]:
    """deploy_app / check_app / screenshot_app / deploy_status / preview_app /
    app_logs / restart_app (APPS.md): one hook each; the reply words what
    happened and what the agent should tell the user, and the hooks that
    render the page attach its pictures."""
    payload: dict = {"session_id": SESSION_ID, "slug": (arguments.get("slug") or "").strip()}
    if arguments.get("visibility"):
        payload["visibility"] = arguments["visibility"].strip().lower()
    if op == "logs" and arguments.get("tail"):
        payload["tail"] = int(arguments["tail"])
    if op == "purge":
        payload["confirm"] = (arguments.get("confirm") or "").strip()
    if op == "screenshot":
        payload["source"] = "live" if arguments.get("source") == "live" else "working"
    renders = op in ("deploy", "check", "screenshot")
    data, err = await _post_hook(f"/v1/hooks/apps/{op}", payload, read=240.0 if renders else 60.0)
    if data is None:
        if "path-not-allowlisted" in (err or ""):
            # A remote session reaches the platform through its machine's
            # satellite, which carries its own copy of the hook allowlist:
            # an older satellite has no route for a newer hook. Three agents
            # read the bare 403 as "no renderer on this install" and skipped
            # the pictures (found 2026-09-14).
            return [TextContent(type="text", text=(
                f"Error ({op}): the satellite on this machine is older than the platform and "
                f"has no route for this tool yet. It updates itself the next time the "
                f"platform's satellite version changes (an admin can also restart it); "
                f"until then, deploy_app and check_app still render the page and attach "
                f"its pictures, so read those instead of skipping the look."))]
        return [TextContent(type="text", text=f"Error ({op}): {err}")]
    slug = payload["slug"]
    if op == "purge":
        return [TextContent(type="text", text=(
            f"Deleted '{slug}' with its data: the registration, the releases, the database "
            f"and {data.get('files_removed', 0)} workspace file(s) are gone."))]
    if op == "deploy":
        return _deploy_reply(slug, data, "deploy_app again")
    rendered, pictures = _render_parts(data) if renders else ("", [])
    if op == "check" and data.get("status") == "refused":
        return _deploy_reply(slug, data, "check_app again")
    warned = _findings_text(slug, data, "") if data.get("findings") else ""
    tail = (f"\n{warned}" if warned else "") + (f"\n{rendered}" if rendered else "")
    if op == "screenshot":
        which = "the live release" if data.get("source") == "live" else "the working folder"
        return [TextContent(type="text", text=f"'{slug}', {which}: {rendered}"), *pictures]
    if op == "check":
        if data.get("status") == "ok":
            # Name the blocks that parsed: one dropped for a typo is missing
            # from this list, and the author reads it before the card does.
            m = data.get("manifest") or {}
            parsed = [k for k in ("files", "egress", "handlers", "exports", "bindings", "requires",
                                  "steps", "secrets", "inbound", "external")
                      if m.get(k)]
            said = f"{m.get('actions', 0)} action(s)" + (", " + ", ".join(parsed) if parsed else "")
            return [TextContent(type="text", text=(
                f"'{slug}' checks out: {data.get('files')} files, manifest valid ({said}), server "
                f"{data.get('server')}. deploy_app when ready."
                + (f"\nFirst log lines:\n{data.get('log')}" if data.get("log") else "")
                + tail)), *pictures]
        return [TextContent(type="text", text=(
            f"'{slug}' is NOT ready ({data.get('status')}): {data.get('reason', '')}"
            + (f"\nServer log:\n{data.get('log')}" if data.get("log") else "")
            + tail)), *pictures]
    if op == "status":
        lines = [f"'{slug}': live release {data.get('release')}, deploy state "
                 f"{data.get('deploy_state')}, server {data.get('server')}"
                 + (f" ({data.get('error')})" if data.get("error") else "")
                 + (", manifest approved" if data.get("manifest_approved") else ", manifest NOT approved")]
        if data.get("pending_release"):
            ch = data.get("changes") or {}
            lines.append(f"Release {data['pending_release']} waits for approval: "
                         f"{len(ch.get('added', []))} added, {len(ch.get('changed', []))} changed, "
                         f"{len(ch.get('removed', []))} removed file(s).")
        if data.get("deploy_requires_approval"):
            lines.append("Deploys of this app always wait for approval.")
        if data.get("lowers_approval") is True:
            lines.append("The pending release turns approval off for every later deploy; "
                         "a person approves it on the card.")
        secrets = data.get("secrets") or []
        if secrets:
            said = []
            for s in secrets:
                use = ("sent to " + (s.get("sends_to") or {}).get("host", "")
                       if s.get("sends_to") else "read by the server" if s.get("env")
                       else "used by the platform")
                said.append(f"{s.get('name')} ({'required' if s.get('required') else 'optional'}, "
                            f"{use}): {'set' if s.get('set') else 'NOT SET'}")
            lines.append("Secrets: " + "; ".join(said) + ".")
        if data.get("waiting"):
            lines.append(f"waiting: {data['waiting']} — ask a manager (the owner of a personal app) "
                         "to set it in the app's settings; you never see or set a value.")
        wakes = data.get("wakes") or []
        if wakes:
            lines.append("Last wakes (newest first):")
            for w in wakes[:10]:
                lines.append(f"  {w.get('at', '')[:19]} {w.get('handler')} ← {w.get('event')}: "
                             f"{w.get('status')}" + (f" after {w.get('attempts')} attempt(s)"
                                                     if w.get("attempts") else "")
                             + (f" — {w.get('error')}" if w.get("error") else ""))
        return [TextContent(type="text", text="\n".join(lines))]
    if op == "preview":
        return [TextContent(type="text", text=(
            f"Preview of '{slug}' is up (server: {data.get('server')}): the owner or an editor "
            f"opens {data.get('url')} — its data is a copy, thrown away when the preview stops. "
            "It starts with no secret values, and its egress route, bindings and events answer "
            "403 (a server that needs a secret at start may fail as a preview and run live). "
            "Everyone else keeps the live release. deploy_app when it looks right."))]
    if op == "logs":
        # A page that reconnects the live socket itself burns through the
        # per-viewer cap and then looks permanently offline. Its author has
        # no browser console, so the count is reported here.
        refused = int(data.get("sockets_refused") or 0)
        return [TextContent(type="text", text=(
            f"'{slug}' server: {data.get('server')}"
            + (f" ({data.get('error')})" if data.get("error") else "")
            + (f"\n{refused} live socket(s) refused since the proxy started: the page is asking for "
               "more than a viewer may hold. otodock.ws reconnects by itself — a page must not "
               'reconnect it as well (APPS.md "Apps with a server").' if refused else "")
            + "\n" + (data.get("log") or "(no log yet)")))]
    return [TextContent(type="text", text=(
        f"'{slug}' restarted: server {data.get('server')}"
        + (f" ({data.get('error')})" if data.get("error") else "") + "."))]


async def _handle_describe(arguments: dict) -> list[TextContent]:
    """describe_app: an app's exports in words (APPS.md "Bindings")."""
    payload = {"session_id": SESSION_ID, "slug": (arguments.get("slug") or "").strip(),
               "agent": (arguments.get("agent") or "").strip()}
    data, err = await _post_hook("/v1/hooks/apps/describe", payload)
    if data is None:
        return [TextContent(type="text", text=f"Error (describe): {err}")]
    ex = data.get("exports") or {}
    b = data.get("binding") or {}
    placement = data.get("placement") or {}
    lines = [f"'{data.get('slug')}' of agent {data.get('agent')} ({data.get('title')}, app_id "
             f"{data.get('app_id')}) offers:"]
    calls = "live calls ("
    if b:
        calls += "GET/POST …/bindings/<name>/<method>[/…] from your server"
    if placement:
        calls += ("; " if b else "") + f"from this session: {data.get('call')}"
    words = {"methods": calls + ")",
             "snapshots": "snapshots (GET …/bindings/<name>/snapshot/<snapshot>, served without waking it)",
             "events": "events (declare handlers.on_event: [\"app:<name>:<event>\"] to be woken)"}
    for kind in ("methods", "snapshots", "events"):
        entries = ex.get(kind) or {}
        if not entries:
            continue
        lines.append(f"  {words[kind]}:")
        for name, spec in entries.items():
            floor = ""
            if spec.get("min_role"):
                judged = ("judged at your role here as the share gives it" if placement and kind == "methods"
                          else f"on {data.get('agent')}")
                floor = f" [{spec.get('min_role')} and up, {judged}]"
            lines.append(f"    - {name}: {spec.get('description', '')}{floor}")
    if len(lines) == 1:
        lines.append("  nothing yet — its app.json has no exports block.")
    if placement:
        role = placement.get("role") or ""
        acting = (f"as {role}" if role and role != "agent"
                  else "with no person behind this session, so only methods without a role floor")
        lines.append(f"Placed in this agent by {_SHARE_WORDS.get(placement.get('kind'), 'a share')} "
                     f"at {placement.get('role_cap')}: this session calls its exported methods "
                     f"{acting} ({data.get('call')}); its snapshots, events, other routes, push, "
                     "state and platform methods stay out of a session's reach.")
    if b:
        lines.append(f"Bind with: \"bindings\": [{{\"name\": \"<your name for it>\", \"agent\": "
                     f"\"{b.get('agent')}\", \"app\": \"{b.get('app')}\"}}] in your app.json, then "
                     "deploy_app — the user approves the binding on your card.")
    else:
        lines.append("No binding reaches it from here: it was found through the placement alone "
                     "(its home agent is outside this agent's reach, or it is a personal app, "
                     "never a binding target), so your server cannot call it over a binding; "
                     "your sessions call its exported methods as above.")
    return [TextContent(type="text", text="\n".join(lines))]


async def _handle_bundle(op: str, arguments: dict) -> list[TextContent]:
    """export_app / import_app (APPS.md "Blueprints and templates")."""
    payload: dict = {"session_id": SESSION_ID}
    if arguments.get("visibility"):
        payload["visibility"] = arguments["visibility"].strip().lower()
    if op == "export":
        payload["slug"] = (arguments.get("slug") or "").strip()
    else:
        payload["path"] = (arguments.get("path") or "").strip()
        if arguments.get("slug"):
            payload["slug"] = arguments["slug"].strip()
    # An import deploys what it unpacked, the rendered check included.
    data, err = await _post_hook(f"/v1/hooks/apps/{op}", payload,
                                 read=240.0 if op == "import" else 60.0)
    if data is None:
        return [TextContent(type="text", text=f"Error ({op}): {err}")]
    if op == "export":
        tasks = data.get("tasks") or []
        return [TextContent(type="text", text=(
            f"Exported '{data.get('slug')}' to {data.get('path')} ({data.get('bytes', 0)} bytes"
            + (f", {len(tasks)} task(s) by slug: {', '.join(tasks)}" if tasks else "")
            + "). Send it with send_file, or import_app it in another agent's workspace; "
            "the database never travels."))]
    if data.get("status") == "refused":
        return _deploy_reply(data.get("slug") or payload.get("slug") or "the bundle", data,
                             "import_app again")
    tasks = data.get("tasks") or {}
    head = (f"Imported into {data.get('path')} ({data.get('scope')}), app_id {data.get('app_id')}"
            + (f", {len(tasks)} task(s) created" if tasks else "") + ". ")
    if data.get("status") == "pending approval":
        return [TextContent(type="text", text=head + (
            f"Release {data.get('release')} WAITS FOR APPROVAL — an import is never pre-approved: "
            f"tell the user to open the app (page /apps/{data.get('app_id')}) and approve it on the "
            "card. Nothing runs before that."))]
    return [TextContent(type="text", text=head + f"Release {data.get('release')} is live "
                        "(an empty manifest needs no approval).")]


async def _op(handler, op: str, arguments: dict) -> list[TextContent | ImageContent]:
    return await handler(op, arguments)


# Tool name → handler. ``display_server.call_tool`` consults this after its
# own media tools, so adding a tool here is one schema plus one entry.
HANDLERS = {
    "pin_app": _handle_pin_app,
    "unpin_app": lambda a: _op(_handle_app_hook, "unpin", a),
    "list_apps": lambda a: _op(_handle_app_hook, "list", a),
    "rollback_app": lambda a: _op(_handle_app_hook, "rollback", a),
    "describe_app": _handle_describe,
    "export_app": lambda a: _op(_handle_bundle, "export", a),
    "import_app": lambda a: _op(_handle_bundle, "import", a),
    "app_push": lambda a: _op(_handle_live_hook, "push", a),
    "app_state": lambda a: _op(_handle_live_hook, "state", a),
    "open_app": lambda a: _op(_handle_live_hook, "open", a),
    "pin_file": lambda a: _op(_handle_file_pin_hook, "pin", a),
    "unpin_file": lambda a: _op(_handle_file_pin_hook, "unpin", a),
    "deploy_app": lambda a: _op(_handle_deploy_hook, "deploy", a),
    "check_app": lambda a: _op(_handle_deploy_hook, "check", a),
    "screenshot_app": lambda a: _op(_handle_deploy_hook, "screenshot", a),
    "deploy_status": lambda a: _op(_handle_deploy_hook, "status", a),
    "preview_app": lambda a: _op(_handle_deploy_hook, "preview", a),
    "app_logs": lambda a: _op(_handle_deploy_hook, "logs", a),
    "restart_app": lambda a: _op(_handle_deploy_hook, "restart", a),
    "purge_app": lambda a: _op(_handle_deploy_hook, "purge", a),
}
