"""Agent-Creator-MCP — agents create new agents from template folders, and
propose community agents, department sets and moves as cards.

The community first: ``search_community_agents`` finds a template or a
department set in the catalog, and the three ``propose_*`` tools draw a
card in the chat (the proxy's services/cards) that anyone in the chat may press; the
press installs, moves or wires through the platform's own routes with the presser's
role, never through this server. ``check_card`` reads who pressed and what happened. For
an agent nothing in the catalog fits, the agent authors a community-agent
template folder somewhere it can write (its workspace), then hands the
path to this server. The proxy loads the
folder through the same pipeline that installs catalog templates, so the
whole manifest surface — persona, required MCPs, skill packages, scheduled
tasks/triggers/notifications, agent-wide and per-user setup guides, auto
context, folder apps and checks — works identically for a locally authored
template. One difference: a catalog install carries a person's consent to
the template's apps and checks; this route carries none, so apps land
waiting for approval and checks are offered, not mandatory.

Permission resolution lives in :func:`_resolve_tool_set` and runs once at
module load using the auto-injected ``OTO_*`` env vars (mirror of
``agent-config-mcp/server.py``):

- platform ``admin`` / ``creator`` → every tool.
- everyone else (platform ``member``, service sessions with no human
  identity) → zero tools; ``tools/list`` returns an empty list.

The env check is a courtesy that keeps the tools out of a member's prompt.
The real boundary is server side: every endpoint independently requires a
platform creator/admin behind the session JWT.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
from typing import Any, Literal

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool, ToolAnnotations

from _tool_args import ToolArgs

# ---------------------------------------------------------------------------
# Env + permission matrix
# ---------------------------------------------------------------------------

AGENT_NAME = os.environ.get("OTO_AGENT_NAME", "")
# The creator-tier question, answered by the proxy in the env
# (core/sandbox/oto_env.py): a separate process cannot import the proxy and
# carries no role vocabulary of its own.
CAN_CREATE = os.environ.get("OTO_CAN_CREATE_AGENTS", "") == "true"

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")
# The session a card is drawn in (the cards route binds it to its chat).
SESSION_ID = os.environ.get("OTO_SESSION_ID", "")

_ALL_TOOLS = {
    "search_community_agents",
    "list_departments",
    "propose_install",
    "propose_department_install",
    "propose_move",
    "check_card",
    "list_building_blocks",
    "validate_agent_template",
    "create_agent",
}

CARDS_PATH = "/v1/cards"
# What the person reads on a refusal of the cards route: its own sentences
# (a 400, 403, 404 or 409 whose detail is a full sentence) are repeated,
# anything else is this one.
NO_ANSWER = "The platform did not answer. Say what you proposed and ask the person to do it in the dashboard."
#: The satellite tunnel of a machine older than 0.5.163 refuses the call itself
#: (the catalog reads, the departments, the card's draw and read).
OLDER_SATELLITE = ("This machine runs an older OtoDock satellite, which does not pass this call on. "
                   "Say what you wanted to do and ask the person to do it in the dashboard, "
                   "or to open a chat that runs on the platform.")
CARD_DRAWN = "A card is in the chat. Anyone in the chat may press it: nothing changes until someone does."


def _resolve_tool_set() -> set[str]:
    if CAN_CREATE:
        return set(_ALL_TOOLS)
    # Platform members and sessions with no human identity see nothing.
    return set()


ENABLED_TOOLS = _resolve_tool_set()


# ---------------------------------------------------------------------------
# HTTP helper (mirrors agent-config-mcp)
# ---------------------------------------------------------------------------

class _ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


# The request never left: retrying it cannot run anything twice.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
# The request may have reached the platform and be running there still: any
# transport failure once the connection opened (a timeout, a dropped or reset
# connection while the platform worked).
_NO_ANSWER = httpx.TransportError


async def _request(
    method: str, path: str, *, retry: Literal["all", "connect"] = "all", **kwargs,
) -> Any:
    """``retry="all"`` sends a call once more after a 5xx or any transport
    error; ``"connect"`` only after a connection that never opened, for a
    call a second run must not repeat or that a retry cannot fix."""
    headers = kwargs.pop("headers", {}) or {}
    if API_KEY and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {API_KEY}"
    if AGENT_NAME and "X-Agent-Name" not in headers:
        headers["X-Agent-Name"] = AGENT_NAME
    url = f"{PROXY_URL}{path}"
    # Installs cascade MCP + skill-package downloads — give them room.
    timeout = kwargs.pop("timeout", 120.0)
    attempts = 2
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
            if resp.status_code >= 500 and retry == "all" and attempt < attempts - 1:
                await asyncio.sleep(1.0)
                continue
            if resp.status_code >= 400:
                detail: Any = resp.text
                try:
                    js = resp.json()
                    detail = js.get("detail") or js
                except (ValueError, AttributeError):
                    pass
                raise _ApiError(
                    f"{method} {path} → {resp.status_code}: {detail}", status=resp.status_code,
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        except _ApiError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1 and (retry == "all" or isinstance(exc, _NOT_SENT)):
                await asyncio.sleep(1.0)
                continue
            raise _ApiError(f"{method} {path}: {exc}") from exc
    if last_exc:
        raise _ApiError(f"{method} {path}: {last_exc}")
    return None


# ---------------------------------------------------------------------------
# The community, the departments, the cards
# ---------------------------------------------------------------------------

def _refusal_sentence(exc: _ApiError) -> str:
    """A refusal the person may read: the route's own sentence (it ends
    with a full stop) or the one sentence for everything else."""
    text = str(exc)
    detail = text.split(": ", 2)[-1] if "→" in text else text
    if exc.status == 403 and "path-not-allowlisted" in detail:
        return OLDER_SATELLITE
    if exc.status in (400, 403, 404, 409) and detail.endswith("."):
        return detail
    return NO_ANSWER


def _score(words: list[str], *fields: tuple[str, int]) -> int:
    score = 0
    for text, weight in fields:
        low = (text or "").lower()
        for w in words:
            if w and w in low:
                score += weight
    return score


async def _tool_search_community_agents(query: str) -> str:
    words = [w for w in query.lower().split() if len(w) > 1]
    try:
        agents = await _request("GET", "/v1/community/agents", retry="connect")
        sets = await _request("GET", "/v1/community/departments", retry="connect")
    except _ApiError as exc:
        return f"The catalog could not be read: {_refusal_sentence(exc)}"
    rows: list[tuple[int, str]] = []
    set_of: dict[str, list[str]] = {}
    for s in (sets or {}).get("departments") or []:
        for m in s.get("members") or []:
            set_of.setdefault(str(m.get("template") or ""), []).append(str(s.get("slug") or ""))
    for a in (agents or {}).get("agents") or []:
        score = _score(words, (a.get("display_name", ""), 5), (a.get("slug", ""), 4),
                       (" ".join(a.get("tags") or []), 3), (a.get("description", ""), 1))
        if words and not score:
            continue
        installed = ", ".join(a.get("installed_as") or []) or "not installed"
        line = (f"- template `{a.get('slug')}` ({a.get('display_name')}, {a.get('version')}): "
                f"{(a.get('description') or '').strip()[:160]} Installed: {installed}.")
        if set_of.get(a.get("slug")):
            line += f" Part of the set: {', '.join(set_of[a['slug']])}."
        rows.append((score, line))
    for s in (sets or {}).get("departments") or []:
        score = _score(words, (s.get("name", ""), 5), (s.get("slug", ""), 4),
                       (" ".join(s.get("tags") or []), 3), (s.get("description", ""), 1),
                       (" ".join(m.get("display_name", "") for m in s.get("members") or []), 2))
        if words and not score:
            continue
        members = ", ".join(f"{m.get('display_name')} ({m.get('level')})" for m in s.get("members") or [])
        installed = ", ".join(d.get("name", "") for d in s.get("installed") or []) or "not installed"
        problems = "; ".join(s.get("problems") or [])
        line = (f"- department set `{s.get('slug')}` ({s.get('name')}, {s.get('version')}): "
                f"{(s.get('description') or '').strip()[:160]} Members: {members}. Installed: {installed}.")
        if problems or s.get("missing_members"):
            line += " Cannot be installed on this platform: " + (problems or "a member template is not in the catalog.")
        rows.append((score + 1, line))
    rows.sort(key=lambda r: -r[0])
    if not rows:
        return ("Nothing in the community catalog matches. Ask the person whether to write a new agent "
                "from a template folder instead.")
    head = ["The community catalog, best matches first. Propose one with `propose_install` (a template), "
            "`propose_department_install` (a set) or write a new one only when nothing fits.", ""]
    return "\n".join(head + [r[1] for r in rows[:12]])


async def _tool_list_departments() -> str:
    try:
        data = await _request("GET", "/v1/departments", retry="connect")
    except _ApiError as exc:
        return f"The departments could not be read: {_refusal_sentence(exc)}"
    depts = (data or {}).get("departments") or []
    if not depts:
        return "There is no department yet. A set from the community catalog makes one, or a person makes one on the Agents page."
    lines = ["The departments this person may see, with the ids a move names:"]
    for d in depts:
        levels = ", ".join(f"{lv.get('name')} (`{lv.get('id')}`)" for lv in d.get("levels") or [])
        members = ", ".join(f"{m.get('display_name')} ({m.get('name')})" for m in d.get("members") or []) or "nobody"
        lines.append(f"- {d.get('name')} (`{d.get('id')}`), delegation {d.get('mode')}, reach {d.get('reach')}. "
                     f"Levels: {levels}. Members: {members}.")
    return "\n".join(lines)


async def _draw(buttons: list[dict[str, Any]], note: str) -> str:
    """One card through the cards route; the person's refusal sentence back
    to the agent when the route refuses."""
    try:
        data = await _request("POST", CARDS_PATH, json={"session_id": SESSION_ID, "buttons": buttons,
                                                        "note": note.strip()[:1000]}, retry="connect")
    except _ApiError as exc:
        return f"No card was drawn: {_refusal_sentence(exc)}"
    card_id = (data or {}).get("card_id", "")
    return f"{CARD_DRAWN} The card's id is {card_id}: `check_card` reads who pressed it and what happened."


async def _tool_propose_install(template_slug: str, target_slug: str, display_name: str,
                                department_id: str, level_id: str, note: str) -> str:
    params: dict[str, Any] = {"template_slug": template_slug.strip()}
    if target_slug.strip():
        params["target_slug"] = target_slug.strip()
    if display_name.strip():
        params["display_name"] = display_name.strip()
    if department_id.strip() or level_id.strip():
        params["department_id"] = department_id.strip()
        params["level_id"] = level_id.strip()
    return await _draw([{"id": "install", "kind": "do", "action": "install_agent", "params": params}], note)


async def _tool_propose_department_install(set_slug: str, department_name: str, note: str) -> str:
    params: dict[str, Any] = {"set_slug": set_slug.strip()}
    if department_name.strip():
        params["department_name"] = department_name.strip()
    return await _draw([{"id": "install", "kind": "do", "action": "install_department_set", "params": params}], note)


async def _tool_propose_move(agent: str, department_id: str, level_id: str, note: str) -> str:
    params = {"agent": agent.strip(), "department_id": department_id.strip(), "level_id": level_id.strip()}
    return await _draw([{"id": "move", "kind": "do", "action": "move_agent", "params": params}], note)


async def _tool_check_card(card_id: str) -> str:
    try:
        # The id is the agent's text: quoted, it names one card and no other route.
        s = await _request("GET", f"{CARDS_PATH}/{urllib.parse.quote(card_id.strip(), safe='')}",
                           retry="connect")
    except _ApiError as exc:
        return f"The card could not be read: {_refusal_sentence(exc)}"
    if not isinstance(s, dict):
        return NO_ANSWER
    if s.get("expired"):
        return f"The card expired: {s['expired']}"
    clicks = [c for c in s.get("clicks") or [] if isinstance(c, dict)]
    # Who pressed: an older platform's state names nobody.
    by = str(clicks[0].get("by") or "Someone") if clicks else "Someone"
    outcome = s.get("outcome") or {}
    if outcome:
        text = outcome.get("text") or ""
        lines = [f"{by} pressed the button. {text}".strip()]
        if outcome.get("status") == "running":
            lines.append("It is still running. Read the card again in a moment.")
        for extra in outcome.get("lines") or []:
            lines.append(f"- {extra}")
        return "\n".join(lines)
    said = [c for c in clicks if c.get("status") == "said"]
    if said:
        labels = ", ".join(c.get("detail") or c.get("button") for c in said)
        return f"{by} answered: {labels}"
    return "Nobody has pressed a button yet."


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _tool_list_building_blocks() -> str:
    data = await _request("GET", "/v1/agents/local-template/building-blocks")
    if not isinstance(data, dict):
        return "❌ Error: unexpected response from building-blocks"

    lines: list[str] = []
    if data.get("catalog_error"):
        lines.append(
            f"> Catalog unreachable ({data['catalog_error']}) — only what is "
            "already installed on this platform is listed below.\n"
        )

    lines.append("# MCPs")
    lines.append(
        "Use the `name` column verbatim in `mcps.json`. Core MCPs are "
        "assigned to every agent automatically — never list them.\n"
    )
    lines.append("| name | installed | category | what it does |")
    lines.append("|---|---|---|---|")
    for m in data.get("mcps", []):
        installed = "yes" if m.get("installed") else "no — admin install needed"
        desc = (m.get("description") or "").replace("|", "/")
        lines.append(
            f"| `{m.get('name', '')}` | {installed} | {m.get('category', '')} | {desc} |",
        )

    skills = data.get("skill_packages", [])
    lines.append("\n# Skill packages")
    if skills:
        lines.append(
            "For `skills.json`. Packages not yet installed can only be "
            "pulled in by a platform admin.\n"
        )
        lines.append("| name | installed | what it teaches |")
        lines.append("|---|---|---|")
        for s in skills:
            desc = (s.get("description") or "").replace("|", "/")
            lines.append(
                f"| `{s.get('name', '')}` | "
                f"{'yes' if s.get('installed') else 'no — admin install needed'} | {desc} |",
            )
    else:
        lines.append("_None available._")

    examples = data.get("check_examples", [])
    if examples:
        lines.append("\n# Check examples")
        lines.append(
            "A template's `checks/<name>/check.json` (plus its script) has one "
            "of these shapes — copy the closest from the checks-mcp examples "
            "folder and edit it.\n"
        )
        lines.append("| example | sections | what it judges |")
        lines.append("|---|---|---|")
        for ex in examples:
            desc = (ex.get("description") or "").replace("|", "/")
            lines.append(
                f"| `{ex.get('name', '')}` | {', '.join(ex.get('sections') or [])} | {desc} |",
            )

    lines.extend(await _engine_lines())

    taken = data.get("agent_slugs", [])
    if taken:
        lines.append(f"\n# Slugs already taken\n{', '.join(f'`{s}`' for s in taken)}")
    return "\n".join(lines)


# The engine descriptor's role an install may make an agent's engine; a
# supporting engine is only ever enabled by hand.
_CODING_ROLE = "coding"


async def _engine_lines() -> list[str]:
    """The engines and models a template task's pin may name, as THIS
    install has them (``GET /v1/execution-layers``: the shared pool's
    models, never one person's own accounts)."""
    lines = ["\n# Engines a task pin may name"]
    try:
        layers = await _request("GET", "/v1/execution-layers")
    except _ApiError as exc:
        lines.append(f"_The engines could not be read ({exc}); write the tasks without a pin._")
        return lines
    engines = {k: v for k, v in layers.items() if isinstance(v, dict)} if isinstance(layers, dict) else {}
    if not engines:
        lines.append("_No engine on this install._")
        return lines

    def _identity(layer: dict) -> dict:
        identity = layer.get("identity")
        return identity if isinstance(identity, dict) else {}

    def _order(item: tuple[str, dict]) -> tuple[bool, int, str]:
        identity = _identity(item[1])
        order = identity.get("sort_order")
        return identity.get("role") != _CODING_ROLE, order if isinstance(order, int) else 0, item[0]

    lines.append(
        "These are this install's engines and models; another install has its own. "
        "A task in `tasks.json` may pin `layer` (an engine id) and `override_model` "
        "(a model id). An install allows every engine below on the new agent and makes "
        "one its default: the first engine below that can be the agent's engine and that "
        "both the platform and the installing person have connected, else the platform's "
        "default engine. A pin holds when `layer`, if set, is an engine below and "
        "`override_model`, if set, is one of its models, or, with no `layer`, a model of "
        "any engine below; any other pin is dropped at install and on every update, and "
        "the task runs with no model choice. Leave a task unpinned unless it truly needs "
        "another model.\n"
    )
    lines.append("| engine | can be the agent's engine | connected here | models |")
    lines.append("|---|---|---|---|")
    for path, layer in sorted(engines.items(), key=_order):
        identity = _identity(layer)
        pickable = "yes" if identity.get("role") == _CODING_ROLE else "no — enabled by hand only"
        models = []
        for m in layer.get("models") or []:
            model_id = m.get("value") if isinstance(m, dict) else ""
            if not model_id:
                continue  # the "System Default" entry: no id to pin
            tier = m.get("tier_label") or ""
            models.append(f"`{model_id}`" + (f" ({tier})" if tier else ""))
        lines.append(
            f"| `{path}` | {pickable} | {'yes' if layer.get('configured') else 'no'} | "
            f"{', '.join(models) or '—'} |",
        )
    return lines


def _format_plan(result: dict) -> list[str]:
    """Render the shared validate/create plan blocks."""
    lines: list[str] = []
    mcp_plan = result.get("mcp_plan") or {}
    ready = mcp_plan.get("ready") or []
    needs_install = mcp_plan.get("needs_install") or []
    needs_request = mcp_plan.get("needs_admin_request") or []
    if ready or needs_install or needs_request:
        lines.append("\n**MCPs**")
        if ready:
            lines.append(f"- ready: {', '.join(ready)}")
        if needs_install:
            lines.append(f"- will be installed: {', '.join(needs_install)}")
        if needs_request:
            lines.append(
                f"- needs an admin to approve: {', '.join(needs_request)}",
            )
    skill_plan = result.get("skill_plan") or {}
    if any(skill_plan.values()):
        lines.append("\n**Skill packages**")
        for key, label in (
            ("ready", "ready"),
            ("missing", "not in any catalog"),
            ("admin_required", "needs a platform admin to install"),
        ):
            if skill_plan.get(key):
                lines.append(f"- {label}: {', '.join(skill_plan[key])}")
    seeds = result.get("seeds") or {}
    if seeds:
        bits = [
            f"{seeds.get('tasks', 0)} task(s)",
            f"{seeds.get('triggers', 0)} trigger(s)",
            f"{seeds.get('notifications', 0)} notification(s)",
            f"{seeds.get('dashboards', 0)} dashboard(s)",
            f"{seeds.get('context_files', 0)} context file(s)",
        ]
        if seeds.get("has_setup"):
            bits.append("agent setup guide")
        if seeds.get("has_user_setup"):
            bits.append("per-user onboarding")
        lines.append(f"\n**Will seed**: {', '.join(bits)}")
        apps = seeds.get("apps") or []
        if apps:
            lines.append("\n**Apps** (deployed waiting for approval — an agent never "
                         "pre-approves an app's buttons)")
            for a in apps:
                who = ("each member approves their own copy" if a.get("visibility") == "user"
                       else "a manager approves the shared copy")
                lines.append(f"- `{a.get('slug', '')}` {a.get('title') or ''}: {who}")
        checks = seeds.get("checks") or []
        if checks:
            lines.append("\n**Checks** (offered, not mandatory, until a manager consents "
                         "in the dashboard): " + ", ".join(f"`{c.get('name', '')}`" for c in checks))
    return lines


async def _tool_validate_agent_template(path: str) -> str:
    if not path.strip():
        return "❌ Error: path is required"
    # Only a connection that never opened is retried: a 503 or a timeout is
    # the folder read failing or running long, and a second attempt would
    # double the wait before the agent hears it.
    result = await _request(
        "POST", "/v1/agents/local-template/validate", json={"path": path.strip()},
        retry="connect",
    )
    if not isinstance(result, dict):
        return "❌ Error: unexpected response from validate"

    if not result.get("ok"):
        errors = result.get("errors") or ["unknown validation error"]
        body = "\n".join(f"- {e}" for e in errors)
        return f"❌ Template is not valid yet:\n{body}\n\nFix these and validate again."

    lines = [f"✅ Template is valid — slug `{result.get('slug', '')}`."]
    if not result.get("slug_available", True):
        lines.append(
            f"⚠️ That slug is taken. Pass `target_slug` to `create_agent` — "
            f"suggested: `{result.get('suggested_slug', '')}`.",
        )
    lines.extend(_format_plan(result))
    lines.append("\nCall `create_agent` with the same path to install it.")
    return "\n".join(lines)


async def _tool_create_agent(path: str, target_slug: str) -> str:
    if not path.strip():
        return "❌ Error: path is required"
    body: dict[str, Any] = {"path": path.strip()}
    if target_slug.strip():
        body["target_slug"] = target_slug.strip()
    try:
        result = await _request(
            "POST", "/v1/agents/install-from-local-template", json=body, retry="connect",
        )
    except _ApiError as exc:
        if isinstance(exc.__cause__, _NO_ANSWER) and not isinstance(exc.__cause__, _NOT_SENT):
            # Never sent twice: the first install may be running still, and
            # a second one would find its slug taken and offer another.
            raise _ApiError(
                f"the install got no answer ({type(exc.__cause__).__name__}) "
                "and may still be running on the platform. Before calling "
                "`create_agent` again, check whether its slug (`target_slug`, else "
                "the template's) is taken: `list_building_blocks` lists every taken "
                "slug. If it is, the agent was created; never install it again under "
                "a suggested slug.",
            ) from exc
        if exc.status == 503:
            # The platform answers 503 only while reading the folder, before
            # the install starts.
            raise _ApiError(
                f"{exc}. Nothing was created; call `create_agent` again in a moment.",
                status=exc.status,
            ) from exc
        raise
    if not isinstance(result, dict):
        return "❌ Error: unexpected response from install"

    slug = result.get("agent_slug", "")
    lines = [
        f"✅ Created agent `{slug}` — you are its manager.",
        f"Open it at {result.get('dashboard_path', f'/agents/{slug}')}",
    ]
    ready = result.get("ready_mcps") or []
    if ready:
        lines.append(f"\n**MCPs enabled**: {', '.join(ready)}")
    requests = result.get("created_requests") or []
    if requests:
        names = ", ".join(
            r.get("mcp_name", "") if isinstance(r, dict) else str(r) for r in requests
        )
        lines.append(
            f"\n⏳ **Waiting on a platform admin** to approve: {names}. "
            "Those tools appear once approved.",
        )
    skills = result.get("skill_packages") or {}
    if skills.get("failed"):
        lines.append(f"\n⚠️ Skill packages that failed: {', '.join(skills['failed'])}")
    seeded = [
        f"{result.get('seeded_tasks', 0)} task(s)",
        f"{result.get('seeded_triggers', 0)} trigger(s)",
        f"{result.get('seeded_notifications', 0)} notification(s)",
        f"{result.get('seeded_dashboards', 0)} dashboard(s)",
    ]
    lines.append(f"\n**Seeded**: {', '.join(seeded)}")
    dropped = result.get("task_pins_dropped") or []
    if dropped:
        lines.append(
            "\n⚠️ **Task pins the new agent cannot run** (those tasks run on its "
            "default model and engine): " + "; ".join(str(d) for d in dropped),
        )
    if result.get("seeded_dashboards"):
        lines.append(
            "Seeded dashboards are pinned already (display-only — no "
            "buttons to approve); the new agent can re-pin them with "
            "actions later.",
        )
    apps = result.get("seeded_apps") or {}
    if any(apps.get(k) for k in ("seeded", "user", "pending", "failed")):
        live = list(apps.get("seeded") or []) + list(apps.get("user") or [])
        if live:
            lines.append(f"\n**Apps live**: {', '.join(live)}")
        pending = apps.get("pending") or []
        if pending:
            lines.append(
                "\n⏳ **Apps waiting for approval** (an agent never pre-approves "
                "an app's buttons; the person approves on the app's card): "
                + ", ".join(p.get("slug", "") if isinstance(p, dict) else str(p) for p in pending),
            )
        failed = apps.get("failed") or []
        if failed:
            lines.append("\n⚠️ Apps that failed: " + "; ".join(
                f"{f.get('slug', '')} ({f.get('reason', '')})" if isinstance(f, dict) else str(f)
                for f in failed))
    checks = result.get("seeded_checks") or {}
    if checks.get("offered") or checks.get("consented"):
        lines.append(
            "\n**Checks offered**: " + ", ".join(
                list(checks.get("consented") or []) + list(checks.get("offered") or []))
            + " — a manager makes them mandatory from the dashboard.",
        )
    if checks.get("failed"):
        lines.append("\n⚠️ Checks that failed: " + "; ".join(
            f"{f.get('name', '')} ({f.get('reason', '')})" if isinstance(f, dict) else str(f)
            for f in checks["failed"]))
    if result.get("setup_md_copied"):
        lines.append(
            "The agent has a setup guide in its context — open a chat with it "
            "to finish configuration.",
        )
    if result.get("ignored_fields"):
        lines.append(
            f"\nℹ️ Ignored (admin-only): {', '.join(result['ignored_fields'])}",
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_NOTE = {"type": "string", "description": "Your own words for the person, one or two sentences, shown on the card apart from the platform's."}

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_community_agents": {
        "description": (
            "Search the community catalog for an agent template or a department set "
            "before writing a new agent: the best matches with their slugs, versions, "
            "what is installed already and which set a template belongs to. Call this "
            "FIRST when asked for a new agent, a team or a department."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A few words: the job, the domain, a name."}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    "list_departments": {
        "description": (
            "The departments this person may see, each with its id, its levels and their ids, "
            "its members and its delegation wiring. A move or an install into a department names "
            "these ids."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    "propose_install": {
        "description": (
            "Draw a card proposing to install a community agent template. Whoever presses it "
            "installs it with their own role through the platform. Nothing is installed by this "
            "call. Optionally the department and level it goes into. When the person manages "
            "this agent, the card offers to let this agent hand work to the new one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_slug": {"type": "string", "description": "The template's slug from the search."},
                "target_slug": {"type": "string", "description": "The slug the agent is installed as, when the template's is taken or another is wanted."},
                "display_name": {"type": "string", "description": "The agent's name on its card, when another than the template's."},
                "department_id": {"type": "string", "description": "A department's id from list_departments, with level_id."},
                "level_id": {"type": "string", "description": "A level's id of that department."},
                "note": _NOTE,
            },
            "required": ["template_slug"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    "propose_department_install": {
        "description": (
            "Draw a card proposing to install a department set from the community catalog: "
            "the department with its levels and wiring, every member agent at its level, the "
            "head's library shared with the team, the schedules paused. An agent the person "
            "manages that was installed from one of the set's templates and sits in no "
            "department joins it instead of a new copy, and the card names it. Whoever presses "
            "it runs it with their own role. Nothing is installed by this call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "set_slug": {"type": "string", "description": "The set's slug from the search."},
                "department_name": {"type": "string", "description": "The department's name, when another than the set's (a boat's name, a team's)."},
                "note": _NOTE,
            },
            "required": ["set_slug"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    "propose_move": {
        "description": (
            "Draw a card proposing to move an agent into a department at a level. Whoever "
            "presses it moves it with their own role (an admin, or a creator who manages the agent); "
            "nothing moves by this call. Use list_departments for the ids."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "The agent's slug."},
                "department_id": {"type": "string", "description": "The department's id."},
                "level_id": {"type": "string", "description": "The level's id in that department."},
                "note": _NOTE,
            },
            "required": ["agent", "department_id", "level_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    "check_card": {
        "description": (
            "What happened to a card you drew: not yet pressed, who answered a say button and "
            "with what, or who pressed it and what the action did (an install still running says "
            "so). Read it when someone says they pressed it, never in a loop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string", "description": "The card's id the draw answered with."}},
            "required": ["card_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    "list_building_blocks": {
        "description": (
            "List the MCPs and skill packages a new agent can be given, the "
            "check examples a template's checks/ folder can start from, the "
            "engines and models a scheduled task's pin may name, plus the "
            "agent slugs already taken. Every list is this install's. Call "
            "this BEFORE writing `mcps.json` — MCP names are canonical "
            "platform names and are not always guessable."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    "validate_agent_template": {
        "description": (
            "Dry-run a template folder you have written: checks the manifest, "
            "persona, required MCPs and skill packages, every app under apps/ "
            "and user-apps/ (the deploy's own validator and static checks), "
            "every check under checks/, and slug availability without "
            "creating anything. Returns the exact errors to fix. Always "
            "validate before `create_agent`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to the template folder you wrote, e.g. "
                        "`/workspace/new-agents/research-assistant`."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    "create_agent": {
        "description": (
            "Create a new agent on the platform from a validated template "
            "folder. You become its manager. Required MCPs are installed (or "
            "queued for admin approval), skill packages assigned, and any "
            "scheduled tasks, triggers, notifications, setup guides, apps and "
            "checks seeded. Apps deploy waiting for approval on their cards "
            "and checks are offered, never mandatory: an agent cannot consent "
            "on a person's behalf. The mode in agent.json fits the team: "
            "default_scope user for people who work alone and hand finals over, "
            "agent for a department's desk. Propose several agents only when "
            "asked to automate a company function, a department or several "
            "projects with different people."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the template folder.",
                },
                "target_slug": {
                    "type": "string",
                    "description": (
                        "Optional — install under a different slug than the "
                        "manifest's (use when the manifest slug is taken)."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    },
}


_TOOL_HANDLERS = {
    "search_community_agents": lambda args: _tool_search_community_agents(args.get("query", "")),
    "list_departments": lambda args: _tool_list_departments(),
    "propose_install": lambda args: _tool_propose_install(
        args.get("template_slug", ""), args.get("target_slug", ""), args.get("display_name", ""),
        args.get("department_id", ""), args.get("level_id", ""), args.get("note", ""),
    ),
    "propose_department_install": lambda args: _tool_propose_department_install(
        args.get("set_slug", ""), args.get("department_name", ""), args.get("note", ""),
    ),
    "propose_move": lambda args: _tool_propose_move(
        args.get("agent", ""), args.get("department_id", ""), args.get("level_id", ""), args.get("note", ""),
    ),
    "check_card": lambda args: _tool_check_card(args.get("card_id", "")),
    "list_building_blocks": lambda args: _tool_list_building_blocks(),
    "validate_agent_template": lambda args: _tool_validate_agent_template(
        args.get("path", ""),
    ),
    "create_agent": lambda args: _tool_create_agent(
        args.get("path", ""), args.get("target_slug", ""),
    ),
}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("agent-creator-mcp")
TOOL_ARGS = ToolArgs()


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_ARGS.listed([
        Tool(name=name, description=schema["description"], inputSchema=schema["inputSchema"],
             annotations=ToolAnnotations(**schema["annotations"]))
        for name, schema in _TOOL_SCHEMAS.items()
        if name in ENABLED_TOOLS
    ])


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    arguments, refusal = TOOL_ARGS.check(name, arguments)
    if refusal is not None:
        return refusal
    if name not in ENABLED_TOOLS:
        return [TextContent(type="text", text=f"❌ Error: tool '{name}' not available in this session")]
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"❌ Error: unknown tool '{name}'")]
    try:
        result = await handler(arguments or {})
    except _ApiError as exc:
        return [TextContent(type="text", text=f"❌ Error: {exc}")]
    except Exception as exc:  # noqa: BLE001  # the tool answers with the error text whatever failed
        return [TextContent(type="text", text=f"❌ Error: {type(exc).__name__}: {exc}")]
    return [TextContent(type="text", text=str(result))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
