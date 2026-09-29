"""Anthropic provider adapter for the Direct LLM path.

Handles all Anthropic-specific API interaction: cache_control on system
prompt and tools, content_block streaming events, server_tool_use blocks
(web_search, web_fetch), and Anthropic message format.
"""

import logging
from typing import AsyncIterator

import anthropic

import config as app_config
from core.layers.providers.base import (
    ProviderAdapter, ProviderStreamEvent, ProviderUsage, ProviderError,
)

logger = logging.getLogger("direct-runner")


def _with_history_breakpoint(messages: list[dict]) -> list[dict]:
    """Copy of ``messages`` with a ``cache_control`` breakpoint on the LAST
    message's last content block — caching the whole conversation prefix.

    Anthropic caching is breakpoint-explicit and prefix-based (tools → system
    → messages). The adapter already marks the system prompt + last tool, but
    without a breakpoint inside ``messages`` the conversation history re-bills
    at the full input rate on EVERY call — each tool-loop iteration within a
    turn and each follow-up turn resend the growing history. Marking the last
    message makes every next call read that prefix at 0.1x (write once at
    1.25x), which is the lowest-cost default for multi-turn / tool-loop
    traffic. (Up to 4 breakpoints are allowed; we use 3 — a moving one here
    plus system + last tool. The API caches the longest previously-marked
    prefix, so moving the marker forward each call still HITS the prior
    prefix.) Copy-on-write: history dicts live in the session's message list
    and must not grow stale markers."""
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return messages  # empty text can't carry a breakpoint
        last["content"] = [{"type": "text", "text": content,
                            "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        blocks = list(content)
        # thinking/redacted_thinking blocks reject cache_control; every block
        # type we actually send last (text / tool_result / image) accepts it.
        if blocks[-1].get("type") in ("thinking", "redacted_thinking"):
            return messages
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        last["content"] = blocks
    else:
        return messages
    return messages[:-1] + [last]


def _serialize_content_blocks(content_blocks: list) -> list[dict]:
    """Serialize SDK content block objects to dicts for the messages API.

    Handles text, tool_use, server_tool_use, web_search_tool_result,
    web_fetch_tool_result, and other block types.
    """
    result = []
    for block in content_blocks:
        if hasattr(block, "model_dump"):
            dumped = block.model_dump(exclude_none=True, exclude_unset=True)
            result.append(dumped)
        elif isinstance(block, dict):
            result.append(block)
        else:
            result.append({"type": "unknown", "data": str(block)})
    return result


def _server_tool_result_preview(block) -> str:
    """A short human-readable preview of a server tool's result block for
    the dashboard's tool row (``result_preview``): a count for list results
    (web search hits), the error code for an error, else the block's text."""
    content = getattr(block, "content", None)
    if isinstance(content, list):
        return f"{len(content)} result(s)"
    err = getattr(content, "error_code", None) if content is not None else None
    if err:
        return f"error: {err}"
    if content is None:
        return ""
    return str(content)[:200]


class AnthropicAdapter(ProviderAdapter):
    """Anthropic Claude API adapter."""

    # The provider's own server-run tools, appended to session.tools for
    # models with server_tools=True. The BASIC tool versions on purpose
    # (decided 2026-09-07): the 2026 versions add "dynamic filtering" — the
    # model writes code that filters the results inside a code-execution
    # sandbox before reading them — which on a chat assistant showed as a
    # pile of code_execution rows (a weather question ran 3 searches + 2
    # fetches through 6 code executions, 41 s, and still answered without
    # live data because fetched weather pages carry no readable forecast).
    # Direct search hands the result snippets straight to the model, the way
    # OpenAI's built-in search does, and needs one step for the same question.
    # Lived in config.py as EXECUTION_PATH_BUILTIN_TOOLS["direct-llm"] until
    # 2026-09-20 — vendor tool ids are the adapter's business, not an engine's.
    SERVER_TOOLS: tuple[dict, ...] = (
        {"type": "web_search_20250305", "name": "web_search"},
        {"type": "web_fetch_20250910", "name": "web_fetch"},
    )

    @property
    def provider_name(self) -> str:
        return "anthropic"

    async def stream_response(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        endpoint_url: str | None = None,
        effort: str = "",
    ) -> AsyncIterator[ProviderStreamEvent]:
        # endpoint_url overrides the API base — used to route through the OtoDock
        # hosted relay (base_url = {RELAY}/v1/relay/anthropic; the SDK appends
        # /v1/messages). When unset, the SDK's default Anthropic endpoint is used.
        client_kwargs: dict = {"api_key": api_key}
        if endpoint_url:
            client_kwargs["base_url"] = endpoint_url
        client = anthropic.AsyncAnthropic(**client_kwargs)

        try:
            # Build API kwargs
            api_kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": _with_history_breakpoint(messages),
            }

            # Reasoning effort — adaptive thinking with effort level. ONLY for
            # reasoning-capable models: a non-reasoning model (e.g. Haiku 4.5) rejects
            # the thinking block with 400 "adaptive thinking is not supported on this
            # model", so we gate on supports_reasoning (mirrors the OpenAI adapter) and
            # silently drop the effort for models that can't think.
            # Platform scale: low < medium < high < xhigh < max. Passed through as-is;
            # xhigh is only valid on Opus 4.7+, so fall back to "max" on older models.
            # "ultra" is Codex-only (multi-agent orchestration) → clamp to "max".
            if (effort and effort in ("low", "medium", "high", "xhigh", "max", "ultra")
                    and app_config.model_supports_reasoning(model)):
                wire_effort = effort
                if wire_effort == "ultra":
                    wire_effort = "max"
                if wire_effort == "xhigh" and not app_config.get_model_supports_xhigh(model):
                    wire_effort = "max"
                api_kwargs["thinking"] = {"type": "adaptive"}
                api_kwargs["output_config"] = {"effort": wire_effort}

            # System prompt — wrap with cache_control for prompt caching
            api_kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

            # Tools — filter out server-side tools for models that don't support them,
            # then mark last tool as cache breakpoint
            if not app_config.model_supports_server_tools(model):
                # Remove server-side tools (they have 'type' field like 'web_search_20250305')
                tools = [t for t in tools if "input_schema" in t]
            if tools:
                formatted_tools = list(tools)
                formatted_tools[-1] = {
                    **formatted_tools[-1],
                    "cache_control": {"type": "ephemeral"},
                }
                api_kwargs["tools"] = formatted_tools

            # Stream response
            current_tool_json = ""
            final_message = None
            # Server-side tools (web_search, web_fetch, code_execution …) run
            # inside the API: their call is a ``server_tool_use`` block and
            # their result the following ``*_tool_result`` block. The runner
            # never executes them, so it is the result block that ends the
            # tool's turn for the dashboard (tool_end) — without it the tool
            # row spun forever (live-hit 2026-09-07). Tracked by id so a
            # server tool the stream never answers is still closed at the end.
            open_server_tools: dict[str, str] = {}

            try:
                async with client.messages.stream(**api_kwargs) as stream:
                    current_tool_name = ""
                    current_tool_id = ""

                    async for event in stream:
                        if event.type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                current_tool_name = block.name
                                current_tool_id = block.id
                                current_tool_json = ""
                                yield ProviderStreamEvent(
                                    type="tool_start",
                                    tool_name=block.name,
                                    tool_id=block.id,
                                )
                            elif block.type == "server_tool_use":
                                # Server-side tool (web_search, web_fetch)
                                open_server_tools[block.id] = block.name
                                yield ProviderStreamEvent(
                                    type="tool_start",
                                    tool_name=block.name,
                                    tool_id=block.id,
                                )
                            elif str(block.type).endswith("_tool_result"):
                                result_id = getattr(block, "tool_use_id", "") or ""
                                yield ProviderStreamEvent(
                                    type="tool_result",
                                    tool_name=open_server_tools.pop(result_id, ""),
                                    tool_id=result_id,
                                    text=_server_tool_result_preview(block),
                                )

                        elif event.type == "content_block_delta":
                            if hasattr(event.delta, "text"):
                                yield ProviderStreamEvent(
                                    type="text_delta",
                                    text=event.delta.text,
                                )
                            elif hasattr(event.delta, "partial_json"):
                                current_tool_json += event.delta.partial_json
                                yield ProviderStreamEvent(
                                    type="tool_input_delta",
                                    tool_input_json=event.delta.partial_json,
                                )
                            elif getattr(event.delta, "thinking", None):
                                # Adaptive thinking streams its text as
                                # thinking_delta blocks (signature_delta is
                                # opaque and stays ignored).
                                yield ProviderStreamEvent(
                                    type="thinking_delta",
                                    text=event.delta.thinking,
                                )

                        elif event.type == "content_block_stop":
                            if current_tool_name:
                                yield ProviderStreamEvent(
                                    type="tool_stop",
                                    tool_name=current_tool_name,
                                    tool_id=current_tool_id,
                                    tool_input_json=current_tool_json,
                                )
                                current_tool_name = ""
                                current_tool_id = ""
                                current_tool_json = ""

                        elif event.type == "message_delta":
                            stop_reason = getattr(
                                event.delta, "stop_reason", None
                            )
                            if stop_reason:
                                yield ProviderStreamEvent(
                                    type="stop",
                                    stop_reason=stop_reason,
                                )

                    # Get final assembled message
                    final_message = await stream.get_final_message()

                # A server tool whose result block never came (the stream
                # ended first): close its row rather than leave it spinning.
                for tool_id, tool_name in open_server_tools.items():
                    yield ProviderStreamEvent(
                        type="tool_result", tool_name=tool_name, tool_id=tool_id,
                        text="",
                    )
                open_server_tools.clear()

            except anthropic.APIError as e:
                raise ProviderError(
                    message=str(e),
                    status_code=getattr(e, "status_code", 0),
                    retryable=getattr(e, "status_code", 0) in (429, 500, 502, 503),
                )

            # Extract usage
            if final_message and final_message.usage:
                u = final_message.usage
                # The SDK accumulates the cumulative message_delta usage, so a
                # server-tool turn reports every iteration's tokens here; the
                # searches it ran (billed per call) ride in server_tool_use.
                server = getattr(u, "server_tool_use", None)
                yield ProviderStreamEvent(
                    type="usage",
                    usage=ProviderUsage(
                        input_tokens=u.input_tokens or 0,
                        output_tokens=u.output_tokens or 0,
                        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                        web_search_requests=getattr(server, "web_search_requests", 0) or 0,
                    ),
                )

            # Yield raw content for serialization
            if final_message and final_message.content:
                yield ProviderStreamEvent(
                    type="content",
                    raw_content=final_message.content,
                )

        finally:
            await client.close()

    def format_tool_results(self, results: list[dict]) -> list[dict]:
        """Anthropic format: single user message with tool_result content blocks."""
        tool_results = []
        for r in results:
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": r["tool_use_id"],
                "content": r["content"],
            })
        return [{"role": "user", "content": tool_results}]

    def serialize_assistant_content(self, raw_content) -> str | list[dict]:
        """Serialize Anthropic content blocks for message history."""
        content_blocks = _serialize_content_blocks(raw_content)
        if (
            len(content_blocks) == 1
            and isinstance(content_blocks[0], dict)
            and content_blocks[0].get("type") == "text"
        ):
            return content_blocks[0]["text"]
        return content_blocks

    def get_builtin_tools(self, model: str = "") -> list[dict]:
        """Anthropic server-side tools: web_search, web_fetch.

        Only returned for models that support programmatic tool calling
        (configured via server_tools flag in config.MODEL_REGISTRY).
        """
        if model and not app_config.model_supports_server_tools(model):
            return []
        return list(self.SERVER_TOOLS)

    def format_image_content_block(
        self,
        *,
        media_type: str,
        base64_data: str,
    ) -> dict:
        """Anthropic vision content block — uses ``source.base64`` shape.

        See https://docs.anthropic.com/en/docs/build-with-claude/vision —
        image blocks live alongside text blocks in a user message's content
        list. ``media_type`` must be one of ``image/jpeg``, ``image/png``,
        ``image/gif``, ``image/webp``.
        """
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64_data,
            },
        }

    async def list_available_models(
        self,
        api_key: str,
        endpoint_url: str | None = None,
    ) -> list[dict]:
        """Anthropic has no list models endpoint — return from registry."""
        return [
            {"model_id": mid, "display_name": info["label"]}
            for mid, info in app_config.MODEL_REGISTRY.items()
            if "direct-llm" in info.get("layers", [])
        ]
