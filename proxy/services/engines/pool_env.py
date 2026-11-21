"""A spawn's credentials as environment: the relay's minted token and the subscription's env.
"""

from __future__ import annotations

import dataclasses
import logging

from services.engines.pool_acquire import (
    acquire_subscription,
)
from services.engines.pool_bindings import (
    release_unbound_seat,
)
from services.engines.pool_state import (
    _LOCAL_AUTH_TYPES,
    _issued_token_expiry,
    _start_token_expiry,
)
from storage.billing import subscription_store

logger = logging.getLogger(__name__)


def relay_llm_credentials(layer: str, provider: str, user_sub: str | None) -> tuple[str, str] | None:
    """For a hosted (``auth_type='relay'``) subscription on ``layer``: mint a
    per-user relay token and build this provider's relay endpoint URL — the
    engine's ``providers[]`` entry declares the path (the provider SDK
    appends its own route suffix to base_url, so the install-side endpoint
    differs per vendor). Returns ``(api_key, endpoint_url)`` where
    ``api_key`` is the minted token, or ``None`` if the provider has no relay
    path or the relay is unavailable / refuses (out of credit, over seat,
    not configured) — the caller then surfaces a clean "no credentials"
    error."""
    import config as app_config
    from core.execution_layer import provider_entry
    from core.session.session_manager import get_layer_capabilities
    from services.billing import relay_client

    caps = get_layer_capabilities(layer)
    entry = provider_entry(caps, provider) if caps is not None else None
    path = (entry or {}).get("relay_path") or ""
    if not path or not app_config.OTODOCK_RELAY_BASE:
        return None
    try:
        token = relay_client.mint_session_token(user_sub or "")
    except relay_client.RelayNotConfigured as e:
        logger.warning("Hosted LLM unavailable (provider=%s): %s", provider, e)
        return None
    base = app_config.OTODOCK_RELAY_BASE.rstrip("/")
    return token, f"{base}{path}"


def resolve_subscription_env(
    execution_path: str,
    user_sub: str | None,
    model: str = "",
    agent_info: dict | None = None,
    sticky_scope: str = "",
) -> tuple[str, dict[str, str]]:
    """Acquire a subscription and build the session's auth env.

    Provider resolution and acquisition are the pool's; the credential-to-env
    mapping is the ENGINE's (``ExecutionLayer.subscription_env`` — the layer
    is looked up by ``execution_path``, fail-closed on an id no engine
    claims). One call for every config builder (chat, task, meeting, phone).

    ``sticky_scope`` (the spawn's ``credential_scope_key``; the builders pass
    it) pins same-scope sessions to one account — see ``acquire_subscription``
    — and only means anything on an engine whose login is a credential FILE
    shared by the scope's sessions (``auth.credential_file``); an engine that
    injects its credential per session env has nothing on disk to share.

    Returns (subscription_id, env_vars_dict).  On failure returns ("", {}).
    """
    import config as app_config  # local import to avoid circular dependency
    from core.session.session_manager import get_layer_by_path

    layer = get_layer_by_path(execution_path)
    caps = layer.capabilities

    # 1. Resolve the provider from the model on a multi-provider engine (one
    # that declares MORE THAN ONE provider: Codex and Direct LLM carry
    # OpenAI-compatible LOCAL endpoints next to their vendor accounts, and
    # the model decides which one a session needs). A single-provider engine
    # has no filter: every row on it is its vendor, validated at insert, and
    # get_model_provider's prefix guess for an unknown id must never gate a
    # spawn there.
    resolved_provider = (
        app_config.get_model_provider(model, layer=execution_path)
        if len(caps.providers or ()) > 1 and model else ""
    )

    # 2. Acquire a subscription from the pool. An agent that allows only
    # local models on this engine (``agents.local_only_engines``) takes the
    # self-hosted servers' rows alone, whatever the model's provider says:
    # the backstop of the model rule (engine_resolution, resolve_agent_model).
    from storage.agents.agent_store import _get_local_only_engines
    file_delivery = caps.auth.credential_file is not None
    sub_handle = acquire_subscription(
        execution_path, user_sub, provider=resolved_provider,
        sticky_scope=sticky_scope if file_delivery else "",
        model=model,
        allowed_auth=(_LOCAL_AUTH_TYPES
                      if execution_path in _get_local_only_engines(agent_info) else None),
    )
    if not sub_handle:
        return "", {}

    # Everything after the acquire runs under one guard: a failure here
    # (a DB read for the local-model catalog, a relay mint) used to leave
    # the seat taken with no handle for anyone to release — the builder
    # swallowed the exception and reported no subscription.
    try:
        # Stamp the expiry of the token THIS spawn will freeze into its env —
        # bind_session (called by the layer moments later in the same spawn flow)
        # snapshots it per session so the re-warm worker / turn-start guard track
        # the frozen token's real runway, not the store's latest.
        start = (sub_handle.subscription_id, sticky_scope if file_delivery else "")
        if sub_handle.oauth_expires_at_ms:
            _issued_token_expiry[sub_handle.subscription_id] = sub_handle.oauth_expires_at_ms
            if start[1]:
                _start_token_expiry[start] = sub_handle.oauth_expires_at_ms
        else:
            _issued_token_expiry.pop(sub_handle.subscription_id, None)
            _start_token_expiry.pop(start, None)

        # 3. The hosted relay: mint a per-user token + this provider's relay
        # endpoint (the vendor key never reaches the install), and hand them
        # to the engine like any BYO key + endpoint. Fail-soft — if the relay
        # is unavailable / out of credit / over seat, surface no creds (a
        # clean "no LLM credentials" error) and give the pool slot back.
        if sub_handle.auth_type == "relay":
            creds = relay_llm_credentials(sub_handle.layer, sub_handle.provider, user_sub)
            if not creds:
                subscription_store.decrement_active_sessions(sub_handle.subscription_id)
                return "", {}
            sub_handle = dataclasses.replace(
                sub_handle, api_key=creds[0], endpoint_url=creds[1],
            )

        # 4. The engine maps the credential to its own env.
        env = layer.subscription_env(sub_handle)

    except Exception:
        release_unbound_seat(sub_handle.subscription_id, sticky_scope if file_delivery else "")
        raise
    return sub_handle.subscription_id, env
