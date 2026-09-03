"""Meeting agent config + prompt builders.

Pure-ish builders factored out of ``services.meetings.meeting_orchestrator``: the
meeting-agent system-prompt suffix, the per-participant ``AgentConfig``
builder, the per-turn prompt builder, and the tolerant ``direct_to`` argument
parser. No shared mutable meeting state lives here — the round loop, the live
turn runners, and the ``_meeting_session_layers`` registry stay in the
orchestrator.

``meeting_orchestrator`` re-exports these names so existing call sites and
tests (``meeting_orchestrator.build_meeting_agent_config`` /
``_parse_directed_agents`` / ``build_turn_prompt``) are unchanged.
"""

import asyncio
import json
import logging

import config
from storage import database as task_store
from storage import agent_store
from services.mcp import mcp_registry
from services.mcp import dynamic_context
from services.engines import subscription_pool
from core.execution_layer import AgentConfig
from auth.path_policy import SecurityContext, build_permission_context

logger = logging.getLogger("claude-proxy")


# ---------------------------------------------------------------------------
# Meeting agent config
# ---------------------------------------------------------------------------

_MEETING_AGENT_SUFFIX = """

---

## Meeting Agent Rules

You are participating in a multi-agent meeting (no interactive user prompts).

- **Be concise** — each response should be 1-3 paragraphs max. Provide data, not filler.
- **ALWAYS call `direct_to(agents=[...])` exactly ONCE, as the LAST action of your response, then END your turn immediately** — no further text and no repeat calls (repeats only waste slow round-trips; the last call wins). Text written after the call is discarded.
- **Write your full message as normal response text BEFORE calling `direct_to`.** The tool only routes — its arguments are NOT shown to the other agents; anything you put in them is lost.
  If you don't call direct_to, your response broadcasts to ALL participants and they ALL respond — avoid this unless the moderator explicitly wants input from everyone.
- **The other participants receive ONLY the response text you write.** They can NOT see your tool results or your thinking. After gathering data with tools you MUST write the findings out as plain response text before routing — otherwise the others receive nothing and the meeting stalls on a restatement round.
- **As participant**: After delivering your report or answering a question, **always direct back to the moderator** — not to other participants. Only direct to another participant if the moderator explicitly asked you to coordinate with them.
- **As participant**: When you have nothing more to add, call `propose_conclude` or `leave_meeting` immediately — do NOT say "nothing further" without using one of these tools.
- **As moderator**: Direct to specific agents with clear questions. Use `end_meeting` to conclude — your final response is the meeting summary.
- **Do NOT** repeat or summarize information already shared by other participants.
- **Do NOT** respond just to acknowledge — only speak when you have new information or a question.
- **Do NOT** use the Agent tool, background subagents, or delegate_task during meetings.
"""


async def build_meeting_agent_config(
    agent_name: str,
    meeting: dict,
    session_id: str,
) -> AgentConfig:
    """Build AgentConfig for a meeting participant session."""
    scope = meeting["scope"]
    created_by = meeting.get("created_by")

    # Resolve identity + visibility exactly like the task builder
    # (core/config/task_config_builder.py). This is what forces a Shared-only
    # participant to AGENT scope even inside a user-scope meeting:
    # ``resolve_task_identity`` returns ``creds_user_sub=None`` / ``scope='agent'``
    # for a Shared-only agent (so the session draws on the platform pool, never
    # the meeting creator's subscription), and ``resolve_visibility`` clamps the
    # mount + owns session_scope / config_visible / available_scopes for the
    # prompt + sandbox decouple.
    from core.config.task_config_builder import resolve_task_identity
    from core.session.visibility import resolve_visibility
    identity = resolve_task_identity(agent_name, scope, created_by)
    task_username = identity.username           # REAL creator (attribution); "" for agent scope
    task_role = identity.role
    user_sub_for_creds = identity.creds_user_sub  # None for agent scope
    vis = resolve_visibility(
        agent_name,
        username=task_username or "",
        user_role=task_role or "",
        user_sub=user_sub_for_creds or "",
        scope_override=identity.scope,
    )

    db_targets = agent_store.get_delegation_targets(agent_name)
    if identity.scope == "user" and user_sub_for_creds:
        user_agents = set(task_store.get_user_agents(user_sub_for_creds))
        resolved_targets = [t for t in db_targets if t in user_agents]
    else:
        resolved_targets = db_targets

    # Per-session ctx for ${session.*} resolution in MCP agent_env declarations
    session_task_owner = user_sub_for_creds if identity.scope == "user" else ""
    session_task_username = task_username if (session_task_owner and task_username) else ""

    is_admin_only = agent_store.is_admin_only(agent_name)
    agent_info = agent_store.get_agent(agent_name)

    # Resolve the execution target + placement facts BEFORE the MCP config /
    # prompt below, so device-local MCPs (computer / browser / app control)
    # attach only on a satellite that grants the capability. Meetings inherit
    # the creator's user_sub for target resolution (user-paired override) or
    # fall to the agent-level default (admin-paired). Resolve ONCE (user
    # override > agent default > local) so SecurityContext metadata and
    # AgentConfig.execution_target agree; get_target_metadata expects the
    # RESOLVED target (the raw DB default mislabeled user-override targets).
    from storage import remote_store as _remote_store
    resolved_target = (await asyncio.to_thread(
        _remote_store.resolve_execution_target,
        agent_name, user_sub_for_creds, task_role,
    ))[0]
    _meeting_target_kind, _meeting_target_label = await asyncio.to_thread(
        _remote_store.get_target_metadata,
        resolved_target, user_sub_for_creds, agent_name,
    )
    is_remote = _meeting_target_kind in ("admin_remote", "user_remote")
    target_has_display = await asyncio.to_thread(
        _remote_store.get_target_has_display, _meeting_target_kind, resolved_target,
    )
    target_device_grants = await asyncio.to_thread(
        _remote_store.get_target_device_grants, _meeting_target_kind, resolved_target,
    )

    # Codex needs the MCP config in TOML (config.toml [mcp_servers.*]), not the
    # Claude JSON format — a JSON blob written into config.toml makes Codex's
    # parser exit 1. Mirrors core/config/config_builder.py (the chat path).
    execution_path = (agent_info or {}).get("execution_path", "claude-code-cli")
    mcp_format = "toml" if execution_path == "codex-cli" else "json"

    mcp_config, credential_env, excluded_mcps, secret_bundles, bash_env_keys = (
        await asyncio.to_thread(
            mcp_registry.build_session_mcp_config,
            agent_name,
            user_sub_for_creds,
            task_mode=True,
            # ALSO match "meeting" in exclude_from (union with "task") — keeps
            # the tool config in step with the prompt catalog, which filters by
            # client_type "meeting".
            meeting_mode=True,
            task_scope=identity.scope,
            delegation_targets=resolved_targets,
            extra_mcps=["meetings-mcp"],
            mcp_config_format=mcp_format,
            task_owner=session_task_owner,
            task_username=session_task_username,
            is_remote=is_remote,
            target_has_display=target_has_display,
            target_device_grants=target_device_grants,
        )
    )

    # For TOML (Codex): inject the resolved credential env INTO the config.toml
    # env sections (Codex doesn't inherit parent env for MCP servers).
    if mcp_format == "toml" and mcp_config and credential_env:
        mcp_config = await asyncio.to_thread(
            mcp_registry.inject_credential_env_into_toml,
            mcp_config, credential_env,
            exclude_keys=bash_env_keys,
        )

    task_security = SecurityContext(
        role=task_role,
        username=task_username,
        agent=agent_name,
        is_admin_agent=is_admin_only,
        target_kind=_meeting_target_kind,
        target_label=_meeting_target_label,
        # Visibility-modes: a Shared-only participant mounts agent scope even in a
        # user-scope meeting (no user dirs; shared workspace) — drives the prompt's
        # scope/folder variants + the sandbox mount decouple.
        session_scope=vis.mount_scope,
        config_visible=vis.config_visible,
        available_scopes=vis.available_scopes,
        # PINNED False: meetings record the real convener, so a naive
        # provenance thread would grant a manager-convened agent-scope
        # meeting knowledge RW — meetings are conversation, not curated
        # schedules, and stay outside the knowledge_rw opt-in.
        knowledge_rw=False,
    )

    # Pass user_sub + user_role so manifest agent_context blocks
    # resolve ${account.*}, ${credential.*}, ${user.*} tokens for the
    # right scope. Agent-scope meetings fall back to service accounts.
    # Async — builder blocks invoke remote MCP tools.
    assigned_mcp_names = [m.name for m in (mcp_registry.get_agent_mcps(
        agent_name, is_remote=is_remote, target_has_display=target_has_display,
        target_device_grants=target_device_grants,
    ) or [])]
    dynamic_contexts = await dynamic_context.get_dynamic_contexts(
        agent_name, assigned_mcp_names,
        user_sub=user_sub_for_creds or "",
        user_role=task_role or "",
        delegation_targets=resolved_targets,
        # Pre-resolved off-loop like the chat/task builders (providers are
        # no-I/O): participants get the layered delegation roster too, and
        # the meetings block follows the creator's access in user-scope
        # meetings (agent-scope passes "" → roster fallback).
        delegation_roster=await asyncio.to_thread(
            dynamic_context.build_delegation_roster, resolved_targets,
        ),
        meetings_access=await asyncio.to_thread(
            dynamic_context.build_meetings_access,
            user_sub_for_creds or "", task_role or "",
        ),
        # Agent-scope participants are no-user sessions and may read their
        # delegation targets during the meeting (the edge is the grant).
        nouser_reads=not user_sub_for_creds,
    )

    agent_prompt = config.build_agent_prompt(
        agent_name,
        username=vis.mount_username,
        role=task_role,
        excluded_mcps=excluded_mcps or None,
        dynamic_contexts=dynamic_contexts or None,
        sandboxed=True,
        client_type="meeting",
        is_remote=is_remote,
        target_has_display=target_has_display,
        target_device_grants=target_device_grants,
        mount_shared=vis.mount_shared,
    )
    agent_prompt = (agent_prompt or "") + build_permission_context(
        task_security,
        assigned_mcp_names=tuple(assigned_mcp_names),
        execution_path=execution_path or "",
    )
    agent_prompt += _MEETING_AGENT_SUFFIX

    # Resolve model and effort from agent's configured defaults
    resolved_model = config.get_cli_model(agent_name)
    resolved_effort = config.get_cli_effort(agent_name)

    # PROXY_TASK_OWNER/USERNAME/SCOPE flow to MCPs via manifest agent_env
    # declarations using ${session.*} tokens (resolved at config-build time
    # above). No direct injection here — declared in: meetings-mcp,
    # notifications-mcp, schedules-mcp, delegation-mcp manifest.json.
    extra_env: dict[str, str] = {}

    # Acquire execution layer subscription
    subscription_id = ""
    sub_user = created_by if scope == "user" else None

    try:
        subscription_id, sub_env = await asyncio.to_thread(
            subscription_pool.resolve_subscription_env,
            execution_path, sub_user,
            model=resolved_model, agent_info=agent_info,
        )
        extra_env.update(sub_env)
    except Exception as e:
        logger.warning(f"Subscription pool error for meeting agent {agent_name}: {e}")

    # Prepare the persistent config dir — .codex/ for Codex, .claude/ otherwise.
    # Single source of truth so the builders can't drift (a Codex meeting agent
    # whose config landed in .claude/ would crash at init). See
    # core.sandbox.sandbox.ensure_persistent_agent_dir.
    from core.sandbox.sandbox import ensure_persistent_agent_dir
    host_claude_dir = await asyncio.to_thread(
        ensure_persistent_agent_dir,
        agent_name,
        execution_path=execution_path,
        username=task_username,
        scope=scope,
    )

    return AgentConfig(
        agent_name=agent_name,
        # Meeting creator — routes MCP-install progress to their dashboard
        # (install_registry participant). "" for agent-scope meetings.
        user_sub=created_by or "",
        system_prompt=agent_prompt,
        mcp_config_path=str(mcp_config) if mcp_config else "",
        credential_env=credential_env or {},
        mcp_secret_bundles=secret_bundles or {},
        permission_mode="auto",
        client_type="meeting",
        model=resolved_model,
        effort=resolved_effort,
        resume=False,
        extra_env=extra_env,
        security_context=task_security,
        subscription_id=subscription_id,
        subscription_user_sub=sub_user or "",
        sandbox_host_claude_dir=str(host_claude_dir),
        execution_target=resolved_target,
        execution_path=execution_path,
    )


# ---------------------------------------------------------------------------
# Turn prompt builder
# ---------------------------------------------------------------------------

def build_turn_prompt(meeting: dict, agent_slug: str, transcript: list[dict],
                      prompt_type: str = "normal",
                      propose_from: str | None = None) -> str:
    """Build the prompt for an agent's turn.

    prompt_type:
        "start"    — moderator's opening turn
        "normal"   — regular turn with transcript since last spoke
        "wrapup"   — auto-queued moderator wrap-up
        "checkin"  — auto-queued moderator check-in (3+ turns without speaking)
        "conclude_proposal" — moderator deciding on propose_conclude
        "restate"  — agent's previous turn ran tools but relayed no findings
    """
    topic = meeting["topic"]
    participants = json.loads(meeting["participants"])
    meeting_id = meeting["id"]

    agent_has_spoken = any(e["agent"] == agent_slug for e in transcript)

    # Transcript since agent's last turn
    relevant = []
    if agent_has_spoken:
        for entry in reversed(transcript):
            if entry["agent"] == agent_slug:
                break
            relevant.insert(0, entry)
    else:
        relevant = list(transcript)

    # Format transcript
    transcript_lines = []
    for entry in relevant:
        ad = agent_store.get_agent(entry["agent"])
        name = (ad or {}).get("display_name", entry["agent"])
        if entry.get("role") == "user":
            name = f"{name} (human)"
        if entry.get("thinking"):
            transcript_lines.append(f"**{name}** (thinking): {entry['thinking'][:300]}")
        transcript_lines.append(f"**{name}**: {entry['content']}")
        tools = entry.get("tools", [])
        if tools:
            tool_names = ", ".join(t.get("name", "?") for t in tools)
            transcript_lines.append(f"  _(used tools: {tool_names})_")

    transcript_section = ""
    if transcript_lines:
        label = "**Discussion so far:**" if not agent_has_spoken else "**Since your last turn:**"
        transcript_section = label + "\n\n" + "\n\n".join(transcript_lines) + "\n\n"

    # Build header based on prompt type
    if prompt_type == "start" or not agent_has_spoken:
        roster_lines = []
        for slug in participants:
            data = agent_store.get_agent(slug)
            if data:
                name = data.get("display_name", slug)
                desc = data.get("description", "")
                role_tag = " — moderator" if slug == meeting["moderator"] else ""
                you_tag = " *(you)*" if slug == agent_slug else ""
                roster_lines.append(
                    f"- **{name}** (`{slug}`){you_tag}{role_tag}"
                    + (f" — {desc}" if desc else "")
                )
        header = (
            f"You are participating in a multi-agent meeting.\n\n"
            f"**Meeting ID**: `{meeting_id}`\n\n"
            f"**Topic**: {topic}\n\n"
            f"**Participants**:\n" + "\n".join(roster_lines) + "\n\n"
            f"**Tools available** (full ids — deferred-tool loading matches on the\n"
            f"exact name, so use these verbatim with ToolSearch `select:`):\n"
            f"- `mcp__meetings-mcp__direct_to(agents=[...])` — address specific agents (they respond next)\n"
            f"- `mcp__meetings-mcp__end_meeting(meeting_id)` — conclude the meeting (moderator only)\n"
            f"- `mcp__meetings-mcp__propose_conclude(meeting_id)` — propose ending (moderator decides)\n"
            f"- `mcp__meetings-mcp__leave_meeting(meeting_id)` — leave if nothing more to contribute\n\n"
        )
    else:
        header = ""

    # Footer based on prompt type
    if prompt_type == "conclude_proposal":
        footer = (
            f"\n**{propose_from or 'An agent'} proposes concluding the meeting.**\n"
            f"If you agree, call `end_meeting(\"{meeting_id}\")` and provide a summary.\n"
            f"If not, respond normally to continue the discussion."
        )
    elif prompt_type == "wrapup":
        footer = (
            f"\nThe discussion has concluded. Please provide a brief summary "
            f"and call `end_meeting(\"{meeting_id}\")` to close."
        )
    elif prompt_type == "checkin":
        footer = (
            f"\nYou haven't spoken for several turns. Review the discussion above "
            f"and contribute, redirect, or call `end_meeting(\"{meeting_id}\")` to conclude."
        )
    elif prompt_type == "restate":
        footer = (
            "\nYour last turn ran tools and routed onward, but your response text "
            "did not include your findings. The other participants can NOT see "
            "your tool results or your thinking — ONLY the plain response text "
            "you write is relayed to them (direct_to arguments are not shown "
            "either). Write your complete message now as plain response text — "
            "the concrete facts, findings, or answer — then call "
            "`direct_to(agents=[...])` again to deliver it."
        )
    elif prompt_type == "start":
        footer = "Please open the discussion. Use `direct_to(agents=[...])` to address specific agents."
    else:
        footer = "Please respond."

    return header + transcript_section + footer


# ---------------------------------------------------------------------------
# direct_to argument parsing
# ---------------------------------------------------------------------------

def _parse_directed_agents(raw) -> list[str] | None:
    """Tolerant parse of the direct_to `agents` argument. Models emit it as a
    JSON array, a JSON-encoded string, a bare agent name, or a comma list —
    a malformed value must never raise (it used to escape `json.loads` into
    the turn's catch-all, marking the agent failed: the dashboard's "Agent
    disconnected from meeting"). Returns None when nothing usable was given
    (caller keeps the prior routing)."""
    if isinstance(raw, list):
        names = [str(a).strip() for a in raw if str(a).strip()]
        return names or None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            names = [str(a).strip() for a in parsed if str(a).strip()]
            return names or None
        if isinstance(parsed, str) and parsed.strip():
            return [parsed.strip()]
        # Bare agent name or comma-separated names
        names = [p.strip() for p in s.split(",") if p.strip()]
        return names or None
    return None
