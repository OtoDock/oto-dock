"""DirectLLMExecutionLayer — wraps DirectSession for direct Anthropic API calls.

Translates run_direct_stream SSE dicts → CommonEvent. Does NOT modify
the DirectSession machinery (core/layers/direct/session.py) — wraps it.
"""

import asyncio
import functools
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import config as app_config
from core import placement
from core.engine_records import PROXY_MANAGED
from core.events.common_events import (
    CONTEXT_COMPACT,
    DONE,
    ERROR,
    METADATA,
    SYSTEM,
    TEXT,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    CommonEvent,
    is_note_mark,
    note_mark,
)
from core.execution_layer import (
    ASK_MODE,
    DONT_ASK_MODE,
    AgentConfig,
    AuthProfile,
    BehaviourProfile,
    EngineIdentity,
    ExecutionLayer,
    LayerCapabilities,
    ModelPolicy,
    ResumeVerdict,
    RuntimeProfile,
    SubscriptionHandle,
    UsageProfile,
)
from core.layers.direct.mcp import ENGINE_ID
from core.layers.direct.session import (
    DirectSession,
    close_direct_session,
    create_direct_session,
    get_direct_session,
    run_direct_stream,
)
from core.session import session_closing, session_kind
from core.session.session_state import (
    START_MARK_TTL_S,
    _record_session_use,
    cleanup_session_permission_state,
    clear_starting,
    forget_session,
    is_starting,
    mark_starting,
    register_session_state,
    session_is_held,
    set_session_mode,
)
from storage.pg import run_db

if TYPE_CHECKING:
    from auth.path_policy import SecurityContext

logger = logging.getLogger("otodock")


# ---------------------------------------------------------------------------
# Direct LLM SSE → CommonEvent translator
# ---------------------------------------------------------------------------

def direct_event_to_common(event: dict) -> CommonEvent | None:
    """Translate a single run_direct_stream SSE event dict into a CommonEvent.

    run_direct_stream yields dicts like: {"type": "text", "data": {...}}
    """
    etype = event.get("type", "")
    data = event.get("data", {})

    if etype == "session":
        return None  # internal plumbing, not user-visible content

    if etype == "text":
        content = data.get("content", "")
        if content:
            return CommonEvent(type=TEXT, data={"content": content})
        if is_note_mark(data):
            return note_mark()
        return None

    if etype == "thinking":
        # Same phase contract as the Codex translator: start / delta / end.
        return CommonEvent(type=THINKING, data=data)

    if etype == "tool_start":
        return CommonEvent(type=TOOL_USE, data={
            "name": data.get("name", ""),
            "tool_id": data.get("tool_use_id", ""),
        })

    if etype == "tool_end":
        return CommonEvent(type=TOOL_RESULT, data={
            "name": "",  # run_direct_stream doesn't include name in tool_end
            "tool_id": data.get("tool_use_id", ""),
            "result_preview": data.get("result_preview", ""),
        })

    if etype == "metadata":
        return CommonEvent(type=METADATA, data={
            **data,
            "cost_is_delta": True,  # Direct LLM emits per-turn cost, not cumulative
        })

    if etype == "context_compact":
        return CommonEvent(type=CONTEXT_COMPACT, data=data)

    if etype == "done":
        return CommonEvent(type=DONE, data={})

    if etype == "error":
        return CommonEvent(type=ERROR, data={
            "message": data.get("message", "Unknown error"),
        })

    # Unknown event type — pass through as system
    return CommonEvent(type=SYSTEM, data={"subtype": etype, **data})


# ---------------------------------------------------------------------------
# Direct LLM layer capabilities
# ---------------------------------------------------------------------------

_DIRECT_CAPABILITIES = LayerCapabilities(
    name=ENGINE_ID,
    display_name="Direct LLM API",
    supports_resume=False,
    supports_permissions=True,
    supports_plan_mode=False,
    supports_todos=False,
    supports_subagents=False,
    supports_context_compression=False,  # future: Anthropic compaction API
    supports_control_commands=False,
    supports_mcps=True,
    permission_modes=[ASK_MODE, DONT_ASK_MODE],
    control_commands=[],
    models=app_config.get_layer_models("direct-llm"),
    effort_levels=["low", "medium", "high", "xhigh", "max"],
    effort_changeable_mid_session=False,
    compression_threshold_pct=None,
    mcp_delivery=PROXY_MANAGED,
    mcp_config_format=None,
    providers=[
        # The ladders are what each provider's API takes (DIRECT-LLM.md
        # "Effort / Reasoning Mapping"): Anthropic's output_config.effort
        # keeps xhigh and max apart and rejects xhigh on older models (the
        # row flag, anthropic_adapter); the OpenAI family tops at xhigh, so
        # the adapter folds max and ultra onto it; Groq documents low /
        # medium / high (gpt-oss) and pins qwen to no thinking.
        # The hosted relay fronts the three vendors (relay_path is where the
        # install-side SDK points: the provider SDK appends its own route —
        # /v1/messages, /chat/completions — so the base differs per vendor).
        {"id": "anthropic", "label": "Anthropic", "requires_key": True,
         "kind": "vendor", "relay_path": "/v1/relay/anthropic", "api_path": "",
         "effort_scale": ["low", "medium", "high", "xhigh", "max"],
         "effort_per_model": ["xhigh"]},
        {"id": "openai", "label": "OpenAI", "requires_key": True,
         "kind": "vendor", "relay_path": "/v1/relay/openai/v1", "api_path": "",
         "effort_scale": ["low", "medium", "high", "xhigh"],
         "effort_per_model": []},
        {"id": "groq", "label": "Groq", "requires_key": True,
         "kind": "vendor", "relay_path": "/v1/relay/groq/v1", "api_path": "",
         "effort_scale": ["low", "medium", "high"],
         "effort_per_model": []},
        # Local providers reach the operator's own network — unavailable on
        # hosted OtoDock (no operator LAN). Gated off at import on cloud.
        *([] if app_config.OTODOCK_CLOUD else [
            {"id": "ollama", "label": "Ollama", "requires_key": False,
             "kind": "local", "relay_path": "", "api_path": "/v1",
             "effort_scale": ["low", "medium", "high", "xhigh"],
             "effort_per_model": []},
            {"id": "openai_compatible", "label": "OpenAI-compatible server", "requires_key": False,
             "kind": "local", "relay_path": "", "api_path": "",
             "effort_scale": ["low", "medium", "high", "xhigh"],
             "effort_per_model": []},
        ]),
    ],
    identity=EngineIdentity(
        short_name="direct",
        vendor_id="",                         # multi-provider: no single vendor
        vendor_label="",
        account_label="",
        role="supporting",                    # the low-latency phone path, not a coding engine
        sort_order=30,
    ),
    runtime=RuntimeProfile(
        has_os_process=False,                 # the tool loop runs in-process
        hard_abort_kills_process=False,
        supports_remote_execution=False,      # nothing to relocate to a satellite
        supports_interactive_pty=False,
        interactive_first_prompt_via_argv=False,
        supports_reattach_after_restart=False,
        binary="",
        pin_key="",
        config_dir_name="",                   # no CLI config dir of its own
        self_wakes=False,
        event_queue_depth=0,                  # never runs on a satellite
        interactive_submit_backstop=False,
    ),
    behaviour=BehaviourProfile(
        rebuilds_history_from_db=True,        # every turn rebuilds from chat_messages
        attach_images_inline=True,            # native vision content blocks
        skills_delivery="prompt_catalog",     # the prompt lists skills its Skill builtin loads
        supports_bash=False,
        supports_plans_dir=False,
        builtin_file_tools=True,              # the platform's own Read/Write/Edit/Delete
        has_shell_on_external_route=False,
        hides_denied_tools=True,              # the functions a limit leaves out are not offered (tool_limits)
        provider_pinned_per_session=False,    # change_model re-acquires across providers
        supports_steer=True,                  # read at the turn's next tool boundary
        supports_compact=False,
        supports_interrupt_for_queued=False,
        supports_background_commands=False,  # no shell
        supports_step_descriptions=False,  # no shell
        # Its Anthropic calls mark the cache ephemeral (five minutes), and
        # OpenAI keeps an idle prefix five to ten minutes.
        prompt_cache_s=300,
        # The platform's own builtins (builtins.py) — canonical names already;
        # the vendors' server tools (web search) are the provider's, not this
        # engine's, and arrive under the provider's name.
        tools={
            "read": ("Read",),
            "glob": ("Glob",),
            "write": ("Write", "Edit"),
            "delete": ("Delete",),
            "skill": ("Skill",),
            "discovery": ("tool_search",),
        },
    ),
    model_policy=ModelPolicy(
        # tier 3: the operator's call (calls included), kept below GPT-6.1
        # Sol (tier 2, the same price) until decided again
        default_model="claude-sonnet-5-5",
        model_filter_policy="all",            # every provider filtered to active subscriptions
        pricing_editable=True,                # BYO keys are metered per model
    ),
    auth=AuthProfile(
        auth_types=("api_key", "local_endpoint", "relay"),  # vendor keys, local servers, the hosted relay; no login
    ),
    usage=UsageProfile(windows=()),           # keys are metered per token, never capped by a window
)


def declared_effort_scale(provider: str) -> list[str]:
    """The effort levels this engine's descriptor says ``provider``'s API
    takes (its ``providers[]`` entry), or ``[]`` for a provider it does not
    declare. The provider adapters fold a level onto it."""
    for entry in _DIRECT_CAPABILITIES.providers or []:
        if entry.get("id") == provider:
            return list(entry.get("effort_scale") or [])
    return []


# ---------------------------------------------------------------------------
# DirectLLMExecutionLayer
# ---------------------------------------------------------------------------

def _plain_text(message: dict) -> str:
    """The visible text of a stored message, whatever provider shape it has:
    a string content; the ``text`` blocks of a content list (Anthropic blocks
    and the chat-shape blocks share that shape — images, tool_use /
    tool_result / thinking / server-tool blocks are dropped); an OpenAI
    assistant dict's ``content`` string. "" when nothing visible remains."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        ]
        return "\n\n".join(p for p in parts if p)
    return ""


def _text_only_history(messages: list[dict]) -> list[dict]:
    """The history as plain text turns — what a chat keeps when it switches to
    another provider (the same shape the DB resume builds). Tool calls, tool
    results, reasoning items and images are provider-shaped and dropped; the
    user's and the assistant's visible text stays. Consecutive same-role
    turns (an assistant's text before and after a tool loop) merge, and
    leading assistant turns go (every provider wants the user to speak
    first)."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _plain_text(m)
        if not text:
            continue
        if not out and role != "user":
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    return out


def _rebuild_history_from_db(session, session_id: str, chat_id: str = "", *,
                             skip_row: int = 0) -> None:
    """Reconstruct conversation history from DB chat_messages.

    Direct LLM sessions are in-memory only — lost on proxy restart or idle
    reap. When resuming, we rebuild the messages list from persisted
    user/assistant messages so the LLM sees the full conversation context.

    If *chat_id* is provided, look up the chat directly (works after restart
    when the session_id in the DB was just updated to a new value).

    ``skip_row``: a task session resumed for its next round. Its
    conversation is the rounds of the runs on it, each in its own chat, in
    the order they started; the round's own prompt row (this turn's message)
    is left out by its id.
    """
    from storage import database as task_store
    chats: list[dict] = []
    if skip_row:
        chats = [c for c in (task_store.get_chat(cid)
                             for cid in task_store.round_chats_of_session(session_id)) if c]
    if not chats:
        # Prefer chat_id lookup (works after restart when session_id changed)
        chat = (task_store.get_chat(chat_id) if chat_id
                else task_store.get_chat_by_session(session_id))
        if not chat:
            logger.info("Direct session resume: no chat found for session %s", session_id)
            return
        chats = [chat]

    from core.session import key_rows
    from core.session.start_context import fence_turn
    messages_db: list[dict] = []
    rebuilt: list[dict] = []
    for chat in chats:
        # A key's message being delivered is this turn's prompt already.
        rows = [m for m in task_store.get_chat_messages(chat["id"])
                if not key_rows.held_row(chat["id"], m)]
        messages_db.extend(rows)
        # A row holds what was typed: fenced as the turn's own text was, so
        # only the platform's block carries the start's tag.
        rebuilt.extend(_aborted_trimmed(chat, [
            {"role": m["role"], "content": fence_turn(m["content"])} for m in rows
            if m.get("role", "") in ("user", "assistant") and m.get("content", "")
            and not (skip_row and m.get("id") == skip_row)]))

    # Deferred tools the model already discovered in this chat: re-load them
    # so it can keep calling them without another tool_search. A tool call
    # persists as its OWN row (role "event", event_type "tool", the block in
    # event_data — stream_pump._serialize_turn_rows), never inside the
    # assistant text row.
    catalog = getattr(session, "catalog", None)
    if catalog is not None:
        used: list[str] = []
        for m in messages_db:
            if m.get("role") != "event" or m.get("event_type") != "tool":
                continue
            try:
                block = json.loads(m.get("event_data") or "{}")
            except ValueError:
                continue
            name = block.get("name") if isinstance(block, dict) else None
            if isinstance(name, str) and name.startswith("mcp__") and name not in used:
                used.append(name)
        if used:
            new_defs = catalog.load(used)
            session.tools.extend(new_defs)
            resident = {t.get("name") for t in session.tools}
            gone = [n for n in used if n not in catalog.deferred and n not in resident]
            logger.info(
                "Direct session resume: re-loaded %s deferred tool(s) from chat %s%s",
                len(new_defs), chats[-1]['id'][:8],
                f"; {len(gone)} no longer available: {gone}" if gone else "",
            )

    if rebuilt:
        # Estimate tokens: ~4 chars per token. If history is too large
        # relative to context window, keep only recent messages.
        context_window = app_config.get_model_context_window(session.model)
        estimated_tokens = sum(len(m.get("content", "")) for m in rebuilt) // 4
        # Reserve 40% for system prompt + tools + response
        max_history_tokens = int(context_window * 0.6)
        KEEP_MESSAGES = 6
        if estimated_tokens > max_history_tokens and len(rebuilt) > KEEP_MESSAGES:
            dropped = len(rebuilt) - KEEP_MESSAGES
            rebuilt = rebuilt[-KEEP_MESSAGES:]
            logger.info(
                "Direct session resume: truncated %s old messages "
                "(~%s tokens > %s limit)",
                dropped, estimated_tokens, max_history_tokens
            )
        session.messages = rebuilt
        logger.info(
            "Direct session resume: rebuilt %s messages "
            "from %s chat(s) for session %s",
            len(rebuilt), len(chats), session_id[:8]
        )


def _aborted_trimmed(chat: dict, rebuilt: list[dict]) -> list[dict]:
    """A chat whose last turn was aborted: its trailing aborted messages go
    (the aborted user message and any partial answer the pump saved). The
    dashboard re-injects the cancelled turn's context into the next user
    message; kept here, the LLM would see it twice."""
    if not (chat.get("last_turn_aborted") and rebuilt):
        return rebuilt
    for i in range(len(rebuilt) - 1, -1, -1):
        if rebuilt[i]["role"] == "user":
            logger.info(
                "Direct session resume: dropped %s message(s) from "
                "the aborted turn of chat %s", len(rebuilt) - i, chat['id'][:8]
            )
            return rebuilt[:i]
    return rebuilt


def excluded_server_keys(names) -> set[str]:
    """The ``mcpServers`` keys of the MCPs a session does not start, from
    their manifest names. A name with no manifest is its own key."""
    from services.mcp import mcp_registry as mcp_reg
    keys: set[str] = set()
    for name in names:
        manifest = mcp_reg.get_manifest(name)
        keys.add((manifest.server_name or manifest.name) if manifest else name)
    return keys


class DirectLLMExecutionLayer(ExecutionLayer):
    """Execution layer wrapping the Direct Anthropic API path.

    Delegates to the existing DirectSession functions
    (core/layers/direct/session.py) — does not modify them.
    """

    # Track active streaming tasks for abort support
    _active_streams: dict[str, asyncio.Task] = {}

    async def start_session(
        self, session_id: str, config: AgentConfig,
    ) -> None:
        """Create a direct LLM session with MCP servers.

        The permission mode + security context are registered BEFORE the MCP
        processes spawn: the session JWT minted into their env derives its
        external claim from the live context. A failed start drops the
        registration again (see the CLI layer).
        """
        await session_closing.wait_closed(session_id)
        held = session_is_held(session_id)
        mark_starting(session_id, START_MARK_TTL_S)
        try:
            register_session_state(session_id, config.permission_mode, config.security_context,
                                   token_minted_at=config.token_minted_at)
            try:
                await self._start_session_impl(session_id, config)
            except BaseException:
                if not held:
                    cleanup_session_permission_state(session_id)
                raise
        finally:
            clear_starting(session_id)

    async def _start_session_impl(
        self, session_id: str, config: AgentConfig,
    ) -> None:
        # Fail CLOSED: a local agent's stdio MCPs MUST run sandboxed +
        # network-isolated. The config builders always set this; empty means an
        # omission — refuse rather than run MCPs un-sandboxed. (The LLM call
        # itself is proxy-side; this guards the MCP subprocesses.)
        if not config.sandbox_host_claude_dir:
            raise RuntimeError(
                f"refusing to start a local Direct-LLM session for agent "
                f"'{config.agent_name}' without a sandbox dir — local agents "
                f"must run sandboxed + network-isolated."
            )
        phone_mode = config.client_type == session_kind.PHONE.name
        # An EXTERNAL session (a phone caller who is not a platform user):
        # its sandbox is a caller's. The tools it gets are the builder's.
        from core.session.external_identity import external_home_of, is_external_ctx
        from services.mcp import tool_limits
        external = is_external_ctx(config.security_context)
        extra = config.extra_env or {}
        # Provider-aware: subscription_pool sets _API_KEY/_PROVIDER/_ENDPOINT_URL
        # on direct-llm sessions (see services/engines/subscription_pool.py — the
        # "else" branch keyed on execution_path).
        api_key = extra.get("_API_KEY")
        provider = extra.get("_PROVIDER", "anthropic")
        endpoint_url = extra.get("_ENDPOINT_URL")

        # Build bwrap sandbox for MCP subprocesses (mirrors CLI layer pattern).
        # Each stdio MCP gets wrapped individually in bwrap so it sees the
        # same restricted mount namespace as CLI MCPs.
        sandbox_builder = None
        # The compute machine this session's stdio tools run on, the builder's
        # one decision of where, carried in the session's own record (a
        # relayed tool's callbacks come through that machine's link).
        _on = placement.compute_of(getattr(config.security_context, "placement", None))
        machine_id = str(_on.machine_id) if _on is not None else ""
        if config.sandbox_host_claude_dir:
            from core.sandbox.sandbox import (
                SandboxBuilder,
                mcp_sandbox_mounts,
                own_engine_home,
                resolve_sandbox_config,
            )
            from services.mcp import mcp_registry as mcp_reg

            ctx = cast("SecurityContext | None", config.security_context)
            # The MCPs the session's build attached (the assigned set minus
            # ``AgentConfig.excluded_mcps``): their manifest mounts and
            # routes. LOCAL bwrap mount path: satellite_only device MCPs
            # never mount here.
            attached = [
                m for m in (await run_db(mcp_reg.get_agent_mcps, config.agent_name,
                                         placement=placement.LOCAL_PLACEMENT) or [])
                if m.name not in (config.excluded_mcps or ())
            ]
            mcp_mounts = mcp_sandbox_mounts(attached)

            sandbox_cfg = await run_db(
                resolve_sandbox_config,
                role=ctx.role if ctx else "viewer",
                username=ctx.mount_username if ctx else "",
                agent_name=config.agent_name,
                is_admin_agent=ctx.is_admin_agent if ctx else False,
                host_claude_dir=Path(config.sandbox_host_claude_dir),
                user_sub=config.user_sub,
                mcp_sandbox_mounts=mcp_mounts,
                account_choices=config.account_choices or None,
                config_visible=ctx.config_visible if ctx else None,
                mount_shared=ctx.mount_shared if ctx else True,
                knowledge_rw=bool(getattr(ctx, "knowledge_rw", False)) if ctx else False,
                # The tool folders follow the builder's config, as for the
                # CLI engines: the MCPs it forces (the meeting tools) have theirs.
                mcp_config_path=config.mcp_config_path,
                external=external,
                external_home=external_home_of(ctx),
                read_only=bool(getattr(ctx, "read_only", False)),
                attached_mcps=[m.name for m in attached],
                outside_files_write=bool(getattr(ctx, "outside_files_write", False)),
                outside_knowledge=tuple(getattr(ctx, "outside_knowledge", ()) or ()),
                wrapup_home=getattr(ctx, "wrapup_home", "") if getattr(ctx, "wrapup", "") else None,
                engine_home=own_engine_home(ctx, config.sandbox_host_claude_dir),
            )
            sandbox_builder = SandboxBuilder(sandbox_cfg)
            # The credential exit: the tool children this layer spawns take
            # its variables (``credential_exit.env_of``) and its port and
            # bundle through the sandbox they are wrapped in on the compute
            # machine (the relayed tool's open carries the session's door).
            from core.credentials import credential_exit
            await credential_exit.attach_sandbox(
                session_id, sandbox_builder, {}, config.sandbox_host_claude_dir,
                sandbox_config_dir=sandbox_builder.get_env_overrides().get(
                    "CLAUDE_CONFIG_DIR", "/workspace/.claude"),
                machine=_on)

        # The builder's tool config, the person's accounts and the chat's
        # choice of account in it, as the CLI engines read it.
        built_config = (Path(config.mcp_config_path) if config.mcp_config_path else None,
                        config.mcp_secret_bundles)
        from services.phone.call_control import (
            ASK_BACK_ENV,
            CALL_CONTROL_MCP,
            CALL_TOOLS_ENV,
        )
        # This engine has the call tools as builtins (``end_call`` and
        # ``transfer_call``, which set ``line_closing``): the builders force
        # the call-control MCP for the CLI engines alone to read.
        excluded = excluded_server_keys({*(config.excluded_mcps or ()), CALL_CONTROL_MCP})
        session = await create_direct_session(
            session_id=session_id,
            agent_name=config.agent_name,
            phone_mode=phone_mode,
            api_key=api_key,
            provider=provider,
            endpoint_url=endpoint_url,
            credential_env=config.credential_env or None,
            system_prompt=config.system_prompt,
            sandbox_builder=sandbox_builder,
            call_tools=bool(extra.get(CALL_TOOLS_ENV)),
            call_ask_back=bool(extra.get(ASK_BACK_ENV)),
            excluded_mcps=excluded or None,
            model=config.model,
            prebuilt_config=built_config,
            machine_id=machine_id,
            web_tools=self._web_tools(config.security_context, external=external),
            tool_grants=tool_limits.engine_pairs(config.security_context),
        )
        # The builtin file tools resolve against this session's mount table
        # (the same decisions the MCP subprocesses get from bwrap).
        session.sandbox_cfg = sandbox_builder.cfg if sandbox_builder else None
        # The scope signal for credential acquisition (here + on provider switch
        # in change_model): a real user_sub ⇒ user-scope (own subs, then borrowable
        # admin APIs only); empty ⇒ agent-scope (full platform pool). Set ONCE here
        # from the resolved creds identity and never blanked for a user chat — else
        # change_model would widen scope. The resolver also normalises "" defensively.
        session.user_sub = extra.get("_USER_SUB", "")
        # Override effort if specified
        if config.effort:
            session.effort = config.effort
        # (permission mode + security context were registered before the start)
        # Register session metadata (agent name, client type) — needed by
        # location bridge and other hooks that look up sessions by agent name
        _record_session_use(session_id, client_type=config.client_type, agent=config.agent_name)
        # Reconstruct conversation history from DB when resuming a direct session
        # (direct sessions are in-memory — lost on proxy restart, idle reap, etc.)
        # Also rebuild when chat_id is provided (restart with new session_id).
        if config.resume or config.chat_id:
            await asyncio.to_thread(functools.partial(
                _rebuild_history_from_db, session, session_id, chat_id=config.chat_id,
                skip_row=int(extra.get("_RESUME_SKIP_ROW") or 0)))
        # Bind subscription for pool cleanup
        if config.subscription_id:
            from services.engines.subscription_pool import bind_session
            await asyncio.wrap_future(bind_session(
                session_id, config.subscription_id,
                layer="direct-llm", user_sub=config.subscription_user_sub,
            ))


    async def _send_turn(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        """One turn: send the message and yield CommonEvents translated from
        SSE dicts (``send_message`` on the base wraps the turn-end loop).

        Kwargs:
            barge_in_chars: int — for phone barge-in annotation
            inject_time: bool — prepend current datetime to message (per-turn).
                The session-start system prompt also has a datetime, but it
                goes stale on long-lived sessions. Per-turn injection mirrors
                CLI/Codex behaviour.
            images: list[dict] | None — chat-attached photos. Each entry is
                ``{"base64": str, "media_type": str}``. When non-empty, the
                user message body is built as a content-block list with the
                text block first, then one image block per entry (formatted
                via the provider adapter's ``format_image_content_block``).
                Direct LLM has no built-in Read tool — images are attached
                directly to the API request (Anthropic + OpenAI vision).
        """
        session = await get_direct_session(session_id)
        if not session:
            yield CommonEvent(type=ERROR, data={"message": "Session not found"})
            return

        barge_in_chars = kwargs.get("barge_in_chars")
        inject_time = kwargs.get("inject_time", False)
        images = kwargs.get("images")

        # Register the consumer task so abort() can cancel it. Unlike CLI/Codex,
        # Direct LLM has no subprocess to kill — cancellation must propagate
        # through the async task to close HTTP streams and stop tool execution.
        task = asyncio.current_task()
        if task is not None:
            self._active_streams[session_id] = task

        # Caller must hold session_lock() — do NOT acquire here (deadlock).
        session.touch()
        try:
            async for sse_event in run_direct_stream(
                session, message, barge_in_chars=barge_in_chars,
                inject_time=inject_time, images=images,
            ):
                event = direct_event_to_common(sse_event)
                if event is not None:
                    yield event
        finally:
            # Clear only if still ours (a new send_message may have registered)
            if self._active_streams.get(session_id) is task:
                self._active_streams.pop(session_id, None)

    async def abort(self, session_id: str) -> bool:
        """Cancel the active streaming task.

        Direct LLM has no subprocess — cancellation propagates through the
        async task: the producer's CancelledError unwinds through the SDK's
        stream iterator, closing the HTTP connection and any in-flight MCP
        tool execution. Safe to call even if no stream is active. Never
        graceful: the direct session rebuilds history from the DB and relies
        on the cancelled-context injection (see the resume drop above).
        """
        task = self._active_streams.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        return False

    async def steer(self, session_id: str, text: str) -> bool:
        """A message into the running turn: the tool loop reads it at its
        next boundary (``session.steers``). Refused with no running turn,
        while a permission card waits (the card is the question: a typed
        message keeps its place in the queue), and once the turn can no longer
        read one, so the caller queues it instead."""
        session = await get_direct_session(session_id)
        if session is None or not text or not session.steer_open or session_id not in self._active_streams:
            return False
        from core.session.session_state import has_pending_prompt
        if has_pending_prompt(session_id):
            return False
        from core.session.start_context import fence_turn
        session.steers.append(fence_turn(text))
        return True

    async def close_session(self, session_id: str) -> None:
        """Close the direct session and its MCP servers."""
        from .session import _direct_sessions
        if session_id not in _direct_sessions and is_starting(session_id):
            # What is kept under the id is the start's in flight.
            return
        with session_closing.closing(session_id):
            await self._drop_session(session_id)
            from core.concurrency import release_chat_slot
            release_chat_slot(session_id)

    async def _drop_session(self, session_id: str) -> None:
        """The session, its MCP servers, its permission state and its seat."""
        await close_direct_session(session_id)
        # A closed session must not keep a live permission context: the hook
        # fails closed without one, and a session token that outlives its
        # session is dead only once this is gone.
        await forget_session(session_id)
        from services.engines.subscription_pool import release_subscription
        release_subscription(session_id)

    async def respond_permission(
        self, session_id: str, request_id: str, approved: bool,
    ) -> None:
        """Resolve a pending permission request from the tool execution loop."""
        from core.session.session_state import resolve_permission
        resolve_permission(request_id, approved)

    async def change_model(
        self, session_id: str, model: str,
    ) -> None:
        """Update the session's model, switching provider if needed."""
        session = await get_direct_session(session_id)
        if not session:
            return

        new_provider = app_config.get_model_provider(model)
        if new_provider != session.provider:
            # Provider changed — acquire new subscription, release old
            import asyncio as _asyncio

            from core.layers.providers.registry import get_adapter
            from services.engines import subscription_pool

            old_provider = session.provider
            subscription_pool.release_subscription(session_id)
            sub_handle = await _asyncio.to_thread(
                subscription_pool.acquire_subscription,
                "direct-llm", session.user_sub or None,
                provider=new_provider,
            )
            if sub_handle:
                session.provider = sub_handle.provider
                if sub_handle.auth_type == "relay":
                    # Hosted relay: the token is provider-agnostic but the endpoint
                    # is per-provider — mint + re-point for the new provider.
                    creds = subscription_pool.relay_llm_credentials(
                        sub_handle.layer, sub_handle.provider, session.user_sub,
                    )
                    session.api_key = creds[0] if creds else None
                    session.endpoint_url = creds[1] if creds else None
                else:
                    session.api_key = sub_handle.api_key
                    session.endpoint_url = sub_handle.endpoint_url
                await asyncio.wrap_future(subscription_pool.bind_session(
                    session_id, sub_handle.subscription_id,
                    layer="direct-llm", user_sub=session.user_sub or "",
                ))
                # The history is provider-shaped (OpenAI tool_calls / reasoning
                # items / tool messages, Anthropic content blocks / tool_result
                # messages, provider image blocks) and the new provider would
                # 400 on it. Keep the conversation as plain text turns — what
                # the DB resume builds — and swap the provider's own server
                # tools (the client tools all carry an input_schema).
                before = len(session.messages)
                session.messages = _text_only_history(session.messages)
                session.tools = [t for t in session.tools if "input_schema" in t]
                session.tools.extend(get_adapter(session.provider).get_builtin_tools())
                logger.info(
                    "Direct session %s switched provider: "
                    "%s → %s for model %s; "
                    "history kept as text (%s → %s messages)",
                    session_id[:8], old_provider, session.provider, model, before,
                    len(session.messages)
                )
            else:
                logger.warning(
                    "No subscription for provider %s — "
                    "model change to %s may fail", new_provider, model
                )

        session.model = model

    async def change_mode(
        self, session_id: str, mode: str,
    ) -> None:
        """No-op — direct LLM doesn't support permission modes yet."""
        pass

    async def send_control_request(
        self, session_id: str, subtype: str, **kwargs,
    ) -> dict:
        """Handle permission mode changes (no CLI control channel)."""
        if subtype == "set_permission_mode":
            set_session_mode(session_id, kwargs.get("mode") or ASK_MODE)
            return {"status": "ok"}
        return {}

    # --- Capabilities ---

    @staticmethod
    def _web_tools(ctx, *, external: bool) -> bool:
        """The provider's own web tools: never for a session with no web (the
        wrap-up), for an outside person only when their managers picked it."""
        return (not getattr(ctx, "no_web", False)
                and (not external or bool(getattr(ctx, "outside_web", False))))

    @property
    def capabilities(self) -> LayerCapabilities:
        return _DIRECT_CAPABILITIES

    # --- Session access ---

    def owns_session(self, session_id: str) -> bool:
        from .session import _direct_sessions
        return session_id in _direct_sessions

    def local_session_ids(self) -> list[str]:
        from .session import _direct_sessions
        return list(_direct_sessions)

    def prepare_config_dir(
        self, agent_name: str, *, username: str = "", scope: str = "user",
        external_home=None, no_shell: bool = False, read_only: bool = False,
        outside_picks=None, kind_rule=None,
    ) -> Path:
        """Direct LLM has no CLI config of its own (``config_dir_name`` is
        ""), but a session's plans dir and its in-process MCP config home
        live in the ``.claude`` tree the sandbox mounts
        (``sandbox_host_claude_dir``), so it asks the Claude engine's
        builder for that tree by name — the coupling the old
        ``execution_path`` branch hid behind its default arm, declared here.
        ``no_shell`` / ``read_only`` are this engine's builtin gate's
        business (``builtins.gate``), not a settings file's: the deny list
        the builder writes is read by no CLI here. Reached through the
        builder's module at call time (tests patch it there)."""
        from core.layers.cli import config_dir as _cd
        extra: dict = {}
        if external_home:
            extra["external_home"] = external_home
            extra["outside_picks"] = outside_picks
            extra["kind_rule"] = kind_rule
        return _cd.ensure_persistent_claude_dir(
            agent_name, username=username, scope=scope, **extra)

    async def on_subscriptions_changed(self) -> None:
        """The phone's Groq turn classifier reuses this engine's Groq key
        (``services.phone.phone_config.direct_llm_groq_credentials``), so a
        change to its subscriptions is pushed to the phone servers at once
        — no phone restart to pick up a connected or removed key."""
        from services.phone.phone_config import notify_phone_config_changed
        await notify_phone_config_changed()

    def subscription_env(self, handle: SubscriptionHandle) -> dict[str, str]:
        """Provider-generic env: the key (a BYO vendor key, or the relay
        token the pool minted) and the endpoint (a local server, or the
        relay's per-provider base URL). Nothing on disk — a Direct LLM
        session holds its credential in memory, so there is no file to
        rotate under it and no sticky scope."""
        env = {"_PROVIDER": handle.provider}
        if handle.api_key:
            env["_API_KEY"] = handle.api_key
        if handle.endpoint_url:
            env["_ENDPOINT_URL"] = handle.endpoint_url
        return env

    async def get_session(self, session_id: str) -> DirectSession | None:
        """Return the underlying DirectSession."""
        return await get_direct_session(session_id)

    async def is_session_alive(self, session_id: str) -> bool:
        """A session that exists, until its relayed tools are lost."""
        session = await get_direct_session(session_id)
        return session is not None and not self._tools_gone(session_id, session)

    def _tools_gone(self, session_id: str, session: DirectSession) -> bool:
        """Its relayed tools went with their machine, or can no longer reach
        the platform. A running turn finishes first (it ends with the tools'
        errors, as a CLI turn whose process died)."""
        manager = session.mcp_manager
        return manager is not None and session_id not in self._active_streams and manager.tools_lost()

    # --- Session lock + lifecycle ---

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        """Wrap DirectSession.lock for multi-turn producers."""
        session = await get_direct_session(session_id)
        if session:
            async with session.lock:
                yield
        else:
            yield

    async def is_session_process_dead(self, session_id: str) -> bool:
        """A Direct session has no process of its own; its tools' processes
        on a compute machine are the part that can die."""
        session = await get_direct_session(session_id)
        return session is not None and self._tools_gone(session_id, session)

    async def prepare_resume(self, session_id: str) -> None:
        """A session whose tools were lost goes here, its pool and seat with
        it, so the next start builds a new one where the platform serves now.
        Its slot stays the caller's, as on the other engines: a call healed in
        place keeps the one it holds."""
        session = await get_direct_session(session_id)
        if session is not None and self._tools_gone(session_id, session):
            await self._drop_session(session_id)

    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
        external_home: str = "",
    ) -> ResumeVerdict:
        """Direct sessions can't resume (in-memory history).

        Recovery is handled via chat_id in AgentConfig instead — see
        _rebuild_history_from_db.
        """
        return ResumeVerdict.FRESH
