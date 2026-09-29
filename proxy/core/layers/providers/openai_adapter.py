"""OpenAI provider adapter for the Direct LLM path.

Two OpenAI wire formats behind one adapter:

- The **Responses API** (``client.responses``) for OpenAI's own endpoint and
  the OtoDock relay's OpenAI route — the only way to send reasoning effort
  together with function tools (chat completions reject the pair), with
  streamed reasoning summaries and usage that carries the cache detail.
  Stateless: ``store: false`` on every request, the chain of thought kept
  across a turn's tool loop through encrypted reasoning items. Models flagged
  ``server_tools`` in the registry also get OpenAI's built-in ``web_search``
  (the API runs the searches inside the response; each ``web_search_call``
  item shows as a server tool row and is priced per call).
- The **Chat Completions API** (``client.chat.completions``) for a custom
  base URL on the ``openai`` provider (an OpenAI-compatible gateway), for
  the ``DIRECT_LLM_OPENAI_API=chat`` escape hatch, and for the
  OpenAI-compatible providers (Groq, Ollama, LM Studio, …) that subclass
  this adapter.

The session history is ALWAYS kept in the chat-completions shape
(``{"role": "assistant", "content", "tool_calls"}`` / ``{"role": "tool"}``);
the Responses path translates it into input items on every request, so the
runner, the DB resume and the context truncation see one shape.
"""

import json
import logging
from typing import AsyncIterator

import config as app_config
from core.layers.providers.base import (
    ProviderAdapter, ProviderStreamEvent, ProviderUsage, ProviderError,
)

logger = logging.getLogger("direct-runner")

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"

# Platform effort → OpenAI's reasoning scale (shared by both APIs). "xhigh",
# "max" AND "ultra" all collapse onto "xhigh" (the top of that scale; "ultra"
# is a Codex-CLI orchestration mode — even codex-rs sends the API "max").
_EFFORT_TO_OPENAI = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
    "ultra": "xhigh",
}

# Responses API: ask for the encrypted reasoning so a stateless (store: false)
# tool loop can hand the model its own chain of thought back.
_RESPONSES_INCLUDE = ["reasoning.encrypted_content"]


def _effort_to_openai(effort: str) -> str | None:
    return _EFFORT_TO_OPENAI.get(effort)


def _thinking_enabled(effort: str) -> bool:
    """Local servers expose thinking as a switch, not a scale: the platform's
    lowest effort turns it off, everything above leaves it on."""
    return effort != "low"


def _relay_prefix() -> str:
    """``OTODOCK_RELAY_BASE`` as a URL prefix (trailing slash), "" when unset."""
    base = (getattr(app_config, "OTODOCK_RELAY_BASE", "") or "").rstrip("/")
    return base + "/" if base else ""


def _summary_rejected(err) -> bool:
    """A 400 that names reasoning summaries — OpenAI gates them on organization
    verification, which a BYO key may lack."""
    return getattr(err, "status_code", 0) == 400 and "summary" in str(err).lower()


class _ThinkTagSplitter:
    """Route ``<think>…</think>`` spans that some OpenAI-compatible servers
    stream INSIDE ``delta.content`` (reasoning parsing off) to the thinking
    channel. Tags can straddle chunk boundaries, so a trailing fragment that
    could still become a tag is held back until the next chunk decides."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buf += text
        out: list[tuple[str, str]] = []
        while self._buf:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            kind = "thinking" if self._in_think else "text"
            idx = self._buf.find(tag)
            if idx >= 0:
                if idx:
                    out.append((kind, self._buf[:idx]))
                self._buf = self._buf[idx + len(tag):]
                self._in_think = not self._in_think
                continue
            # Hold back a suffix that is a proper prefix of the tag.
            hold = 0
            for n in range(min(len(tag) - 1, len(self._buf)), 0, -1):
                if self._buf.endswith(tag[:n]):
                    hold = n
                    break
            emit, self._buf = self._buf[:len(self._buf) - hold], self._buf[len(self._buf) - hold:]
            if emit:
                out.append((kind, emit))
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self._buf:
            return []
        piece, self._buf = self._buf, ""
        return [("thinking" if self._in_think else "text", piece)]


def _web_search_replay_item(item) -> dict:
    """The stored (and replayed) shape of a Responses ``web_search_call`` output
    item: the fields the API requires back — the server id, the status and a
    minimal ``action`` (``sources`` and other decoration dropped)."""
    action = getattr(item, "action", None)
    dump: dict = {"type": getattr(action, "type", None) or "search"}
    for key in ("query", "queries", "url", "pattern"):
        value = getattr(action, key, None)
        if value:
            dump[key] = value
    return {
        "type": "web_search_call",
        "id": getattr(item, "id", "") or "",
        "status": getattr(item, "status", None) or "completed",
        "action": dump,
    }


def _web_search_preview(replay: dict) -> str:
    """The dashboard's result preview for a finished web search: what it
    searched / opened, plus the status when the call did not complete."""
    action = replay.get("action") or {}
    what = action.get("query") or "; ".join(action.get("queries") or []) \
        or action.get("url") or ""
    if action.get("pattern"):
        what = f"{action['pattern']} in {action.get('url', '')}".strip()
    status = replay.get("status") or "completed"
    if status != "completed":
        return f"{status}: {what}".strip(": ")
    return what


def _user_content_items(content):
    """A stored user message body (string, or chat-shape content blocks —
    ``text`` / ``image_url``) → Responses input content."""
    if isinstance(content, list):
        parts: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append({"type": "input_text", "text": block.get("text") or ""})
            elif kind == "image_url":
                url = block.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                parts.append({
                    "type": "input_image", "image_url": url or "", "detail": "auto",
                })
            elif kind in ("input_text", "input_image"):
                parts.append(block)
        return parts
    return content if isinstance(content, str) else str(content or "")


class OpenAIAdapter(ProviderAdapter):
    """OpenAI adapter: Responses API for OpenAI itself, Chat Completions for
    everything OpenAI-compatible (see the module docstring)."""

    # Set once OpenAI rejected ``reasoning.summary`` for this process's key
    # (organization not verified): later requests keep the effort, drop the
    # summary, and the thinking channel simply stays empty.
    _summary_unsupported = False

    @property
    def provider_name(self) -> str:
        return "openai"

    def _get_base_url(self, endpoint_url: str | None) -> str | None:
        """Base URL for the API. Override in subclasses for compat providers."""
        return endpoint_url  # None = OpenAI default

    def default_api_key(self) -> str | None:
        """Default API key when none provided. Override for local providers."""
        return None

    def _extra_api_kwargs(self, model: str, has_tools: bool, effort: str) -> dict:
        """Provider-specific chat-completion params merged into every request.
        Override in subclasses (Groq pins qwen models to non-thinking; the
        local adapters translate the platform effort into their servers'
        thinking switch)."""
        return {}

    def _effort_allowed_with_tools(self) -> bool:
        """Chat Completions only: whether ``reasoning_effort`` may accompany
        function tools. OpenAI's ``/v1/chat/completions`` REJECTS the pair
        (verified live 2026-09-06 on gpt-5.6-luna: 400 "Function tools with
        reasoning_effort are not supported … use /v1/responses or set
        reasoning_effort to 'none'"), so the base keeps the no-tools condition
        for the chat path — which the ``openai`` provider only takes for a
        custom base URL or under ``DIRECT_LLM_OPENAI_API=chat``; its normal
        path is the Responses API, where effort rides with tools. Providers
        verified to accept the pair on chat completions override to True."""
        return False

    def _uses_responses_api(self, endpoint_url: str | None) -> bool:
        """The Responses API is spoken to OpenAI's own endpoint (no base URL)
        and to the OtoDock relay's OpenAI route; a custom base URL on the
        ``openai`` provider keeps chat completions. Subclasses (Groq, Ollama,
        OpenAI-compatible) report their own ``provider_name`` and never take
        this path; ``DIRECT_LLM_OPENAI_API=chat`` pins OpenAI to it too."""
        if self.provider_name != "openai":
            return False
        if getattr(app_config, "DIRECT_LLM_OPENAI_API", "responses") != "responses":
            return False
        if not endpoint_url:
            return True
        relay = _relay_prefix()
        return bool(relay) and endpoint_url.startswith(relay)

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    @staticmethod
    def _decompose_usage(usage) -> ProviderUsage:
        """OpenAI chat-completions usage → ProviderUsage in the convention
        ``calculate_cost`` expects (Anthropic-style: ``input_tokens`` = tokens
        billed at the plain input rate only).

        OpenAI's ``prompt_tokens`` INCLUDES cache reads and (gpt-5.6+) cache
        writes, so both are subtracted out and reported on their own fields:

        - ``prompt_tokens_details.cached_tokens`` — read from cache, billed at
          the 90%-discount rate (also what Groq reports for its automatic
          50%-discount caching; the discount itself lives in the pricing tuple).
        - ``prompt_tokens_details.cache_write_tokens`` — gpt-5.6+ only: writes
          are billed at 1.25x the input rate (implicit AND explicit caching)
          and reported per call. The pinned SDK's typed model doesn't declare
          the field yet, but pydantic ``extra="allow"`` keeps it addressable
          via getattr; pre-5.6 models never send it (writes were free there).
        """
        cached = 0
        written = 0
        ptd = getattr(usage, "prompt_tokens_details", None)
        if ptd:
            cached = getattr(ptd, "cached_tokens", 0) or 0
            written = getattr(ptd, "cache_write_tokens", 0) or 0
        total_prompt = usage.prompt_tokens or 0
        return ProviderUsage(
            input_tokens=max(0, total_prompt - cached - written),
            output_tokens=usage.completion_tokens or 0,
            cache_read_tokens=cached,
            cache_write_tokens=written,
        )

    @staticmethod
    def _decompose_responses_usage(usage) -> ProviderUsage:
        """Responses API usage → ProviderUsage, same convention as above:
        ``input_tokens`` includes the cached (``input_tokens_details.cached_tokens``)
        and, gpt-5.6+, the written (``cache_write_tokens``, via getattr) part —
        both come out of the plain-rate column; ``output_tokens`` already
        includes the reasoning tokens (billed as output)."""
        cached = 0
        written = 0
        itd = getattr(usage, "input_tokens_details", None)
        if itd:
            cached = getattr(itd, "cached_tokens", 0) or 0
            written = getattr(itd, "cache_write_tokens", 0) or 0
        total_input = getattr(usage, "input_tokens", 0) or 0
        return ProviderUsage(
            input_tokens=max(0, total_input - cached - written),
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=cached,
            cache_write_tokens=written,
        )

    # ------------------------------------------------------------------
    # Tools + history translation
    # ------------------------------------------------------------------

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert MCP-format tools to OpenAI chat-completions function tools.

        MCP format: {"name", "description", "input_schema"}
        OpenAI format: {"type": "function", "function": {"name", "description", "parameters"}}
        """
        openai_tools = []
        for t in tools:
            if "input_schema" not in t:
                continue  # skip server-side tools (Anthropic-only)
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            })
        return openai_tools

    def _convert_tools_responses(self, tools: list[dict]) -> list[dict]:
        """MCP-format tools → Responses API function tools (flat shape).
        ``strict`` defaults to TRUE there and would reject every schema that
        lacks ``additionalProperties: false`` / all-required properties —
        MCP schemas are not written that way, so it is switched off."""
        out = []
        for t in tools:
            if "input_schema" not in t:
                continue
            out.append({
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
                "strict": False,
            })
        return out

    @staticmethod
    def _responses_input(messages: list[dict]) -> list[dict]:
        """The stored chat-shape history → Responses input items.

        - user: string content or ``input_text`` / ``input_image`` parts;
        - assistant: its stored Responses items (the encrypted reasoning and
          the ``web_search_call`` items, in output order) ONLY when the
          message belongs to the current turn (after the last user message —
          the tool loop, where OpenAI needs the chain of thought back, and a
          reasoning item needs the item that followed it), then the text as a
          plain assistant message, then one ``function_call`` per tool call
          that has its result in the history (a call without an output is a
          400; an aborted tool loop leaves such calls behind);
        - tool: ``function_call_output``.

        Replayed function calls never carry server ids (``fc_…`` / ``msg_…``
        would make the API demand the paired reasoning item); the reasoning
        and web search items keep theirs, as the API requires.
        """
        last_user = -1
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                last_user = i
        answered = {
            m.get("tool_call_id") for m in messages if m.get("role") == "tool"
        }
        items: list[dict] = []
        for i, m in enumerate(messages):
            role = m.get("role")
            if role == "user":
                items.append({
                    "role": "user", "content": _user_content_items(m.get("content")),
                })
            elif role == "assistant":
                if i > last_user:
                    for r in m.get("reasoning") or []:
                        if not isinstance(r, dict):
                            continue
                        if r.get("encrypted_content") or r.get("type") == "web_search_call":
                            items.append(r)
                text = m.get("content")
                if isinstance(text, str) and text:
                    items.append({"role": "assistant", "content": text})
                for tc in m.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    call_id = tc.get("id") or ""
                    if call_id not in answered:
                        continue
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if not isinstance(args, str):
                        args = json.dumps(args or {})
                    items.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": fn.get("name") or "",
                        "arguments": args or "{}",
                    })
            elif role == "tool":
                out = m.get("content")
                if not isinstance(out, str):
                    out = json.dumps(out) if out is not None else ""
                items.append({
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id") or "",
                    "output": out,
                })
            elif role == "system":
                items.append({"role": "system", "content": str(m.get("content") or "")})
        return items

    @staticmethod
    def _chat_history(messages: list[dict]) -> list[dict]:
        """The stored history for a chat-completions request: the Responses
        path's ``reasoning`` key must not reach that API (a session that
        switched APIs mid-life carries it)."""
        out: list[dict] = []
        for m in messages:
            if m.get("role") == "assistant" and "reasoning" in m:
                m = {k: v for k, v in m.items() if k != "reasoning"}
            out.append(m)
        return out

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

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
        from openai import AsyncOpenAI

        effective_key = api_key or self.default_api_key() or ""
        base_url = self._get_base_url(endpoint_url)

        client_kwargs: dict = {"api_key": effective_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = AsyncOpenAI(**client_kwargs)

        try:
            if self._uses_responses_api(endpoint_url):
                gen = self._stream_responses(
                    client, model=model, system_prompt=system_prompt,
                    messages=messages, tools=tools, max_tokens=max_tokens,
                    effort=effort,
                )
            else:
                gen = self._stream_chat_completions(
                    client, model=model, system_prompt=system_prompt,
                    messages=messages, tools=tools, max_tokens=max_tokens,
                    effort=effort,
                )
            async for event in gen:
                yield event
        finally:
            await client.close()

    # --- Responses API -------------------------------------------------

    def _responses_request(
        self, *, model: str, system_prompt: str, messages: list[dict],
        tools: list[dict], max_tokens: int, effort: str,
    ) -> dict:
        kwargs: dict = {
            "model": model,
            "instructions": system_prompt,
            "input": self._responses_input(messages),
            "max_output_tokens": max_tokens,
            "stream": True,
            "store": False,
        }
        rtools = self._convert_tools_responses(tools)
        if rtools and app_config.model_supports_server_tools(model):
            # OpenAI's built-in web search rides with the client tools only —
            # a tool-less request (chat titles) must not carry it.
            rtools.append({"type": "web_search"})
        if rtools:
            kwargs["tools"] = rtools
        if app_config.model_supports_reasoning(model):
            # A reasoning model reasons at its default effort too — always ask
            # for the encrypted items so the tool loop keeps the chain of
            # thought; the effort itself only when the agent set one.
            kwargs["include"] = list(_RESPONSES_INCLUDE)
            mapped = _effort_to_openai(effort) if effort else None
            if mapped:
                reasoning: dict = {"effort": mapped}
                if not self._summary_unsupported:
                    reasoning["summary"] = "auto"
                kwargs["reasoning"] = reasoning
                # max_output_tokens includes the reasoning tokens here.
                kwargs["max_output_tokens"] = max(
                    max_tokens,
                    int(getattr(app_config, "DIRECT_LLM_OPENAI_REASONING_MAX_TOKENS", 0) or 0),
                )
        return kwargs

    async def _create_responses_stream(self, client, kwargs: dict):
        from openai import APIError

        try:
            return await client.responses.create(**kwargs)
        except APIError as e:
            reasoning = kwargs.get("reasoning")
            if isinstance(reasoning, dict) and "summary" in reasoning and _summary_rejected(e):
                # Organization-verification gate on summaries: keep the effort,
                # drop the summary, remember it for the process.
                type(self)._summary_unsupported = True
                logger.warning(
                    "OpenAI rejected reasoning summaries for this key (organization "
                    "not verified?) — retrying without a summary; the thinking "
                    "channel stays empty on this provider"
                )
                retry = dict(kwargs)
                retry["reasoning"] = {k: v for k, v in reasoning.items() if k != "summary"}
                return await client.responses.create(**retry)
            raise

    async def _stream_responses(
        self, client, *, model: str, system_prompt: str, messages: list[dict],
        tools: list[dict], max_tokens: int, effort: str,
    ) -> AsyncIterator[ProviderStreamEvent]:
        from openai import APIError

        kwargs = self._responses_request(
            model=model, system_prompt=system_prompt, messages=messages,
            tools=tools, max_tokens=max_tokens, effort=effort,
        )
        # Function calls by output index; ``done`` marks a call the server
        # completed (only those are handed to the runner).
        calls: dict[int, dict] = {}
        # The items replayed within the turn's tool loop, in output order:
        # encrypted reasoning and the server-run web searches.
        replay_items: list[dict] = []
        # Web searches the API ran inside this response: open rows (by item
        # id) and the completed count (billed per call).
        open_searches: set[str] = set()
        searches = 0
        accumulated_text = ""
        completed = False

        def _usage_event(usage) -> ProviderStreamEvent:
            u = self._decompose_responses_usage(usage)
            u.web_search_requests = searches
            return ProviderStreamEvent(type="usage", usage=u)

        try:
            stream = await self._create_responses_stream(client, kwargs)
            async for ev in stream:
                etype = getattr(ev, "type", "") or ""

                if etype == "response.output_item.added":
                    item = getattr(ev, "item", None)
                    itype = getattr(item, "type", "") or ""
                    if itype == "function_call":
                        call_id = getattr(item, "call_id", "") or ""
                        name = getattr(item, "name", "") or ""
                        calls[ev.output_index] = {
                            "id": call_id, "name": name, "arguments": "",
                        }
                        yield ProviderStreamEvent(
                            type="tool_start", tool_name=name, tool_id=call_id,
                        )
                    elif itype == "web_search_call":
                        sid = getattr(item, "id", "") or ""
                        open_searches.add(sid)
                        yield ProviderStreamEvent(
                            type="tool_start", tool_name="web_search", tool_id=sid,
                        )

                elif etype == "response.function_call_arguments.delta":
                    tc = calls.get(getattr(ev, "output_index", -1))
                    delta = getattr(ev, "delta", "") or ""
                    if tc is not None and delta:
                        tc["arguments"] += delta
                        yield ProviderStreamEvent(
                            type="tool_input_delta", tool_input_json=delta,
                        )

                elif etype == "response.output_item.done":
                    item = getattr(ev, "item", None)
                    itype = getattr(item, "type", "") or ""
                    if itype == "function_call":
                        idx = getattr(ev, "output_index", -1)
                        tc = calls.setdefault(idx, {
                            "id": getattr(item, "call_id", "") or "",
                            "name": getattr(item, "name", "") or "",
                            "arguments": "",
                        })
                        final_args = getattr(item, "arguments", None)
                        if isinstance(final_args, str) and final_args:
                            tc["arguments"] = final_args
                        status = getattr(item, "status", None)
                        if status in (None, "completed"):
                            tc["done"] = True
                            yield ProviderStreamEvent(
                                type="tool_stop",
                                tool_name=tc["name"],
                                tool_id=tc["id"],
                                tool_input_json=tc["arguments"],
                            )
                    elif itype == "reasoning":
                        encrypted = getattr(item, "encrypted_content", None)
                        if encrypted:
                            summary = [
                                {"type": "summary_text", "text": getattr(s, "text", "") or ""}
                                for s in (getattr(item, "summary", None) or [])
                            ]
                            replay_items.append({
                                "type": "reasoning",
                                "id": getattr(item, "id", "") or "",
                                "summary": summary,
                                "encrypted_content": encrypted,
                            })
                    elif itype == "web_search_call":
                        replay = _web_search_replay_item(item)
                        sid = replay["id"]
                        if sid not in open_searches:
                            # No ``added`` event came first: open the row now.
                            yield ProviderStreamEvent(
                                type="tool_start", tool_name="web_search", tool_id=sid,
                            )
                        open_searches.discard(sid)
                        if replay["status"] == "completed":
                            searches += 1
                        replay_items.append(replay)
                        yield ProviderStreamEvent(
                            type="tool_result", tool_name="web_search", tool_id=sid,
                            text=_web_search_preview(replay),
                        )

                elif etype in ("response.output_text.delta", "response.refusal.delta"):
                    piece = getattr(ev, "delta", "") or ""
                    if piece:
                        accumulated_text += piece
                        yield ProviderStreamEvent(type="text_delta", text=piece)

                elif etype in (
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                ):
                    piece = getattr(ev, "delta", "") or ""
                    if piece:
                        yield ProviderStreamEvent(type="thinking_delta", text=piece)

                elif etype == "response.completed":
                    completed = True
                    usage = getattr(getattr(ev, "response", None), "usage", None)
                    if usage:
                        yield _usage_event(usage)
                    any_call = any(tc.get("done") for tc in calls.values())
                    yield ProviderStreamEvent(
                        type="stop", stop_reason="tool_use" if any_call else "end_turn",
                    )

                elif etype == "response.incomplete":
                    response = getattr(ev, "response", None)
                    usage = getattr(response, "usage", None)
                    if usage:
                        yield _usage_event(usage)
                    details = getattr(response, "incomplete_details", None)
                    reason = getattr(details, "reason", None) or "incomplete"
                    if not accumulated_text:
                        raise ProviderError(
                            message=(
                                f"OpenAI ended the response before producing any "
                                f"text ({reason})"
                                + (
                                    " — the output budget was spent on reasoning; "
                                    "lower the agent's effort or raise "
                                    "DIRECT_LLM_OPENAI_REASONING_MAX_TOKENS"
                                    if reason == "max_output_tokens" else ""
                                )
                            ),
                            status_code=0, retryable=False,
                        )
                    yield ProviderStreamEvent(
                        type="stop",
                        stop_reason="length" if reason == "max_output_tokens" else reason,
                    )

                elif etype == "response.failed":
                    err = getattr(getattr(ev, "response", None), "error", None)
                    raise ProviderError(
                        message=getattr(err, "message", None)
                        or "OpenAI reported the response failed",
                        status_code=0, retryable=False,
                    )

                elif etype == "error":
                    raise ProviderError(
                        message=getattr(ev, "message", None) or "OpenAI stream error",
                        status_code=0, retryable=False,
                    )

        except APIError as e:
            raise ProviderError(
                message=str(e),
                status_code=getattr(e, "status_code", 0),
                retryable=getattr(e, "status_code", 0) in (429, 500, 502, 503),
            )

        # A search whose ``done`` item never came (the stream ended first):
        # close its row rather than leave it spinning.
        for sid in sorted(open_searches):
            yield ProviderStreamEvent(
                type="tool_result", tool_name="web_search", tool_id=sid, text="",
            )
        open_searches.clear()

        raw = {
            "content": accumulated_text or None,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for _, tc in sorted(calls.items())
                if tc.get("done")
            ] if completed else [],
            "reasoning": replay_items,
        }
        yield ProviderStreamEvent(type="content", raw_content=raw)

    # --- Chat Completions API ------------------------------------------

    async def _stream_chat_completions(
        self, client, *, model: str, system_prompt: str, messages: list[dict],
        tools: list[dict], max_tokens: int, effort: str,
    ) -> AsyncIterator[ProviderStreamEvent]:
        from openai import APIError

        # Build messages: system prompt first, then conversation
        api_messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        api_messages.extend(self._chat_history(messages))

        # Convert tools to OpenAI format
        openai_tools = self._convert_tools(tools)

        # OpenAI's newer models (o-series, gpt-4.1+) require
        # max_completion_tokens instead of max_tokens.
        # Use max_completion_tokens universally — it works for all
        # current OpenAI models and is the forward-compatible option.
        api_kwargs: dict = {
            "model": model,
            "messages": api_messages,
            "max_completion_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if openai_tools:
            api_kwargs["tools"] = openai_tools

        # Reasoning effort — only for reasoning-capable models, and (unless
        # the provider is verified to accept the pair) only WITHOUT tools:
        # OpenAI's /v1/chat/completions rejects reasoning_effort next to
        # function tools — see _effort_allowed_with_tools.
        if effort and (not openai_tools or self._effort_allowed_with_tools()):
            openai_effort = _effort_to_openai(effort)
            if openai_effort and app_config.model_supports_reasoning(model):
                api_kwargs["reasoning_effort"] = openai_effort

        api_kwargs.update(self._extra_api_kwargs(model, bool(openai_tools), effort))

        # Track accumulated content for raw_content event
        accumulated_text = ""
        # Track tool calls by index (OpenAI streams them incrementally)
        tool_call_acc: dict[int, dict] = {}
        # Only a stream that finished WITH tool calls hands them to the runner
        # and stores them: a call cut off by the output cap ("length") would
        # sit in the history without its result and 400 the next request.
        finished_with_tools = False
        splitter = _ThinkTagSplitter()

        try:
            stream = await client.chat.completions.create(**api_kwargs)

            async for chunk in stream:
                # Usage-only chunk (last chunk with stream_options.include_usage)
                if not chunk.choices:
                    if chunk.usage:
                        yield ProviderStreamEvent(
                            type="usage",
                            usage=self._decompose_usage(chunk.usage),
                        )
                    continue

                choice = chunk.choices[0]
                delta = choice.delta
                finish = choice.finish_reason

                # Reasoning content: llama.cpp / vLLM / DeepSeek-style
                # servers stream it as ``reasoning_content``, Groq as
                # ``reasoning``. Neither is declared on the SDK's typed
                # delta (pydantic extra="allow" keeps them addressable).
                if delta:
                    reasoning = (getattr(delta, "reasoning_content", None)
                                 or getattr(delta, "reasoning", None))
                    if isinstance(reasoning, str) and reasoning:
                        yield ProviderStreamEvent(
                            type="thinking_delta", text=reasoning,
                        )

                # Text content (with inline <think> spans split out)
                if delta and delta.content:
                    for kind, piece in splitter.feed(delta.content):
                        if kind == "thinking":
                            yield ProviderStreamEvent(
                                type="thinking_delta", text=piece,
                            )
                        else:
                            accumulated_text += piece
                            yield ProviderStreamEvent(
                                type="text_delta", text=piece,
                            )

                # Tool call deltas
                if delta and delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_call_acc:
                            tool_call_acc[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments": "",
                            }
                        tc = tool_call_acc[idx]

                        # ID arrives in first chunk for this index
                        if tc_delta.id:
                            tc["id"] = tc_delta.id

                        # Function name arrives in first chunk
                        if tc_delta.function and tc_delta.function.name:
                            tc["name"] = tc_delta.function.name
                            yield ProviderStreamEvent(
                                type="tool_start",
                                tool_name=tc["name"],
                                tool_id=tc["id"],
                            )

                        # Arguments stream incrementally
                        if tc_delta.function and tc_delta.function.arguments:
                            tc["arguments"] += tc_delta.function.arguments
                            yield ProviderStreamEvent(
                                type="tool_input_delta",
                                tool_input_json=tc_delta.function.arguments,
                            )

                # Finish reason
                if finish:
                    # Emit tool_stop events
                    if finish == "tool_calls":
                        finished_with_tools = True
                        for idx in sorted(tool_call_acc.keys()):
                            tc = tool_call_acc[idx]
                            yield ProviderStreamEvent(
                                type="tool_stop",
                                tool_name=tc["name"],
                                tool_id=tc["id"],
                                tool_input_json=tc["arguments"],
                            )

                    yield ProviderStreamEvent(
                        type="stop",
                        stop_reason=(
                            "tool_use" if finish == "tool_calls" else finish
                        ),
                    )

        except APIError as e:
            raise ProviderError(
                message=str(e),
                status_code=getattr(e, "status_code", 0),
                retryable=getattr(e, "status_code", 0) in (429, 500, 502, 503),
            )

        for kind, piece in splitter.flush():
            if kind == "thinking":
                yield ProviderStreamEvent(type="thinking_delta", text=piece)
            else:
                accumulated_text += piece
                yield ProviderStreamEvent(type="text_delta", text=piece)

        # Yield raw content for message serialization
        raw = {
            "content": accumulated_text or None,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                }
                for tc in (
                    tool_call_acc[i]
                    for i in sorted(tool_call_acc.keys())
                )
            ] if (tool_call_acc and finished_with_tools) else [],
        }
        yield ProviderStreamEvent(type="content", raw_content=raw)

    # ------------------------------------------------------------------
    # History serialization
    # ------------------------------------------------------------------

    def format_tool_results(self, results: list[dict]) -> list[dict]:
        """OpenAI format: separate tool messages per result."""
        return [
            {
                "role": "tool",
                "tool_call_id": r["tool_use_id"],
                "content": r["content"],
            }
            for r in results
        ]

    def serialize_assistant_content(self, raw_content) -> str | dict:
        """Serialize the response for the message history (chat shape).

        Returns str for simple text, or a dict with content + tool_calls (+
        ``reasoning``: the Responses path's replay items — encrypted
        reasoning and web search calls, in output order — replayed within
        the turn's tool loop) that the runner merges into the assistant
        message.
        """
        if isinstance(raw_content, dict):
            text = raw_content.get("content") or ""
            tcs = raw_content.get("tool_calls") or []
            reasoning = raw_content.get("reasoning") or []
            if not tcs and not reasoning:
                return text
            result: dict = {"content": text or None}
            if tcs:
                result["tool_calls"] = tcs
            if reasoning:
                result["reasoning"] = reasoning
            return result
        return str(raw_content)

    async def list_available_models(
        self,
        api_key: str,
        endpoint_url: str | None = None,
    ) -> list[dict]:
        """Fetch models from OpenAI API."""
        from openai import AsyncOpenAI

        client_kwargs: dict = {"api_key": api_key}
        base_url = self._get_base_url(endpoint_url)
        if base_url:
            client_kwargs["base_url"] = base_url

        client = AsyncOpenAI(**client_kwargs)
        try:
            response = await client.models.list()
            models = []
            for m in response.data:
                # Filter to chat-capable models (skip embeddings, tts, etc.)
                mid = m.id
                if any(skip in mid for skip in (
                    "embedding", "tts", "whisper", "dall-e",
                    "moderation", "davinci", "babbage",
                )):
                    continue
                models.append({
                    "model_id": mid,
                    "display_name": mid,
                })
            return sorted(models, key=lambda x: x["model_id"])
        finally:
            await client.close()
