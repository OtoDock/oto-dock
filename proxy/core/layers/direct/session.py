"""Direct LLM API runner with MCP tool support — provider-agnostic.

Manages sessions with full message history (including tool_use/tool_result
blocks) and streams responses as SSE events. Provider-specific behavior
(Anthropic, OpenAI, Groq, Ollama, etc.) is handled by ProviderAdapter
implementations in core/layers/providers/.

Architecture:
- DirectSession: holds conversation state + MCP manager + provider info
- run_direct_stream(): async generator yielding SSE events (provider-agnostic)
- Session pool with per-session locks to prevent concurrent API calls
"""

import asyncio
import functools
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import config
from core.events.common_events import NOTE_MARK
from core.layers.direct import (
    builtins as direct_builtins,
    files as direct_files,
    tool_catalog,
)
from core.layers.direct.mcp import TOOL_CALL_TIMEOUT_CHAT, AgentMCPManager, mcp_pool
from core.layers.providers import (
    ProviderAdapter,
    ProviderError,
    ProviderUsage,
    get_adapter,
)
from core.session.session_state import (
    PROMPT_WAIT_S,
    get_permission_queue,
    get_session_mode,
    get_session_user_tz,
    has_pending_prompt,
    mark_closing,
    wait_for_permission,
)
from storage.pg import run_db, run_db_fast

if TYPE_CHECKING:
    from core.sandbox.sandbox import SandboxConfig

logger = logging.getLogger("direct-runner")

# Safety limit: max tool-use loop iterations per request
MAX_TOOL_LOOPS = 20


def _agent_model_or_empty(agent_name: str) -> str:
    """The agent's model, ``""`` when none is resolvable yet (the first turn
    then fails on it). Store reads."""
    try:
        return config.get_agent_model(agent_name)
    except RuntimeError:
        return ""


def _granted(name: str, limits: dict[str, set]) -> bool:
    """Whether a namespaced tool (``mcp__<server>__<tool>``) is one an outside
    session was picked: a server with no limit keeps all of its tools."""
    for server, names in limits.items():
        prefix = f"mcp__{server}__"
        if name.startswith(prefix):
            return name[len(prefix):] in names
    return True


class DirectSession:
    """Holds state for a direct API session."""

    def __init__(
        self,
        session_id: str,
        agent_name: str,
        system_prompt: str,
        mcp_manager: AgentMCPManager | None = None,
        provider: str = "anthropic",
        endpoint_url: str | None = None,
        model: str | None = None,
        web_tools: bool = True,
        tool_grants: tuple = (),
    ):
        self.session_id = session_id
        self.agent_name = agent_name
        # ``create_direct_session`` always hands the model in: this runs on the
        # event loop, so the read is only for a construction that gives none.
        self.model = _agent_model_or_empty(agent_name) if model is None else model
        self.system_prompt = system_prompt
        self.mcp_manager = mcp_manager
        self.provider = provider
        self.endpoint_url = endpoint_url
        self.messages: list[dict] = []
        self.tools: list[dict] = []  # universal format: {name, description, input_schema}
        self.last_activity: float = time.monotonic()
        self.lock = asyncio.Lock()
        self.api_key: str | None = None  # explicit key from subscription pool
        self.user_sub: str = ""  # for subscription acquisition on provider switch
        self.effort: str = ""  # reasoning effort level (low/medium/high/max)
        # The session's sandbox description (set by the layer at start): the
        # client-side file tools resolve their paths against its mount table
        # — the same RO/RW decisions bwrap renders for the MCP subprocesses.
        self.sandbox_cfg: SandboxConfig | None = None
        self._mount_table: list | None = None
        # Deferred-tools catalog (core/layers/direct/tool_catalog.py) — set by
        # _apply_deferred_tools once the MCP tool list is known; None means
        # every tool is resident (deferral off / below the threshold).
        self.catalog: tool_catalog.ToolCatalog | None = None
        # Set by an accepted end_call / transfer_call builtin: the tool loop
        # ends the turn after the tool result instead of calling the model.
        self.line_closing: bool = False
        # Messages typed during a running turn (``layer.steer``), read at its
        # next tool boundary. The door is open only while the turn can still
        # read one, so an accepted text is delivered exactly once.
        self.steers: list[str] = []
        self.steer_open: bool = False

        # Populate tools from MCP manager; an outside session is offered, of
        # an attached server, only the functions its pick names.
        if mcp_manager:
            limits = {key: set(names) for key, names in (tool_grants or ()) if names is not None}
            self.tools = [t for t in mcp_manager.get_tools() if _granted(t.get("name") or "", limits)]

        # Client-side builtins (Read / Glob / Write / Edit / Delete / Skill:
        # core/layers/direct/builtins.py). Each is gated inline and its handler
        # runs in-process. A file tool's handler hands the file work to the file
        # program in the session's sandbox.
        self.tools.extend(direct_builtins.client_tool_defs())

        # Add provider-specific built-in tools (e.g., Anthropic web_search/web_fetch).
        # An outside session gets them only when its managers picked the web.
        adapter = get_adapter(provider)
        builtin = adapter.get_builtin_tools() if web_tools else []
        if builtin:
            self.tools.extend(builtin)
            logger.info(
                "Added %s built-in tools for %s: "
                "%s",
                len(builtin), provider, [t.get('name', t.get('type', '?')) for t in builtin]
            )

    def mount_table(self) -> list:
        """The session's ``Mount`` decisions (computed once; empty without a
        sandbox config — the file tools then refuse)."""
        if self._mount_table is None:
            if self.sandbox_cfg is None:
                return []
            from core.layers.direct.files import mount_table
            self._mount_table = mount_table(self.sandbox_cfg)
        return self._mount_table

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def annotate_barge_in(self, spoken_chars: int) -> None:
        """Annotate the last assistant message for barge-in.

        Modifies the stored text content to show what the user heard vs.
        what was cut off. Works on both simple text messages and messages
        with content blocks (tool_use + text).
        """
        if spoken_chars <= 0 or not self.messages:
            return

        last = self.messages[-1]
        if last.get("role") != "assistant":
            return

        content = last.get("content")
        if not content:
            return

        # Simple text message
        if isinstance(content, str):
            if spoken_chars >= len(content):
                return
            spoken = content[:spoken_chars]
            unheard = content[spoken_chars:]
            if not unheard.strip():
                return
            last["content"] = (
                f"{spoken} [INTERRUPTED — the listener did NOT hear the rest: "
                f"{unheard.strip()[:200]}]"
            )
            logger.info(
                "Annotated barge-in: %s chars spoken, "
                "%s chars unheard", spoken_chars, len(unheard)
            )
            return

        # Content blocks (list of dicts with type: text/tool_use/tool_result)
        if isinstance(content, list):
            total_text_chars = 0
            for block in content:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    block_start = total_text_chars
                    block_end = total_text_chars + len(text)

                    if spoken_chars <= block_end:
                        offset = spoken_chars - block_start
                        if offset <= 0:
                            return
                        spoken = text[:offset]
                        unheard = text[offset:]
                        if not unheard.strip():
                            return
                        block["text"] = (
                            f"{spoken} [INTERRUPTED — the listener did NOT hear the rest: "
                            f"{unheard.strip()[:200]}]"
                        )
                        logger.info(
                            "Annotated barge-in (content block): %s chars spoken",
                            spoken_chars
                        )
                        return

                    total_text_chars = block_end


# Session pool
_direct_sessions: dict[str, DirectSession] = {}
_direct_sessions_lock = asyncio.Lock()


async def create_direct_session(
    session_id: str,
    agent_name: str,
    phone_mode: bool = False,
    api_key: str | None = None,
    provider: str = "anthropic",
    endpoint_url: str | None = None,
    credential_env: dict[str, str] | None = None,
    system_prompt: str = "",
    sandbox_builder=None,
    tool_timeout: int = TOOL_CALL_TIMEOUT_CHAT,
    call_tools: bool = False,
    call_ask_back: bool = False,
    excluded_mcps: set[str] | None = None,
    model: str = "",
    web_tools: bool = True,
    tool_grants: tuple = (),
    prebuilt_config: tuple | None = None,
    machine_id: str = "",
) -> DirectSession:
    """Create a new direct session with MCP servers started.

    Args:
        model: The session's model (``AgentConfig.model``). Empty: the agent's
            own, read in a worker thread.
        system_prompt: Pre-built prompt from config_builder (includes user context,
            permissions, MCP skills, dynamic context, etc.). If empty, falls back
            to building a basic prompt from the agent's agent.md persona.
        tool_timeout: the per-call cap of the session's tools, the chat's
            for every kind (a call's too: what a chat connects, a call does).
        call_ask_back: a phone session on an outbound call another agent
            placed: the ask tool joins the call tools.
        excluded_mcps: the ``mcpServers`` keys the pool does not start
            (``AgentConfig.excluded_mcps`` mapped by the layer).
        prebuilt_config: the builder's ``(config path, secret bundles)``.
        machine_id: the compute machine its stdio servers' processes run on.
    """
    if not system_prompt:
        # A caller that passes no prompt gets the agent's basic one (no
        # person, no permission context), built in a worker thread like the
        # builders' prompts.
        basic_prompt = await asyncio.to_thread(
            functools.partial(config.build_agent_prompt, agent_name, session_id=session_id))
        if not basic_prompt:
            raise ValueError(f"Unknown agent: {agent_name}")
        system_prompt = basic_prompt

    session_model = model or await run_db(_agent_model_or_empty, agent_name)

    # The text carries no time: it would change at every start. The time
    # rides on the first turn (``start_context.prelude``), and the turns that
    # take the time line carry it as the other engines' do. The history is
    # rebuilt from the chat's rows, which hold no riding part: this session
    # holds none of it.
    from core.session import start_context
    start_context.reset(session_id)

    # Start MCP servers (pass credential_env so MCPs get their API keys)
    mcp_manager = await mcp_pool.get_or_create(
        session_id, agent_name, phone_mode=phone_mode,
        credential_env=credential_env,
        sandbox_builder=sandbox_builder,
        tool_timeout=tool_timeout,
        excluded_mcps=excluded_mcps,
        prebuilt_config=prebuilt_config,
        machine_id=machine_id,
        # The programs of the platform's own the session carries.
        programs=(direct_files.PROGRAM,),
    )

    session = DirectSession(
        session_id=session_id,
        agent_name=agent_name,
        system_prompt=system_prompt,
        mcp_manager=mcp_manager,
        provider=provider,
        endpoint_url=endpoint_url,
        model=session_model,
        web_tools=web_tools,
        tool_grants=tool_grants,
    )
    if api_key:
        session.api_key = api_key
    # The call tools: on a phone session when the builder says the daemon
    # acts on them (an older daemon keeps the markers, so the model must not
    # see a tool nothing answers); in a chat of an agent that places calls
    # (phone-mcp among its servers) as the companion of an outbound call
    # that runs inside the chat. The CLI engines carry them through the
    # call-control MCP, which a direct session never attaches.
    if call_tools_wanted(phone_mode, call_tools, has_phone_mcp=_has_phone_mcp(session)):
        session.tools.extend(direct_builtins.call_tool_defs(ask_back=phone_mode and call_ask_back))
    # The MCP tool list is known only now (the prompt was built before the
    # servers started), so the deferred-tools split + catalog addendum
    # happen here, before the session is visible to anyone.
    _apply_deferred_tools(session)

    async with _direct_sessions_lock:
        _direct_sessions[session_id] = session

    deferred = len(session.catalog.deferred) if session.catalog else 0
    logger.info(
        "Created direct session %s for agent '%s' "
        "(provider=%s, model=%s, %s tools"
        "%s; "
        "prompt≈%s tokens, "
        "tools≈%s tokens)",
        session_id, agent_name, provider, session.model, len(session.tools),
        f', {deferred} deferred' if deferred else '', len(session.system_prompt) // 4,
        tool_catalog.estimate_tokens(session.tools)
    )
    return session


def call_tools_wanted(phone_mode: bool, call_tools: bool, *, has_phone_mcp: bool) -> bool:
    """Whether a direct session registers ``end_call`` and ``transfer_call``:
    a phone session only on the builder's word (the daemon's capability),
    a chat session of a phone-mcp agent always (its in-session calls)."""
    if phone_mode:
        return call_tools
    return has_phone_mcp


def _has_phone_mcp(session) -> bool:
    """Whether the phone MCP is among the session's servers (its mcpServers
    key is its manifest's server name)."""
    manager = session.mcp_manager
    if manager is None:
        return False
    try:
        from services.mcp import mcp_registry
        from services.phone.call_control import PHONE_MCP
        manifest = mcp_registry.get_manifest(PHONE_MCP)
    except ImportError:
        return False
    if manifest is None:
        return False
    key = getattr(manifest, "server_name", "") or manifest.name
    return key in getattr(manager, "servers", {})


def _registry_facts() -> tuple[set[str], dict[str, list[str]]]:
    """(always-load server keys, server key → on-demand skill ids) from the
    manifest registry; empty when the registry is not initialized (tests,
    startup race)."""
    always: set[str] = set()
    guides: dict[str, list[str]] = {}
    try:
        from services.mcp import mcp_registry
        for m in mcp_registry.get_all_manifests().values():
            key = getattr(m, "server_name", "") or m.name
            if getattr(m, "always_load", False):
                always.add(key)
            ids = [s.id for s in m.skills if s.loading == "on_demand"]
            if ids:
                guides[key] = ids
    except ImportError:
        logger.debug("deferred tools: registry facts unavailable", exc_info=True)
    return always, guides


def _apply_deferred_tools(session: DirectSession) -> None:
    """Split ``session.tools`` into resident + deferred per
    ``DIRECT_LLM_TOOL_SEARCH``; when deferral is active the session keeps the
    resident tools plus ``tool_search`` and its prompt gains the
    ``# Deferred tools`` catalog."""
    mode = config.DIRECT_LLM_TOOL_SEARCH
    threshold = config.DIRECT_LLM_TOOL_SEARCH_THRESHOLD_TOKENS
    always, guides = _registry_facts()
    resident, deferrable = tool_catalog.ToolCatalog.split(session.tools, always)
    if not tool_catalog.ToolCatalog.should_defer(mode, deferrable, threshold):
        session.catalog = None
        return
    catalog = tool_catalog.ToolCatalog(
        deferred={t["name"]: t for t in deferrable},
        guides={k: v for k, v in guides.items()},
    )
    session.tools = resident + [tool_catalog.tool_search_def()]
    session.system_prompt = (
        session.system_prompt.rstrip() + "\n\n---\n\n" + catalog.catalog_text()
    )
    session.catalog = catalog
    logger.info(
        "Deferred tools for session %s: "
        "%s deferred (≈%s tokens), "
        "%s resident (mode=%s)",
        session.session_id[:8], len(deferrable), tool_catalog.estimate_tokens(deferrable),
        len(resident), mode
    )


async def get_direct_session(session_id: str) -> DirectSession | None:
    """Look up an existing direct session."""
    async with _direct_sessions_lock:
        return _direct_sessions.get(session_id)


async def close_direct_session(session_id: str) -> bool:
    """Close a direct session and its MCP servers."""
    async with _direct_sessions_lock:
        session = _direct_sessions.pop(session_id, None)
    if not session:
        return False
    mark_closing(session_id)

    await mcp_pool.close_session(session_id)
    logger.info("Closed direct session %s", session_id)
    return True


async def _reap_idle_direct_pass() -> None:
    """One reaper tick: close direct sessions idle past the timeout, then the
    orphaned MCP managers."""
    from core.session.session_state import cached_idle_timeout
    idle_timeout = await cached_idle_timeout()  # once per tick, before the lock
    now = time.monotonic()
    to_reap = []

    async with _direct_sessions_lock:
        for sid, session in list(_direct_sessions.items()):
            # A session whose relayed tools went with their machine goes at
            # this tick, once no turn holds it.
            lost = (session.mcp_manager is not None and not session.lock.locked()
                    and session.mcp_manager.tools_lost())
            if (now - session.last_activity > idle_timeout or lost) and not has_pending_prompt(sid):
                to_reap.append(sid)
                del _direct_sessions[sid]
                mark_closing(sid)

    from core.concurrency import reap_session
    for sid in to_reap:
        logger.info("Reaping idle direct session: %s", sid)
        # The slot and the seat go with it (this bypasses layer.close_session).
        await reap_session(sid, mcp_pool.close_session)

    # Also reap orphaned MCP managers: one whose session lives is no orphan.
    async with _direct_sessions_lock:
        live = frozenset(_direct_sessions)
    await mcp_pool.reap_idle(timeout=idle_timeout, keep=live)


async def reap_idle_direct_sessions() -> None:
    """Background task: reap idle direct sessions periodically."""
    while True:
        await asyncio.sleep(60)
        try:
            await _reap_idle_direct_pass()
        except Exception as e:  # noqa: BLE001  # the reaper's per-tick step: one failure must not end the reaper
            logger.error("Direct session reaper error: %s", e)


def _take_steers(session: DirectSession) -> bool:
    """The texts typed during this turn join the history as one user message,
    list-shaped on every provider: the branches that drop a trailing bare
    user string (an abort, a provider error) must never remove it, while its
    row stands in the chat. Whether anything was taken."""
    if not session.steers:
        return False
    text = "\n\n".join(session.steers)
    session.steers.clear()
    session.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
    return True


_BINARY_PLACEHOLDER_RE = re.compile(r"^\[binary data: [^\]\n]*\]\n?", re.M)


# A model sees at most this many of one result's kept pictures: every one
# rides the session's history into each later request.
_RESULT_PICTURES_SHOWN = 4


async def _keep_result_files(session_id: str, tool_calls: list[dict], results: list[dict], *,
                             with_pictures: bool) -> None:
    """Hand each MCP result's image blocks and embedded resources to
    ``services/mcp/result_files`` (back on the main loop: the MCP manager
    ran the tools on its own); a kept result's ``[binary data]`` lines give
    way to the line naming the saved files and, ``with_pictures``, the chat
    copies of its pictures ride as the result's ``images`` (the adapter
    places them inside the tool result)."""
    by_id = {tc["id"]: tc for tc in tool_calls}
    for result in results:
        blocks = result.pop("file_blocks", None)
        if not blocks:
            continue
        from services.mcp import result_files
        name = (by_id.get(result["tool_use_id"]) or {}).get("name") or ""
        content = str(result.get("content") or "")
        text = _BINARY_PLACEHOLDER_RE.sub("", content).strip()
        kept = await result_files.keep_blocks(
            session_id, name, blocks, text, is_error=content[:6].lower().startswith("error"),
        )
        if kept is None:
            continue
        result["content"] = "\n".join(t for t in (text, kept.line) if t) or "(empty result)"
        if with_pictures and kept.vision:
            result["images"] = kept.vision[:_RESULT_PICTURES_SHOWN]


async def _append_user_message(
    session: DirectSession,
    adapter: ProviderAdapter,
    prompt: str,
    barge_in_chars: int | None,
    inject_time: bool,
    images: list[dict] | None,
) -> None:
    """The turn's user message onto the history: the barge-in annotation
    of the reply before it, the time and sibling prelude, the text alone
    or with the attached images as the adapter's content blocks."""
    # Handle barge-in annotation
    if barge_in_chars is not None and barge_in_chars > 0:
        session.annotate_barge_in(barge_in_chars)

    # The time line where the turn takes it (mirrors CLI/Codex), else the
    # time rides with the start's data on the first turn.
    from core.session import start_context
    user_text = start_context.fence_turn(prompt)
    now = await run_db_fast(config.format_current_time, get_session_user_tz(session.session_id))
    if inject_time:
        user_text = f"[Current time: {now}]\n\n{user_text}"
    start = await start_context.prelude(
        session.session_id, parallel=inject_time, time_line="" if inject_time else now)
    if start.prefix:
        user_text = f"{start.prefix}\n\n{user_text}"

    # Build the user message content. With images, attach as content blocks
    # (provider-specific format via adapter); without, keep plain string for
    # max compatibility and minimal payload.
    if images:
        content_blocks: list[dict] = [{"type": "text", "text": user_text}]
        for img in images:
            content_blocks.append(adapter.format_image_content_block(
                media_type=img["media_type"],
                base64_data=img["base64"],
            ))
        session.messages.append({"role": "user", "content": content_blocks})
    else:
        session.messages.append({"role": "user", "content": user_text})
    start.started()


@dataclass
class _DirectUsage:
    """The turn's token counts across its model calls."""
    # Accumulate usage across tool-use loops (for cost calculation)
    total: ProviderUsage = field(default_factory=ProviderUsage)
    # Track last API call's usage (for context gauge — avoids double-counting prompt)
    last_call: ProviderUsage = field(default_factory=ProviderUsage)


class _TurnCost:
    """A turn's cost, one stretch per model it ran on. The long-context line
    stays judged on the whole turn's tokens across its calls, as it has been:
    judging it per call or per model would change what people are billed, a
    decision of its own."""

    def __init__(self, adapter: ProviderAdapter) -> None:
        self._adapter = adapter
        self._stretches: list[tuple[str, tuple[float, float, float, float], ProviderUsage]] = []
        self._counted = ProviderUsage()

    def close_stretch(self, model: str, rates: tuple[float, float, float, float],
                      total: ProviderUsage) -> None:
        """What ``total`` added since the last stretch ran on ``model``."""
        was = self._counted
        self._stretches.append((model, rates, ProviderUsage(
            input_tokens=total.input_tokens - was.input_tokens,
            output_tokens=total.output_tokens - was.output_tokens,
            cache_write_tokens=total.cache_write_tokens - was.cache_write_tokens,
            cache_read_tokens=total.cache_read_tokens - was.cache_read_tokens,
            web_search_requests=total.web_search_requests - was.web_search_requests,
        )))
        self._counted = replace(total)

    def total(self, turn: ProviderUsage) -> float:
        line = turn.input_tokens + turn.cache_write_tokens + turn.cache_read_tokens
        return sum(self._adapter.calculate_cost(model, usage, rates=rates, line_tokens=line)
                   for model, rates, usage in self._stretches)


@dataclass
class _DirectCall:
    """One model call of the turn: what its stream accumulated."""
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = ""
    raw_content: Any = None
    # Thinking phase bracket (start/delta/end — the Codex layer's
    # THINKING contract): opened lazily on the first reasoning
    # fragment, closed by the first non-reasoning event so a
    # reasoning-only call still ends its block.
    thinking_open: bool = False
    # Per-call timing (one INFO line per model call): where a slow
    # turn spends its time — time to the first token (prefill on a
    # local server; a tools change re-prefills), reasoning volume,
    # cached tokens — is otherwise invisible from the dashboard.
    call_started: float = 0.0
    first_token_at: float | None = None
    think_chars: int = 0
    text_chars: int = 0
    # The call's newest frame is text: a tool call that starts now follows
    # it in the same message, so that text is a progress note.
    said: bool = False
    # The stream answered the turn with an error frame: the caller returns
    # before its per-call log line.
    ended: bool = False


async def _stream_call(
    session: DirectSession,
    adapter: ProviderAdapter,
    api_key: str,
    call: _DirectCall,
    usage: _DirectUsage,
):
    """One model call's frames: the thinking bracket, the text, the tool
    rows and the usage; an error event or a provider error is the turn's
    error frame and sets ``call.ended``."""
    try:
        async for event in adapter.stream_response(
            api_key=api_key,
            model=session.model,
            system_prompt=session.system_prompt,
            messages=session.messages,
            tools=session.tools,
            max_tokens=config.DIRECT_LLM_MAX_TOKENS,
            endpoint_url=session.endpoint_url,
            effort=session.effort,
        ):
            if call.first_token_at is None and event.type in (
                "thinking_delta", "text_delta", "tool_start",
            ):
                call.first_token_at = time.monotonic()
            if event.type == "thinking_delta":
                call.said = False
                call.think_chars += len(event.text or "")
                if not call.thinking_open:
                    call.thinking_open = True
                    yield {"type": "thinking", "data": {"phase": "start"}}
                yield {"type": "thinking", "data": {"phase": "delta", "text": event.text}}
                continue
            if call.thinking_open and event.type != "tool_input_delta":
                call.thinking_open = False
                yield {"type": "thinking", "data": {"phase": "end", "text": ""}}

            if event.type == "text_delta":
                call.text_chars += len(event.text or "")
                call.said = call.said or bool((event.text or "").strip())
                yield {"type": "text", "data": {"content": event.text}}

            elif event.type == "tool_start":
                if call.said:
                    call.said = False
                    yield {"type": "text", "data": dict(NOTE_MARK)}
                yield {
                    "type": "tool_start",
                    "data": {
                        "name": event.tool_name,
                        "tool_use_id": event.tool_id,
                    },
                }

            elif event.type == "tool_input_delta":
                pass  # tool input accumulated inside adapter

            elif event.type == "tool_result":
                # A server-side tool (Anthropic web_search / web_fetch /
                # code_execution) ran inside the API and returned: the
                # dashboard's tool row ends here — the runner never
                # executes these, so no tool_stop / result message.
                call.said = False
                yield {
                    "type": "tool_end",
                    "data": {
                        "tool_use_id": event.tool_id,
                        "result_preview": (event.text or "")[:200],
                    },
                }

            elif event.type == "tool_stop":
                # Parse accumulated JSON for MCP tool input
                try:
                    tool_input = json.loads(
                        event.tool_input_json
                    ) if event.tool_input_json else {}
                except json.JSONDecodeError:
                    tool_input = {}
                call.tool_calls.append({
                    "id": event.tool_id,
                    "name": event.tool_name,
                    "input": tool_input,
                })

            elif event.type == "usage":
                if event.usage:
                    # Accumulate for cost (all API calls sum up)
                    usage.total.input_tokens += event.usage.input_tokens
                    usage.total.output_tokens += event.usage.output_tokens
                    usage.total.cache_write_tokens += event.usage.cache_write_tokens
                    usage.total.cache_read_tokens += event.usage.cache_read_tokens
                    usage.total.web_search_requests += event.usage.web_search_requests
                    # Snapshot for context gauge (last call only)
                    usage.last_call = event.usage

            elif event.type == "content":
                call.raw_content = event.raw_content

            elif event.type == "stop":
                call.stop_reason = event.stop_reason

            elif event.type == "error":
                yield {"type": "error", "data": {"message": event.text}}
                call.ended = True
                return

    except ProviderError as e:
        if call.thinking_open:
            call.thinking_open = False
            yield {"type": "thinking", "data": {"phase": "end", "text": ""}}
        logger.error(
            "Provider error (%s): status=%s, "
            "message=%s", session.provider, e.status_code, e.message
        )
        if e.status_code == 404 and "model" in e.message.lower():
            msg = (
                f"Model '{session.model}' not found at {session.provider}. "
                f"Check your model configuration."
            )
        else:
            msg = f"{session.provider} API error: {e.message}"
        # The relay refuses with its code as the body's `detail` (a vendor's
        # own error passed through carries `error`); the SDK's error is the
        # adapter's chained cause. A code with no sentence keeps the text above.
        from core.layers.providers.openai_adapter import _relay_prefix
        relay = _relay_prefix()
        body = getattr(e.__cause__, "body", None)
        code = body.get("detail") if isinstance(body, dict) else None
        if relay and (session.endpoint_url or "").startswith(relay) and isinstance(code, str):
            from services.billing import relay_client
            msg = relay_client.relay_error_message(code, msg)
        yield {"type": "error", "data": {"message": msg}}
        # Remove dangling user message if no response was generated
        if call.raw_content is None and session.messages and session.messages[-1]["role"] == "user":
            last_content = session.messages[-1].get("content")
            if isinstance(last_content, str):
                session.messages.pop()
                _lost_start(session.session_id, last_content)
        call.ended = True
        return


async def _gate_tool_calls(
    session: DirectSession, tool_calls: list[dict], perm_mode: str,
) -> tuple[list[dict], list[dict], list[tuple[dict, str]]]:
    """The permission gate per tool call: the MCP tools approved for the
    manager, the builtins approved to run in-process, the denied with
    their reasons (a prompt waits on the session's permission queue)."""
    approved_calls: list[dict] = []   # MCP tools
    builtin_calls: list[dict] = []    # in-process builtins
    denied_calls: list[tuple[dict, str]] = []

    for tc in tool_calls:
        # A deferred tool called directly by its exact name is
        # loaded on first use: weaker models skip tool_search and
        # call the catalog entry outright (the MCP server
        # validates the arguments and reports what is wrong).
        _cat = session.catalog
        _name = tc["name"] or ""
        if (
            _cat is not None and _name in _cat.deferred
            and not _cat.is_loaded(_name)
        ):
            session.tools.extend(_cat.load(
                [_name],
                whole_server=tool_catalog.batch_loads_for(session.provider),
            ))
            logger.info(
                "Deferred tool %s loaded on direct call "
                "(session %s)", _name, session.session_id[:8]
            )
        is_builtin = direct_builtins.is_builtin(_name)
        if is_builtin:
            outcome, deny_reason = await direct_builtins.gate_for_session(
                session, tc, perm_mode,
            )
        else:
            # MCP tools go through the ONE permission authority
            # (session_events.pre_tool → decide_tool_permission):
            # the same tier × mode table as before, plus what
            # every other placement gets — target revocation,
            # the device-grant auto-allow, the session
            # allow-memory, the meeting parent-mode and
            # routing backstop, and the
            # dashboard block-and-wait on the meeting's queue
            # for a participant. The authority never answers
            # "ask"/"defer" for a non-terminal session, so
            # anything but allow is a deny with its reason.
            from core.session import session_events
            decision = await session_events.pre_tool(
                session.session_id, _name, tc.get("input") or {},
            )
            outcome = "allow" if decision.get("decision") == "allow" else "deny"
            deny_reason = decision.get("reason") or (
                f"{tc['name']} requires interactive user approval "
                "and this session runs unattended."
            )
        if outcome == "allow":
            (builtin_calls if is_builtin else approved_calls).append(tc)
            continue
        if outcome == "deny":
            denied_calls.append((tc, deny_reason))
            continue
        request_id = str(uuid.uuid4())
        perm_queue = get_permission_queue(session.session_id)
        await perm_queue.put({
            "event_type": "permission_prompt",
            "request_id": request_id,
            "tool_name": tc["name"],
            "tool_input": tc.get("input", {}),
        })
        approved = await wait_for_permission(request_id, session.session_id, timeout=PROMPT_WAIT_S)
        if approved:
            (builtin_calls if is_builtin else approved_calls).append(tc)
        else:
            denied_calls.append((tc, "Tool use denied by user."))
    return approved_calls, builtin_calls, denied_calls


async def _execute_tool_calls(
    session: DirectSession,
    adapter: ProviderAdapter,
    tool_calls: list[dict],
    approved_calls: list[dict],
    builtin_calls: list[dict],
    denied_calls: list[tuple[dict, str]],
) -> list[dict]:
    """The results of one call's tools, in the order their frames go out:
    the builtins, the MCP tools, the denied; the kept result files and
    the turn's ``post_tool`` records."""
    # Execute approved tools — builtins in-process, then MCP tools
    results: list[dict] = []
    for tc in builtin_calls:
        _t0 = time.monotonic()
        content = await direct_builtins.execute(
            session, tc["name"], tc.get("input") or {},
        )
        results.append({"tool_use_id": tc["id"], "content": content})
        logger.info(
            "Builtin %s ran in %.1f ms "
            "(session %s)",
            tc['name'], (time.monotonic() - _t0) * 1000, session.session_id[:8]
        )
    if approved_calls and session.mcp_manager:
        _t0 = time.monotonic()
        results.extend(await session.mcp_manager.execute_tools(approved_calls))
        logger.info(
            "MCP tools %s ran in "
            "%.2fs (session %s)",
            [tc['name'] for tc in approved_calls], time.monotonic() - _t0,
            session.session_id[:8]
        )
    elif approved_calls:
        results.extend(
            {
                "tool_use_id": tc["id"],
                "content": "Error: No MCP tools available",
            }
            for tc in approved_calls
        )

    # Add denied results
    for tc, reason in denied_calls:
        results.append({
            "tool_use_id": tc["id"],
            "content": reason,
        })

    # The pictures and files of a tool its MCP opts in
    # (``result_files``): kept and shown, the saved paths named
    # in the tool's text, the pictures beside it for a model the
    # adapter shows them to.
    await _keep_result_files(session.session_id, tool_calls, results,
                             with_pictures=adapter.tool_result_images)

    # The turn's record (session_events.post_tool) — the direct
    # placement's ONE source, denied calls included (flagged).
    from core.session import session_events as _events
    _by_id = {tc["id"]: tc for tc in tool_calls}
    _denied_ids = {tc["id"] for tc, _ in denied_calls}
    for result in results:
        _tc = _by_id.get(result["tool_use_id"]) or {}
        _content = result.get("content") or ""
        _events.post_tool(
            session.session_id, _tc.get("name") or "",
            tool_use_id=result["tool_use_id"],
            tool_input=_tc.get("input") or {},
            is_error=(result["tool_use_id"] in _denied_ids
                      or str(_content)[:6].lower().startswith("error")),
            source="direct",
        )
    return results


def _lost_start(session_id: str, content: str) -> None:
    """A first turn removed unanswered (a provider error, a barge-in) took
    the start's block with it: the next turn carries every section."""
    from core.session import start_context
    if content.startswith(start_context.OPEN):
        start_context.reset(session_id)


def _truncate_context(session: DirectSession, context_used: int, context_window: int) -> int:
    """Drop the oldest messages once the context nears its limit; the
    number dropped (0: nothing)."""
    CONTEXT_TRUNCATE_PCT = 0.80
    CONTEXT_KEEP_MESSAGES = 6  # 3 user/assistant pairs
    if (
        context_window > 0
        and context_used / context_window > CONTEXT_TRUNCATE_PCT
        and len(session.messages) > CONTEXT_KEEP_MESSAGES
    ):
        dropped = len(session.messages) - CONTEXT_KEEP_MESSAGES
        session.messages = session.messages[-CONTEXT_KEEP_MESSAGES:]
        # The blind slice can open the kept history mid tool-exchange — a
        # leading tool_result user message (Anthropic) or role="tool"
        # message (OpenAI) whose originating tool_use/tool_calls message
        # was dropped — and the provider 400s the next API call. Advance
        # the head to the next plain user message (which also satisfies
        # Anthropic's history-must-open-with-a-user-message rule).
        while session.messages:
            head = session.messages[0]
            head_content = head.get("content")
            if head.get("role") == "user" and not (
                isinstance(head_content, list)
                and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in head_content
                )
            ):
                break
            session.messages.pop(0)
            dropped += 1
        logger.info(
            "Context truncated for session %s: "
            "dropped %s messages, kept %s "
            "(%s/%s tokens = "
            "%s%%)",
            session.session_id[:8], dropped, CONTEXT_KEEP_MESSAGES, context_used,
            context_window, context_used * 100 // context_window
        )
        # The first turn's riding part went with the dropped messages: the
        # next turn carries it again.
        from core.session import start_context
        start_context.reset(session.session_id)
        return dropped
    return 0


def _turn_numbers(model: str, provider: str) -> tuple[tuple[float, float, float, float], int]:
    """A turn's rates and its model's context window. Store reads."""
    return config.get_model_pricing(model, provider), config.get_model_context_window(model)


async def run_direct_stream(
    session: DirectSession,
    prompt: str,
    barge_in_chars: int | None = None,
    inject_time: bool = False,
    images: list[dict] | None = None,
):
    """Stream an LLM response with tool use support.

    Provider-agnostic: delegates to the appropriate ProviderAdapter for
    streaming, tool formatting, and message serialization.

    Yields SSE event dicts: {"type": str, "data": dict}
    Event types: session, text, tool_start, tool_end, metadata, done, error

    inject_time: if True, prepend ``[Current time: ...]`` to the user message
    using the session's user_tz (set via client_info on WS connect). Same
    pattern as CLI / Codex — keeps long-lived sessions on accurate time.

    images: list of ``{"base64": str, "media_type": str}`` — chat-attached
    photos. When non-empty, the user message body is built as a content-block
    list (one text block + one image block per image) using the provider
    adapter's ``format_image_content_block``. Direct LLM has no built-in Read
    tool — this is how Anthropic / OpenAI vision works on this path. Empty /
    None → user content stays as a plain string (regression-safe).
    """
    session.touch()
    adapter = get_adapter(session.provider)
    turn_start = time.monotonic()

    await _append_user_message(session, adapter, prompt, barge_in_chars, inject_time, images)
    session.steer_open = True

    # Emit session event
    yield {"type": "session", "data": {"session_id": session.session_id}}

    # API key comes from the subscription pool (session-specific). There is NO
    # global fallback anymore (config.ANTHROPIC_API_KEY was removed with the
    # provider-agnostic pool — the old `or` fallback here raised AttributeError
    # the moment a session arrived credential-less, masking the real problem).
    # Keyless local providers (ollama / openai_compatible) fill their own
    # defaults in the adapter; for cloud providers an empty key must surface a
    # CLEAN error instead of an SDK auth stacktrace.
    effective_api_key = session.api_key or ""
    _adapter_has_default = bool(adapter.default_api_key()) \
        if hasattr(adapter, "default_api_key") else False
    if not effective_api_key and not _adapter_has_default:
        yield {"type": "error", "data": {"message": (
            f"No LLM credentials available for provider '{session.provider}'. "
            "Add a Direct LLM subscription (API key or endpoint) for this "
            "provider in Admin → Execution Layers, or connect the install to "
            "an OtoDock account for hosted credits."
        )}}
        return

    try:
        loop_count = 0
        usage = _DirectUsage()
        # Read here, never at the turn's end: an await before the metadata
        # lets a barge-in lose a finished turn's cost and its truncation.
        turn_model = session.model
        rates, context_window = await run_db_fast(_turn_numbers, turn_model, adapter.provider_name)
        costed = _TurnCost(adapter)

        while loop_count < MAX_TOOL_LOOPS:
            loop_count += 1

            # A model picked during the tool loop takes effect at this call:
            # what ran so far is priced at the model that ran it.
            if session.model != turn_model:
                costed.close_stretch(turn_model, rates, usage.total)
                turn_model = session.model
                rates, context_window = await run_db_fast(_turn_numbers, turn_model, adapter.provider_name)

            # Stream via provider adapter
            call = _DirectCall(call_started=time.monotonic())
            async for frame in _stream_call(session, adapter, effective_api_key, call, usage):
                yield frame
            if call.ended:
                return

            if call.thinking_open:
                # Stream ended on a reasoning fragment (no stop event).
                yield {"type": "thinking", "data": {"phase": "end", "text": ""}}

            _now = time.monotonic()
            logger.info(
                "Direct call %s for session %s: "
                "ttft=%.1fs "
                "total=%.1fs "
                "in=%s cached=%s "
                "written=%s "
                "out=%s "
                "searches=%s "
                "think_chars=%s "
                "text_chars=%s tool_calls=%s "
                "tools_sent=%s",
                loop_count, session.session_id[:8], (call.first_token_at or _now) - call.call_started,
                _now - call.call_started, usage.last_call.input_tokens,
                usage.last_call.cache_read_tokens, usage.last_call.cache_write_tokens,
                usage.last_call.output_tokens, usage.last_call.web_search_requests, call.think_chars,
                call.text_chars, len(call.tool_calls), len(session.tools)
            )

            # Store assistant message via adapter's serialization
            if call.raw_content is not None:
                serialized = adapter.serialize_assistant_content(call.raw_content)
                if isinstance(serialized, dict):
                    # OpenAI format: merge content + tool_calls into message
                    session.messages.append({"role": "assistant", **serialized})
                else:
                    session.messages.append({
                        "role": "assistant",
                        "content": serialized,
                    })

            # If MCP tools were called, execute them and loop
            if call.stop_reason == "tool_use" and call.tool_calls:
                # Permission gate: manifest permission tier × session mode per
                # tool (services/mcp/mcp_permissions.py) — the same table the
                # CLI hook applies, evaluated inline since the direct path has
                # no hook subprocess. open runs silently, standard and
                # sensitive ask in Ask (Direct LLM offers no Auto: it has no
                # reviewer), critical asks everywhere and is denied in
                # unattended `auto` sessions (nobody can answer).
                # Client-side builtins (Read / Write / … — builtins.py) run the
                # CLI hook's two-pass gate inline: path policy, then the
                # builtin tier × mode table (Delete asks in Ask, like `rm`).
                # Their handlers run in-process, never via the MCP manager's
                # batch, and a file tool's handler asks the file program.
                perm_mode = get_session_mode(session.session_id) or "auto"
                approved_calls, builtin_calls, denied_calls = await _gate_tool_calls(
                    session, call.tool_calls, perm_mode,
                )
                results = await _execute_tool_calls(
                    session, adapter, call.tool_calls, approved_calls, builtin_calls, denied_calls,
                )

                # Emit tool_end events
                for result in results:
                    yield {
                        "type": "tool_end",
                        "data": {
                            "tool_use_id": result["tool_use_id"],
                            "result_preview": result["content"][:200],
                        },
                    }

                # Append tool results in provider-specific format
                result_messages = adapter.format_tool_results(results)
                session.messages.extend(result_messages)

                if session.line_closing:
                    # The daemon accepted an end or a transfer: the line
                    # closes as the farewell drains, so a model call after
                    # the tool result would only be transcript.
                    session.line_closing = False
                    break
                # A message typed during this call is read here, as the CLI
                # engines read one at their next tool boundary.
                _take_steers(session)
                call.tool_calls = []
                continue

            # A message typed after this call's last tool: the next call of
            # the same turn. No await between this check and the door's
            # closing below, so an accepted text is never left behind.
            if _take_steers(session):
                continue
            break

        session.steer_open = False

        # Emit metadata with cost + context + cache stats (per-turn delta)
        # Cost uses accumulated totals across all API calls in the tool loop.
        # Context uses only the LAST API call's tokens — that represents the
        # actual context window usage (system prompt + tools + full history).
        # Accumulated totals would double-count the system prompt on each tool loop.
        costed.close_stretch(turn_model, rates, usage.total)
        cost_usd = costed.total(usage.total)
        context_used = (
            usage.last_call.input_tokens
            + usage.last_call.cache_read_tokens
            + usage.last_call.cache_write_tokens
            + usage.last_call.output_tokens
        )
        duration_ms = int((time.monotonic() - turn_start) * 1000)
        yield {
            "type": "metadata",
            "data": {
                "cost_usd": round(cost_usd, 6),
                "duration_ms": duration_ms,
                "input_tokens": usage.total.input_tokens,
                "output_tokens": usage.total.output_tokens,
                "cache_read": usage.last_call.cache_read_tokens,
                "cache_write": usage.last_call.cache_write_tokens,
                "context_used": context_used,
                "context_max": context_window,
            },
        }

        # Auto-truncate context when approaching the limit.
        # Keeps last CONTEXT_KEEP_MESSAGES messages, drops older ones.
        # This prevents "context too long" errors on the next turn.
        dropped = _truncate_context(session, context_used, context_window)
        if dropped:
            yield {
                "type": "context_compact",
                "data": {
                    "message": f"Context approaching limit — older messages trimmed to free space.",
                },
            }

        yield {"type": "done", "data": {}}

    except asyncio.CancelledError:
        # Barge-in: generator was cancelled
        session.steers.clear()
        if session.messages and session.messages[-1]["role"] == "user":
            last_content = session.messages[-1].get("content")
            if isinstance(last_content, str):
                session.messages.pop()
                _lost_start(session.session_id, last_content)
        raise

    except Exception as e:
        logger.exception("Direct stream error: %s", e)
        yield {"type": "error", "data": {"message": str(e)}}

    finally:
        # Every exit shuts the door. What it left waiting (the tool loop's
        # cap, a provider error) is read with the next turn.
        session.steer_open = False
        _take_steers(session)
        session.touch()
