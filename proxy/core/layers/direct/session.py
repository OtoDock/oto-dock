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
import json
import logging
import time
import uuid

import config
from core.layers.direct.mcp import AgentMCPManager, mcp_pool
from core.layers.providers import get_adapter, ProviderError, ProviderUsage
from core.session.session_state import (
    get_session_mode,
    get_permission_queue,
    wait_for_permission,
    get_session_user_tz,
)

logger = logging.getLogger("direct-runner")

# Safety limit: max tool-use loop iterations per request
MAX_TOOL_LOOPS = 20


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
    ):
        self.session_id = session_id
        self.agent_name = agent_name
        # Model is almost always set explicitly by DirectLLMExecutionLayer.start_session
        # via config.model — resolve here as a defensive fallback. Catch RuntimeError
        # so the constructor doesn't fail if no model is resolvable yet; callers that
        # need an actual model will set session.model afterward or fail at first turn.
        try:
            self.model = config.get_agent_model(agent_name)
        except RuntimeError:
            self.model = ""
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

        # Populate tools from MCP manager
        if mcp_manager:
            self.tools = mcp_manager.get_tools()

        # Add provider-specific built-in tools (e.g., Anthropic web_search/web_fetch)
        adapter = get_adapter(provider)
        builtin = adapter.get_builtin_tools()
        if builtin:
            self.tools.extend(builtin)
            logger.info(
                f"Added {len(builtin)} built-in tools for {provider}: "
                f"{[t.get('name', t.get('type', '?')) for t in builtin]}"
            )

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
                f"Annotated barge-in: {spoken_chars} chars spoken, "
                f"{len(unheard)} chars unheard"
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
                            f"Annotated barge-in (content block): {spoken_chars} chars spoken"
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
) -> DirectSession:
    """Create a new direct session with MCP servers started.

    Args:
        system_prompt: Pre-built prompt from config_builder (includes user context,
            permissions, MCP skills, dynamic context, etc.). If empty, falls back
            to building a basic prompt from the agent's prompt.md.
    """
    if not system_prompt:
        # Fallback: basic prompt without user/permission context (phone path, etc.)
        system_prompt = config.build_agent_prompt(agent_name)
        if not system_prompt:
            raise ValueError(f"Unknown agent: {agent_name}")

    # Add datetime context (same as CLI path). The system-prompt time is the
    # baseline; per-turn injection in run_direct_stream() refreshes it on each
    # user message so long-lived sessions don't show stale time.
    _initial_user_tz = get_session_user_tz(session_id)
    system_prompt = f"Current date and time: {config.format_current_time(_initial_user_tz)}\n\n{system_prompt}"

    # Start MCP servers (pass credential_env so MCPs get their API keys)
    mcp_manager = await mcp_pool.get_or_create(
        session_id, agent_name, phone_mode=phone_mode,
        credential_env=credential_env,
        sandbox_builder=sandbox_builder,
    )

    session = DirectSession(
        session_id=session_id,
        agent_name=agent_name,
        system_prompt=system_prompt,
        mcp_manager=mcp_manager,
        provider=provider,
        endpoint_url=endpoint_url,
    )
    if api_key:
        session.api_key = api_key

    async with _direct_sessions_lock:
        _direct_sessions[session_id] = session

    logger.info(
        f"Created direct session {session_id} for agent '{agent_name}' "
        f"(provider={provider}, model={session.model}, {len(session.tools)} tools)"
    )
    return session


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

    await mcp_pool.close_session(session_id)
    logger.info(f"Closed direct session {session_id}")
    return True


async def reap_idle_direct_sessions() -> None:
    """Background task: reap idle direct sessions periodically."""
    while True:
        await asyncio.sleep(60)
        try:
            now = time.monotonic()
            to_reap = []

            async with _direct_sessions_lock:
                for sid, session in list(_direct_sessions.items()):
                    if now - session.last_activity > config.get_idle_timeout():
                        to_reap.append(sid)
                        del _direct_sessions[sid]

            for sid in to_reap:
                logger.info(f"Reaping idle direct session: {sid}")
                await mcp_pool.close_session(sid)
                # Release concurrency slot + subscription (bypasses layer.close_session)
                from core.concurrency import release_chat_slot
                release_chat_slot(sid)
                from services.engines.subscription_pool import release_subscription
                release_subscription(sid)

            # Also reap orphaned MCP managers
            await mcp_pool.reap_idle()
        except Exception as e:
            logger.error(f"Direct session reaper error: {e}")


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

    # Handle barge-in annotation
    if barge_in_chars is not None and barge_in_chars > 0:
        session.annotate_barge_in(barge_in_chars)

    # Optional per-turn datetime injection (mirrors CLI/Codex). The system
    # prompt's date is set at session start and goes stale on long sessions.
    user_text = prompt
    if inject_time:
        user_tz = get_session_user_tz(session.session_id)
        user_text = f"[Current time: {config.format_current_time(user_tz)}]\n\n{prompt}"
        from core.session import sibling_awareness
        sibling_line = await sibling_awareness.prelude_line(session.session_id)
        if sibling_line:
            user_text = f"{sibling_line}\n\n{user_text}"

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
    _adapter_has_default = bool(adapter._get_default_api_key()) \
        if hasattr(adapter, "_get_default_api_key") else False
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
        # Accumulate usage across tool-use loops (for cost calculation)
        total_usage = ProviderUsage()
        # Track last API call's usage (for context gauge — avoids double-counting prompt)
        last_call_usage = ProviderUsage()
        raw_content = None

        while loop_count < MAX_TOOL_LOOPS:
            loop_count += 1

            # Stream via provider adapter
            tool_calls: list[dict] = []
            stop_reason = ""
            raw_content = None

            try:
                async for event in adapter.stream_response(
                    api_key=effective_api_key,
                    model=session.model,
                    system_prompt=session.system_prompt,
                    messages=session.messages,
                    tools=session.tools,
                    max_tokens=config.DIRECT_LLM_MAX_TOKENS,
                    endpoint_url=session.endpoint_url,
                    effort=session.effort,
                ):
                    if event.type == "text_delta":
                        yield {"type": "text", "data": {"content": event.text}}

                    elif event.type == "tool_start":
                        yield {
                            "type": "tool_start",
                            "data": {
                                "name": event.tool_name,
                                "tool_use_id": event.tool_id,
                            },
                        }

                    elif event.type == "tool_input_delta":
                        pass  # tool input accumulated inside adapter

                    elif event.type == "tool_stop":
                        # Parse accumulated JSON for MCP tool input
                        try:
                            tool_input = json.loads(
                                event.tool_input_json
                            ) if event.tool_input_json else {}
                        except json.JSONDecodeError:
                            tool_input = {}
                        tool_calls.append({
                            "id": event.tool_id,
                            "name": event.tool_name,
                            "input": tool_input,
                        })

                    elif event.type == "usage":
                        if event.usage:
                            # Accumulate for cost (all API calls sum up)
                            total_usage.input_tokens += event.usage.input_tokens
                            total_usage.output_tokens += event.usage.output_tokens
                            total_usage.cache_write_tokens += event.usage.cache_write_tokens
                            total_usage.cache_read_tokens += event.usage.cache_read_tokens
                            # Snapshot for context gauge (last call only)
                            last_call_usage = event.usage

                    elif event.type == "content":
                        raw_content = event.raw_content

                    elif event.type == "stop":
                        stop_reason = event.stop_reason

                    elif event.type == "error":
                        yield {"type": "error", "data": {"message": event.text}}
                        return

            except ProviderError as e:
                logger.error(
                    f"Provider error ({session.provider}): status={e.status_code}, "
                    f"message={e.message}"
                )
                if e.status_code == 404 and "model" in e.message.lower():
                    msg = (
                        f"Model '{session.model}' not found at {session.provider}. "
                        f"Check your model configuration."
                    )
                else:
                    msg = f"{session.provider} API error: {e.message}"
                yield {"type": "error", "data": {"message": msg}}
                # Remove dangling user message if no response was generated
                if raw_content is None and session.messages and session.messages[-1]["role"] == "user":
                    last_content = session.messages[-1].get("content")
                    if isinstance(last_content, str):
                        session.messages.pop()
                return

            # Store assistant message via adapter's serialization
            if raw_content is not None:
                serialized = adapter.serialize_assistant_content(raw_content)
                if isinstance(serialized, dict):
                    # OpenAI format: merge content + tool_calls into message
                    session.messages.append({"role": "assistant", **serialized})
                else:
                    session.messages.append({
                        "role": "assistant",
                        "content": serialized,
                    })

            # If MCP tools were called, execute them and loop
            if stop_reason == "tool_use" and tool_calls:
                # Permission gate: manifest permission tier × session mode per
                # tool (services/mcp/mcp_permissions.py) — the same table the
                # CLI hook applies, evaluated inline since the direct path has
                # no hook subprocess. open runs silently, standard is silent
                # in acceptEdits, sensitive prompts in both prompting modes,
                # critical prompts everywhere and is denied in unattended
                # `auto` sessions (nobody can answer).
                from services.mcp import mcp_permissions
                perm_mode = get_session_mode(session.session_id) or "auto"

                approved_calls: list[dict] = []
                denied_calls: list[tuple[dict, str]] = []

                for tc in tool_calls:
                    parts = (tc["name"] or "").split("__", 2)
                    tier = mcp_permissions.resolve_tool_tier(
                        parts[1] if len(parts) >= 2 else "",
                        parts[2] if len(parts) >= 3 else "",
                    )
                    outcome = mcp_permissions.tier_decision(tier, perm_mode)
                    if outcome == "allow":
                        approved_calls.append(tc)
                        continue
                    if outcome == "deny":
                        denied_calls.append((tc, (
                            f"{tc['name']} requires interactive user approval "
                            "and this session runs unattended."
                        )))
                        continue
                    request_id = str(uuid.uuid4())
                    perm_queue = get_permission_queue(session.session_id)
                    await perm_queue.put({
                        "event_type": "permission_prompt",
                        "request_id": request_id,
                        "tool_name": tc["name"],
                        "tool_input": tc.get("input", {}),
                    })
                    approved = await wait_for_permission(request_id, session.session_id, timeout=604800.0)
                    if approved:
                        approved_calls.append(tc)
                    else:
                        denied_calls.append((tc, "Tool use denied by user."))

                # Execute approved tools
                results: list[dict] = []
                if approved_calls and session.mcp_manager:
                    results = await session.mcp_manager.execute_tools(approved_calls)
                elif approved_calls:
                    results = [
                        {
                            "tool_use_id": tc["id"],
                            "content": "Error: No MCP tools available",
                        }
                        for tc in approved_calls
                    ]

                # Add denied results
                for tc, reason in denied_calls:
                    results.append({
                        "tool_use_id": tc["id"],
                        "content": reason,
                    })

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

                tool_calls = []
                continue

            # No tool use — done
            break

        # Emit metadata with cost + context + cache stats (per-turn delta)
        # Cost uses accumulated totals across all API calls in the tool loop.
        # Context uses only the LAST API call's tokens — that represents the
        # actual context window usage (system prompt + tools + full history).
        # Accumulated totals would double-count the system prompt on each tool loop.
        cost_usd = adapter.calculate_cost(session.model, total_usage)
        context_window = config.get_model_context_window(session.model)
        context_used = (
            last_call_usage.input_tokens
            + last_call_usage.cache_read_tokens
            + last_call_usage.cache_write_tokens
            + last_call_usage.output_tokens
        )
        duration_ms = int((time.monotonic() - turn_start) * 1000)
        yield {
            "type": "metadata",
            "data": {
                "cost_usd": round(cost_usd, 6),
                "duration_ms": duration_ms,
                "input_tokens": total_usage.input_tokens,
                "output_tokens": total_usage.output_tokens,
                "cache_read": last_call_usage.cache_read_tokens,
                "cache_write": last_call_usage.cache_write_tokens,
                "context_used": context_used,
                "context_max": context_window,
            },
        }

        # Auto-truncate context when approaching the limit.
        # Keeps last CONTEXT_KEEP_MESSAGES messages, drops older ones.
        # This prevents "context too long" errors on the next turn.
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
                f"Context truncated for session {session.session_id[:8]}: "
                f"dropped {dropped} messages, kept {CONTEXT_KEEP_MESSAGES} "
                f"({context_used}/{context_window} tokens = "
                f"{context_used * 100 // context_window}%)"
            )
            yield {
                "type": "context_compact",
                "data": {
                    "message": f"Context approaching limit — older messages trimmed to free space.",
                },
            }

        yield {"type": "done", "data": {}}

    except asyncio.CancelledError:
        # Barge-in: generator was cancelled
        if session.messages and session.messages[-1]["role"] == "user":
            last_content = session.messages[-1].get("content")
            if isinstance(last_content, str):
                session.messages.pop()
        raise

    except Exception as e:
        logger.error(f"Direct stream error: {e}", exc_info=True)
        yield {"type": "error", "data": {"message": str(e)}}

    finally:
        session.touch()
