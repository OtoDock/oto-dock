"""Out-of-band MCP tool invocation for agent_context builder blocks.

At session-build time, ``dynamic_context._resolve_manifest_blocks`` calls
``execute_builder()`` for each block that declares a ``builder`` field. This
module substitutes ``${ns.key}`` tokens into the builder's ``args``, opens
an HTTP/SSE MCP session, invokes the named tool with a hard wall-clock
timeout, and renders the block's ``template`` with the result exposed via
the ``${result.*}`` namespace.

Failure semantics: every error path returns ``None`` and the caller drops
the block silently — same fail-safe contract as the ``requires`` gate.
The block never half-renders; the surrounding prompt never crashes.

Each invocation runs on a throwaway event loop in its own thread, so a
timed-out call can never damage the proxy's main loop: abandoned client
async generators are finalized when the private loop shuts down (instead
of surfacing later as foreign-task anyio teardown errors on the shared
loop), and an SDK cancellation that wedges mid-teardown strands only a
daemon thread instead of hanging the session build.

Stdio MCPs are rejected: subprocess-per-session MCPs have no out-of-band
channel for pre-session calls. ``mcp_registry._validate_builder_block_transports``
strips stdio-backed builder blocks at startup; the same check repeats here
as defence in depth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from services.mcp import dynamic_context
from services.mcp import mcp_registry

logger = logging.getLogger(__name__)


# Entry ``type`` values from ``resolve_server_config`` that we know how to
# invoke. ``streamable-http`` is the canonical one-shot protocol; ``sse``
# also works via the MCP SDK but we prefer streamable_http because we ask
# the resolver for the TOML/Codex format (which yields the ``/mcp`` path).
_INVOKE_ENTRY_TYPES = {"streamable-http", "sse"}


async def execute_builder(
    *,
    block,
    tokens: dict[str, str],
    trigger_payload: dict | None,
    mcp_name: str,
    agent_name: str,
    user_sub: str = "",
) -> str | None:
    """Invoke a builder block's MCP tool and render its template.

    Returns the fully-substituted prompt block on success, ``None`` on any
    failure (timeout, network error, tool error, parse error, MCP gone,
    stdio transport). All failures log WARN; nothing raises.

    Args:
        block: ``AgentContextBlock`` whose ``builder`` is non-None.
        tokens: pre-built source-token map (account/credential/agent/user/
            session/trigger flat keys). Doesn't yet contain ``result.*``.
        trigger_payload: raw payload dict for ``${trigger.body.<path>}``
            walks. ``None`` for non-triggered sessions.
        mcp_name: the MCP that OWNS this block (i.e. the manifest the
            ``agent_context`` is declared on — not necessarily the same
            MCP the builder calls into).
        agent_name: the session's agent — used for ``pick_account`` to
            resolve which bound account answers this MCP.
        user_sub: session's user_sub. Empty string = agent-scope session.
    """
    builder = block.builder
    if builder is None:
        return None  # defensive — caller should pre-filter

    # 1. Resolve the tool's MCP and validate transport. Defence in depth:
    # the post-load validator already drops stdio-backed builder blocks,
    # but the registry could theoretically be edited at runtime.
    tool_mcp = mcp_registry._resolve_tool_mcp(builder.tool)
    if tool_mcp is None:
        logger.warning(
            "builder on '%s' targets unknown MCP via tool=%r — skipped",
            mcp_name, builder.tool,
        )
        return None
    if tool_mcp.server.transport not in mcp_registry._HTTP_TRANSPORTS:
        logger.warning(
            "builder on '%s' targets stdio MCP %s (tool=%r) — skipped",
            mcp_name, tool_mcp.name, builder.tool,
        )
        return None

    # 2. Build the server URL via the framework's standard resolver. The
    # ``toml`` format hint gives us the streamable-http ``/mcp`` endpoint —
    # the canonical one-shot protocol for builder calls.
    try:
        entry = mcp_registry.resolve_server_config(
            tool_mcp, agent_name, mcp_config_format="toml",
        )
    except Exception as e:
        logger.warning(
            "builder on '%s' resolve_server_config failed for %s: %s",
            mcp_name, tool_mcp.name, e,
        )
        return None

    if entry.get("type") not in _INVOKE_ENTRY_TYPES:
        logger.warning(
            "builder on '%s' resolved %s to non-HTTP entry type %r — skipped",
            mcp_name, tool_mcp.name, entry.get("type"),
        )
        return None

    url = entry.get("url") or ""
    if not url:
        logger.warning(
            "builder on '%s' resolved %s to empty URL — skipped",
            mcp_name, tool_mcp.name,
        )
        return None

    # 3. Inject OAuth bearer header if the target MCP opts in AND the host
    # is allowlisted AND the user has a bound account. No-op for infra-
    # credential MCPs (which carry their own auth in the container's env).
    # ``task_scope`` passed to ``pick_account`` via the helper.
    task_scope = "user" if user_sub else "agent"
    entry = mcp_registry.maybe_inject_bearer_header(
        entry, tool_mcp, user_sub or None, agent_name, task_scope,
    )
    headers = entry.get("headers") or {}

    # 4. Substitute ``${ns.key}`` in the args dict — walks strings inside
    # nested dicts/lists, preserves non-string scalars. Result tokens
    # aren't available yet (this *is* the call that fills them).
    try:
        resolved_args = dynamic_context.substitute_in_json(
            builder.args, tokens, trigger_payload,
        )
    except Exception as e:
        logger.warning(
            "builder on '%s' arg substitution failed: %s", mcp_name, e,
        )
        return None

    # 5. Tool short name = strip ``mcp__<server>__`` prefix. The regex
    # in ``_parse_builder_block`` already validated the shape, so split
    # is safe.
    tool_short = builder.tool.split("__", 2)[2]

    # 6. Open the MCP session and call the tool with a hard timeout.
    # Timeout bounds: 1-30s, enforced at parse time. Phone-call latency
    # budget needs the lower end (≤5s default) — exceed and the caller
    # waits while the user listens to silence.
    try:
        result_text = await _invoke_quarantined(
            url, headers, tool_short, resolved_args,
            timeout=float(builder.timeout_seconds),
        )
    except asyncio.TimeoutError:
        logger.warning(
            "builder on '%s' tool=%s timed out after %ds — skipped",
            mcp_name, builder.tool, builder.timeout_seconds,
        )
        return None
    except Exception as e:
        logger.warning(
            "builder on '%s' tool=%s failed: %s: %s",
            mcp_name, builder.tool, type(e).__name__, e,
        )
        return None

    if not result_text:
        return None

    # 7. Build the ``${result.*}`` token map from the tool's return value.
    result_tokens = _flatten_result(result_text)

    # 8. Re-check `requires` against the combined source+result map.
    # `_resolve_manifest_blocks` already verified source requirements
    # pre-builder; this pass enforces `result.*` requirements (e.g.
    # `requires: ["result.account_name"]` on a CRM-lookup block).
    # Drops blocks where the tool returned an empty result, preventing
    # half-rendered prompts like "Hi , calling about your ".
    combined = {**tokens, **result_tokens}
    if not dynamic_context._requires_ok(
        block.requires, combined, trigger_payload,
    ):
        logger.debug(
            "builder on '%s' tool=%s dropped post-call: requires not met "
            "against combined source+result tokens (empty result?)",
            mcp_name, builder.tool,
        )
        return None

    # 9. Render the template with the merged token map (source + result).
    # ``trigger.body.*`` dot-paths still resolve through ``_substitute_tokens``
    # against the raw payload — single uniform substitution pass.
    rendered = dynamic_context._substitute_tokens(
        block.template, combined, trigger_payload,
    )
    return rendered or None


async def _invoke_quarantined(
    url: str,
    headers: dict[str, str],
    tool_short: str,
    args: dict[str, Any],
    *,
    timeout: float,
) -> str:
    """Run one builder invocation on a private event loop in its own thread.

    Two failure modes of a timed-out ``streamablehttp_client`` motivate
    this. First, cancellation abandons suspended async generators inside
    the SDK/httpx stack; on a shared loop the GC finalizes them later in a
    foreign task context, where the anyio cancel-scope teardown raises
    (cooperative cancellation via ``anyio.fail_after`` leaks the same way —
    empirically verified). Running under ``asyncio.run()`` on a throwaway
    loop finalizes them at loop shutdown instead, before the loop dies.
    Second, the SDK's task-group teardown can wedge mid-cancellation; a
    plain ``asyncio.wait_for`` would await that teardown forever and hang
    the session build.

    The worker is therefore a daemon thread: if teardown wedges, the
    grace-period bound below abandons the thread instead of hanging the
    caller, and a wedged thread can't block process exit.
    """
    loop = asyncio.get_running_loop()
    outcome: asyncio.Future = loop.create_future()

    def _deliver(setter, value) -> None:
        if not outcome.done():
            setter(value)

    def _post(setter, value) -> None:
        # main loop already closed — nothing to deliver to
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_deliver, setter, value)

    def _worker() -> None:
        try:
            result = asyncio.run(
                asyncio.wait_for(
                    _invoke(url, headers, tool_short, args), timeout,
                )
            )
        except BaseException as e:
            _post(outcome.set_exception, e)
        else:
            _post(outcome.set_result, result)

    threading.Thread(
        target=_worker, name="builder-invoke", daemon=True,
    ).start()
    # The in-thread wait_for normally delivers asyncio.TimeoutError itself;
    # this outer bound fires only if teardown wedges inside the SDK.
    return await asyncio.wait_for(outcome, timeout + 5.0)


async def _invoke(
    url: str, headers: dict[str, str], tool_short: str, args: dict[str, Any],
) -> str:
    """Open a streamable-HTTP MCP session and call one tool.

    Runs on the quarantined per-call loop — ``_invoke_quarantined`` applies
    the wall-clock timeout around it. Returns the concatenated text content
    of the result. Errors propagate to the caller (which logs + returns
    None).
    """
    async with streamablehttp_client(
        url, headers=headers or None,
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_short, args)
            # ``isError`` flips True when the tool itself returns an error
            # (vs. raising). Surface as exception so the WARN log path
            # captures it consistently with transport errors.
            if getattr(result, "isError", False):
                preview = ""
                for c in result.content:
                    t = getattr(c, "text", "")
                    if t:
                        preview = t[:200]
                        break
                raise RuntimeError(f"tool reported error: {preview}")
            parts: list[str] = []
            for content in result.content:
                text = getattr(content, "text", None)
                if text:
                    parts.append(text)
            return "\n".join(parts)


def _flatten_result(result_text: str) -> dict[str, str]:
    """Flatten one level of a JSON tool result into ``${result.*}`` tokens.

    Always exposes ``${result.text}`` set to the raw output as a fallback.
    JSON-parseable object results additionally expose one ``${result.<key>}``
    per top-level field; scalar values via ``str()``, dict/list values
    serialised as JSON so they render readably inside the prompt.

    JSON array root → exposes ``${result.length}`` + ``${result.json}``.
    Non-JSON / parse failure → only ``${result.text}``.
    Empty / whitespace → empty dict.
    """
    if not result_text or not result_text.strip():
        return {}
    tokens: dict[str, str] = {"result.text": result_text}
    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        return tokens
    if isinstance(parsed, dict):
        for k, v in parsed.items():
            if isinstance(v, (dict, list)):
                tokens[f"result.{k}"] = json.dumps(v, ensure_ascii=False)
            elif v is None:
                tokens[f"result.{k}"] = ""
            else:
                tokens[f"result.{k}"] = str(v)
    elif isinstance(parsed, list):
        tokens["result.length"] = str(len(parsed))
        tokens["result.json"] = json.dumps(parsed, ensure_ascii=False)
    return tokens
