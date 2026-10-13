"""LLM backend factory for the phone pipeline.

Creates the appropriate LLM client based on the PhoneRoute.

Both modes use ProxyClient — the proxy handles everything:
- proxy mode: CLI subprocess with MCP servers
- direct mode: Anthropic API with MCP servers (lower latency)
"""

import logging

from config_manager import PhoneRoute
from proxy.client import ProxyClient

logger = logging.getLogger("llm_factory")


async def create_llm_backend(
    route: PhoneRoute,
    call_type: str = "inbound",
    *,
    audiosocket_uuid: str = "",
    caller_phone: str = "",
    caller_did: str = "",
    dial_event: dict | None = None,
    pin_verified: bool = False,
    attach: dict | None = None,
    transfer_available: bool = True,
) -> ProxyClient:
    """Create the LLM backend for a phone route.

    Both proxy and direct modes go through ProxyClient. The proxy
    handles MCP tools, message history, and Anthropic API calls.

    ``phone_route_id`` is sourced from ``route.id`` directly so callers
    don't have to plumb it separately. Works for both inbound and outbound.

    Establishes a persistent WebSocket connection to the proxy for
    low-overhead communication across the entire call duration. A
    connection that fails marks the client ``proxy_lost``: the pipeline
    ends the call with the apology phrase.

    The caller-metadata kwargs (``audiosocket_uuid``, ``caller_phone``,
    ``caller_did``, ``dial_event``) are forwarded into the proxy's warmup
    message so it can resolve the route → trigger linkage and build a
    ``${trigger.*}`` payload enriching manifest ``agent_context`` blocks.
    Empty defaults mean "no enrichment" — the call proceeds with the base
    prompt. ``pin_verified`` is the PIN gate's result (inbound routes with a
    PIN) — it rides the warmup so the proxy can trust the caller's identity.
    """
    override = route.phone_context_override or ""
    common_kwargs = dict(
        model=route.agent,
        call_type=call_type,
        phone_context_override=override,
        audiosocket_uuid=audiosocket_uuid,
        phone_route_id=route.id or "",
        caller_phone=caller_phone,
        caller_did=caller_did,
        dial_event=dial_event,
        pin_verified=pin_verified,
        attach=attach,
        transfer_available=transfer_available,
    )
    if route.llm_mode == "direct":
        client = ProxyClient(
            llm_mode="direct",
            phone_mode=True,
            **common_kwargs,
        )
    else:
        client = ProxyClient(**common_kwargs)

    ws_ok = await client.connect()
    logger.info(
        f"Created ProxyClient ({route.llm_mode} mode) for agent='{route.agent}' "
        f"({'connected' if ws_ok else 'the proxy is unreachable'}, "
        f"trigger_payload={'yes' if audiosocket_uuid else 'no'})"
    )
    return client
