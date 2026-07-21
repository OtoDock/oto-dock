"""Dynamic prompt context providers for MCPs.

Two complementary mechanisms inject runtime-generated text into the agent
system prompt at session-build time. Both run only for MCPs that are
actually assigned to the agent; both emit into the same
``# MCP Dynamic Context`` section of the prompt.

1. **Python providers** (``register(mcp_name, fn)``) — for iterative or
   computed context that doesn't fit a template (e.g. ``delegation-mcp``
   enumerating delegation-target agents with their descriptions). The
   ``delegation-mcp`` and ``meetings-mcp`` providers below are the canonical
   examples.

2. **Manifest ``agent_context`` blocks** — declared in ``manifest.json`` as
   a list of ``{template, requires?, scope?, builder?}`` objects with
   ``${ns.key}`` token substitution. ``builder`` blocks additionally call
   an HTTP-class MCP tool out-of-band and expose its result via the
   ``${result.*}`` namespace.

Token resolution covers BOTH user-scope sessions (``user_sub`` truthy,
account picked via ``credential_resolver.pick_account`` from
``user_credential_accounts`` + ``agent_account_bindings``) AND
agent-scope sessions (``user_sub`` empty, account picked via the same
``pick_account`` from ``service_agent_bindings`` — which points at a
user's own connected account). Same manifest template works in both
scopes — no per-scope branching needed in MCP authoring.

The ``${trigger.*}`` namespace is fed by ``trigger_payload``
from phone calls (route → trigger lookup at warmup) and webhook trigger
fires. Sessions with no trigger payload resolve every ``trigger.*`` token
to the empty string, so the ``requires`` gate naturally skips
trigger-only blocks for plain chat sessions.

Python provider signature:
    def build_context(agent_name: str, **kwargs) -> str | None
"""

import asyncio
import json
import logging
import re
from typing import Any, Callable

logger = logging.getLogger("claude-proxy")

_providers: dict[str, Callable[..., str | None]] = {}


def register(mcp_name: str, builder: Callable[..., str | None]) -> None:
    """Register a Python dynamic context provider for an MCP."""
    _providers[mcp_name] = builder


# Matches ``${ns.key}`` — the same syntax ``_resolve_template`` uses for
# env-var tokens. Subgroup 1 captures ``ns.key`` for lookup.
_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")


async def get_dynamic_contexts(
    agent_name: str,
    assigned_mcp_names: list[str],
    **kwargs,
) -> list[tuple[str, str]]:
    """Resolve all per-session prompt context for one session.

    For each assigned MCP, run (1) any registered Python provider and
    (2) any manifest ``agent_context`` blocks. Both contribute to the
    final list of ``(mcp_name, markdown_text)`` pairs the prompt
    builder appends.

    ``kwargs`` accepts ``user_sub``, ``user_role``, ``session_ctx``,
    ``delegation_targets``, ``trigger_payload``. All optional — missing
    kwargs mean the corresponding tokens (or Python provider behaviors)
    resolve to empty / no-op rather than erroring.

    Async because manifest builder blocks invoke remote MCP tools.
    Python providers stay sync (no I/O) and are called directly inside
    this coroutine.
    """
    results: list[tuple[str, str]] = []
    user_sub = kwargs.get("user_sub", "") or ""
    user_role = kwargs.get("user_role", "") or ""
    session_ctx = kwargs.get("session_ctx") or {}
    trigger_payload = kwargs.get("trigger_payload") or None

    for mcp_name in assigned_mcp_names:
        # 1. Python provider (iterative / computed context)
        builder = _providers.get(mcp_name)
        if builder:
            try:
                text = builder(agent_name=agent_name, **kwargs)
                if text:
                    results.append((mcp_name, text))
            except Exception as e:
                logger.warning(
                    "Dynamic context provider '%s' failed: %s", mcp_name, e
                )

        # 2. Manifest agent_context blocks (template + optional builder)
        try:
            blocks = await _resolve_manifest_blocks(
                mcp_name, agent_name, user_sub, user_role, session_ctx,
                trigger_payload,
            )
            for text in blocks:
                results.append((mcp_name, text))
        except Exception as e:
            logger.warning(
                "agent_context resolution failed for '%s': %s", mcp_name, e
            )

    return results


# ---------------------------------------------------------------------------
# Manifest-driven agent_context: token map + block evaluator
# ---------------------------------------------------------------------------


# Common field names a trigger payload may use for normalised tokens. Order
# matters — the first non-empty match wins. The fallback also dips into
# ``payload['body']`` so webhook payloads that put the value under the raw
# request body (Stripe, GitHub, etc.) still produce a populated flat token.
_PHONE_KEYS = ("phone", "caller_id", "from", "from_number", "callerid")
_EMAIL_KEYS = ("email", "from_email", "sender")


def _pick_first(payload: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty value from ``payload`` whose key is in ``keys``.

    Also dips into ``payload['body']`` as a fallback so webhook payloads with
    the value nested under the raw body don't need a manifest tweak.
    """
    for k in keys:
        v = payload.get(k)
        if v:
            return str(v)
    body = payload.get("body")
    if isinstance(body, dict):
        for k in keys:
            v = body.get(k)
            if v:
                return str(v)
    return ""


def _build_trigger_tokens(payload: dict | None) -> dict[str, str]:
    """Build the ``${trigger.*}`` token map (flat normalised fields only).

    Raw body dot-path access (``${trigger.body.<dot.path>}``) is handled by
    ``_substitute_tokens`` against the payload directly — those paths are
    open-ended, so precomputing them isn't possible.

    Empty payload / no payload → empty dict → every ``trigger.*`` token
    resolves to the empty string (same soft-empty contract as 2.5a tokens).

    Vendor-event tokens are added for webhook-dispatcher fires
    (``${trigger.event_type}``, ``${trigger.actor.id}``, etc.). Phone
    fires leave those empty; vendor fires leave ``trigger.phone`` /
    ``trigger.email`` empty unless the payload happens to include them.
    Single payload shape — no branching downstream.
    """
    if not payload:
        return {}
    # Phone + generic-webhook tokens.
    out = {
        "trigger.source": str(payload.get("source") or ""),
        "trigger.route": str(payload.get("route") or ""),
        "trigger.phone": _pick_first(payload, _PHONE_KEYS),
        "trigger.email": _pick_first(payload, _EMAIL_KEYS),
        "trigger.did": str(payload.get("did") or ""),
    }
    # Vendor-event tokens.
    out["trigger.event_type"] = str(payload.get("event_type") or "")
    out["trigger.vendor_event_id"] = str(payload.get("vendor_event_id") or "")
    out["trigger.provider_id"] = str(payload.get("provider_id") or "")
    out["trigger.subscription_id"] = str(payload.get("subscription_id") or "")
    actor = payload.get("actor") if isinstance(payload.get("actor"), dict) else {}
    subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    for ns, src in (("actor", actor), ("subject", subject), ("target", target)):
        for key in ("id", "email", "name", "url", "type", "title"):
            v = src.get(key) if isinstance(src, dict) else None
            out[f"trigger.{ns}.{key}"] = str(v) if v is not None else ""
    return out


def _walk_body_path(payload: dict | None, path: str) -> str:
    """Walk a dot-path through ``payload['body']`` for raw token access.

    Returns the empty string on any miss (path doesn't exist, intermediate
    isn't a dict, value is None). Nested dicts / lists are JSON-serialised
    so they render readably inside the prompt.
    """
    if not payload:
        return ""
    body = payload.get("body")
    if not isinstance(body, dict):
        return ""
    current: Any = body
    for part in path.split("."):
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
        if current is None:
            return ""
    if isinstance(current, (dict, list)):
        return json.dumps(current, ensure_ascii=False)
    return str(current)


def _build_token_map(
    mcp_name: str,
    agent_name: str,
    user_sub: str,
    user_role: str,
    session_ctx: dict[str, str],
    trigger_payload: dict | None = None,
) -> dict[str, str]:
    """Build the ``${ns.key} → value`` map for one (mcp, session).

    Both scopes resolve ``account.*`` + ``credential.*`` via
    ``credential_resolver.pick_account`` — user scope reads the user's
    bound account, agent scope reads the per-agent binding (a user's own
    account a manager designated as the agent's service identity).
    ``user.*`` is only populated in user scope.

    Always populates ``agent.*`` (from ``agent_store``) and
    ``session.*`` (passthrough from ``session_ctx``). ``user.role``
    comes from the ``user_role`` kwarg regardless of scope.

    ``trigger.*`` flat tokens populated from ``trigger_payload``
    when supplied; raw body access (``trigger.body.*``) is resolved on
    demand in ``_substitute_tokens`` rather than precomputed.

    Tokens that cannot be resolved are simply absent from the map. The
    substitution loop renders absent tokens as the empty string.
    """
    from storage import agent_store
    from storage import credential_store
    from storage import database as task_store
    from services.oauth import credential_resolver

    tokens: dict[str, str] = {}

    # ----- agent.* (always populated) -----
    agent_data = agent_store.get_agent(agent_name) or {}
    tokens["agent.name"] = agent_name
    tokens["agent.display_name"] = str(
        agent_data.get("display_name") or agent_name
    )
    tokens["agent.description"] = str(agent_data.get("description") or "")
    tokens["agent.color"] = str(agent_data.get("color") or "")

    # ----- user.role (kwarg) + session.* (passthrough) -----
    tokens["user.role"] = user_role or ""
    for key in ("task_owner", "task_username", "chat_id"):
        tokens[f"session.{key}"] = str(session_ctx.get(key) or "")

    # ----- trigger.* (normalised flat fields from payload) -----
    tokens.update(_build_trigger_tokens(trigger_payload))

    # ----- account.*, credential.*, user.* (scope-branched) -----
    # Look up provider_id once; needed for `account.extra.*` token-file read.
    from services.mcp import mcp_registry as _mcp_registry
    _manifest = _mcp_registry.get_manifest(mcp_name)
    _oauth = (_manifest.credentials.oauth if _manifest else None) or None
    _provider_id = _oauth.get("provider_id", "") if _oauth else ""

    if user_sub:
        # User scope: bound user account.
        ref = credential_resolver.pick_account(
            mcp_name, agent_name, user_sub=user_sub,
        )
        if ref is not None:
            account_label = ref.label
            tokens["account.label"] = account_label
            accounts = credential_store.list_user_accounts(user_sub, mcp_name)
            match = next(
                (a for a in accounts if a["account_label"] == account_label),
                None,
            )
            display_email = (match or {}).get("display_email") or ""
            tokens["account.email"] = display_email or account_label

            try:
                creds = credential_store.get_user_credentials(
                    user_sub, mcp_name, account_label,
                )
                for k, v in creds.items():
                    tokens[f"credential.{k}"] = str(v or "")
            except Exception as e:
                logger.warning(
                    "Failed to load user credentials for token map "
                    "(mcp=%s user=%s account=%s): %s",
                    mcp_name, user_sub[:8], account_label, e,
                )

            # account.extra.* — vendor metadata persisted in the token file
            # (Slack team_id, Microsoft tenant_id, Zoom account_id, etc.).
            # Only available for OAuth MCPs using the generic_oauth_v1 schema.
            _populate_account_extras(
                tokens, mcp_name, _provider_id, account_label,
                user_sub=user_sub,
            )

        user_row = task_store.get_user(user_sub) or {}
        tokens["user.email"] = str(user_row.get("email") or "")
        tokens["user.name"] = str(
            user_row.get("name") or user_row.get("display_name") or ""
        )
    else:
        # Agent scope: the binding points at a user's own account (a manager
        # designated it as the agent's service identity). owner_sub is always
        # a real user_sub — read their user_credentials directly.
        ref = credential_resolver.pick_account(mcp_name, agent_name)
        if ref is not None:
            account_label = ref.label
            tokens["account.label"] = account_label

            accounts = credential_store.list_user_accounts(
                ref.owner_sub, mcp_name,
            )
            match = next(
                (a for a in accounts if a["account_label"] == account_label),
                None,
            )
            display_email = (match or {}).get("display_email") or ""
            try:
                svc = credential_store.get_user_credentials(
                    ref.owner_sub, mcp_name, account_label,
                )
            except Exception as e:
                logger.warning(
                    "Failed to load bound user credentials for token map "
                    "(mcp=%s account=%s user=%s): %s",
                    mcp_name, account_label, ref.owner_sub[:8], e,
                )
                svc = {}

            # Preferred email: display_email > GOOGLE_EMAIL > first email-shaped
            # credential > account_label as fallback identifier.
            email = (
                display_email
                or svc.get("GOOGLE_EMAIL")
                or _first_email_value(svc)
                or account_label
            )
            tokens["account.email"] = email
            for k, v in svc.items():
                tokens[f"credential.{k}"] = str(v or "")

            _populate_account_extras(
                tokens, mcp_name, _provider_id, account_label,
                user_sub=ref.owner_sub,
            )
        # user.email / user.name stay absent for agent-scope.

    return tokens


def _populate_account_extras(
    tokens: dict[str, str],
    mcp_name: str,
    provider_id: str,
    account_label: str,
    *,
    user_sub: str,
) -> None:
    """Read ``extra.*`` keys from the bound account's token file into the
    ``${account.extra.<key>}`` namespace.

    Reads ``{provider}-tokens/{username}/{label}.json`` for the account owner
    (``user_sub``) — for agent-scope sessions the caller passes the binding's
    owner sub.

    Best-effort: silently does nothing for non-OAuth MCPs or missing
    token files. If the token file has no ``extra`` block, no tokens
    get added — the ``requires`` gate on the manifest's ``agent_context``
    block then skips templates that depend on those fields.
    """
    if not provider_id:
        return
    try:
        from services.oauth import oauth_account_store
        from storage import database as task_store
        username = task_store.get_username_by_sub(user_sub) if user_sub else ""
        if not username:
            return
        token_dir = oauth_account_store.get_token_dir(
            username, provider_id=provider_id,
        )
        token_data = oauth_account_store.read_account_token(token_dir, account_label)
        if not token_data:
            return
        extra = token_data.get("extra") or {}
        if not isinstance(extra, dict):
            return
        for ek, ev in extra.items():
            tokens[f"account.extra.{ek}"] = str(ev or "")
    except Exception as e:
        logger.debug(
            "account.extra.* population skipped (mcp=%s account=%s): %s",
            mcp_name, account_label, e,
        )


def _first_email_value(creds: dict[str, str]) -> str:
    """Best-effort identifier lookup for non-Google service accounts.

    Returns the first value in ``creds`` whose key resembles an email
    field (USERNAME / EMAIL / USER), or empty string. Only used by the
    agent-scope branch when GOOGLE_EMAIL isn't present.
    """
    for key in ("EMAIL_USER", "NEXTCLOUD_USER", "NEXTCLOUD_USERNAME", "USERNAME", "USER"):
        v = creds.get(key)
        if v:
            return str(v)
    return ""


def _substitute_tokens(
    template: str,
    tokens: dict[str, str],
    trigger_payload: dict | None = None,
) -> str:
    """Replace every ``${ns.key}`` in ``template`` with ``tokens[ns.key]``.

    Tokens not present in the map render as the empty string — matches
    the soft-empty behavior the ``requires`` semantics rely on.

    ``${trigger.body.<dot.path>}`` tokens fall through to a
    direct walk of ``trigger_payload['body']`` so manifests can read raw
    webhook fields without an entry in the flat token map.
    """
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key in tokens:
            return tokens[key]
        if key.startswith("trigger.body.") and trigger_payload is not None:
            return _walk_body_path(trigger_payload, key[len("trigger.body."):])
        return ""
    return _TOKEN_RE.sub(replace, template)


def substitute_in_json(
    value: Any,
    tokens: dict[str, str],
    trigger_payload: dict | None = None,
) -> Any:
    """Recursively substitute ``${ns.key}`` in string leaves of a JSON value.

    Used by ``builder_executor`` to prepare ``builder.args`` for the MCP
    tool call — the JSON structure is preserved; only string leaves get
    template substitution.
    """
    if isinstance(value, str):
        return _substitute_tokens(value, tokens, trigger_payload)
    if isinstance(value, dict):
        return {
            k: substitute_in_json(v, tokens, trigger_payload)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [substitute_in_json(v, tokens, trigger_payload) for v in value]
    return value


def _requires_ok(requires: list[str], tokens: dict[str, str],
                 trigger_payload: dict | None) -> bool:
    """True iff every token in ``requires`` resolves to a non-empty string.

    Looks first in the flat ``tokens`` map; for ``trigger.body.<path>``
    entries, walks ``trigger_payload['body']`` directly. Same lookup
    semantics as ``_substitute_tokens`` so the gate matches what the
    template would render.
    """
    for req in requires:
        if tokens.get(req):
            continue
        if req.startswith("trigger.body.") and trigger_payload is not None:
            v = _walk_body_path(trigger_payload, req[len("trigger.body."):])
            if v:
                continue
        return False
    return True


async def _resolve_manifest_blocks(
    mcp_name: str,
    agent_name: str,
    user_sub: str,
    user_role: str,
    session_ctx: dict[str, str],
    trigger_payload: dict | None = None,
) -> list[str]:
    """Render the ``agent_context`` blocks for one assigned MCP.

    Returns an ordered list of fully-substituted markdown strings, one per
    block that passes the ``scope`` filter AND has all its ``requires``
    tokens resolved. Skipped blocks produce no output (no half-prompts).

    Template-only blocks render synchronously. Builder blocks are scheduled
    for parallel evaluation via ``asyncio.gather`` so total latency is
    ``max(per-block timeouts)`` rather than the sum.
    """
    from services.mcp import mcp_registry
    from services.mcp import builder_executor

    manifest = mcp_registry.get_manifest(mcp_name)
    if not manifest or not manifest.agent_context:
        return []

    current_scope = "user" if user_sub else "agent"
    tokens: dict[str, str] | None = None  # lazy — only build if needed

    # Pass 1: filter + render template-only blocks; collect builder coros.
    results: dict[int, str] = {}
    builder_jobs: list[tuple[int, asyncio.Future]] = []

    for idx, block in enumerate(manifest.agent_context):
        if block.scope and current_scope not in block.scope:
            continue

        if tokens is None:
            tokens = _build_token_map(
                mcp_name, agent_name, user_sub, user_role, session_ctx,
                trigger_payload,
            )

        # Pre-builder gate: only check source-token requirements
        # (``result.*`` tokens aren't populated until after the builder
        # runs — they're re-checked inside ``execute_builder`` against
        # the combined source+result map). For template-only blocks
        # this is the only gate; ``result.*`` requirements are
        # meaningless without a builder anyway.
        pre_requires = (
            [r for r in block.requires if not r.startswith("result.")]
            if block.builder is not None
            else block.requires
        )
        if not _requires_ok(pre_requires, tokens, trigger_payload):
            continue

        if block.builder is None:
            text = _substitute_tokens(block.template, tokens, trigger_payload)
            if text:
                results[idx] = text
            continue

        # Schedule builder evaluation — runs in parallel with other builders.
        coro = builder_executor.execute_builder(
            block=block,
            tokens=tokens,
            trigger_payload=trigger_payload,
            mcp_name=mcp_name,
            agent_name=agent_name,
            user_sub=user_sub,
        )
        builder_jobs.append((idx, asyncio.ensure_future(coro)))

    # Pass 2: await builders in parallel. ``return_exceptions=True`` keeps
    # one bad block from sinking the rest — execute_builder is supposed to
    # catch internally, but this is defence in depth.
    if builder_jobs:
        idxs = [j[0] for j in builder_jobs]
        coros = [j[1] for j in builder_jobs]
        builder_results = await asyncio.gather(*coros, return_exceptions=True)
        for idx, result in zip(idxs, builder_results):
            if isinstance(result, Exception):
                logger.warning(
                    "builder block %d on '%s' raised %s: %s",
                    idx, mcp_name, type(result).__name__, result,
                )
                continue
            if result:
                results[idx] = result

    # Return rendered blocks in original manifest order.
    return [results[i] for i in sorted(results)]


# ---------------------------------------------------------------------------
# Built-in Python providers (iterative logic that doesn't fit templates)
# ---------------------------------------------------------------------------

# Cap on per-layer model ids rendered into the delegation roster — the full
# catalog belongs in the dashboard picker, not every system prompt.
_ROSTER_MODEL_CAP = 8


def build_delegation_roster(
    delegation_targets: list[str],
) -> dict[str, list[dict]]:
    """Resolve each target agent's enabled execution layers + models.

    Returns ``{slug: [{path, is_default, local_only, default_model, models,
    more}, …]}`` — the data ``_delegation_mcp_context`` renders so a
    "delegate this on Opus / on codex" request can be validated and chosen
    instead of delegating blind. Sources match the spawn validator
    (``spawn_authz.validate_spawn_overrides``): ``_get_execution_paths`` for
    layers, enabled ``list_models`` rows per layer (deduped — one query per
    distinct layer, not per agent), ``resolve_agent_model`` for the default —
    so the advertised set is exactly what a spawn override will pass.

    Sync DB reads — call via ``asyncio.to_thread`` from the config builders
    and pass the result through the ``delegation_roster`` kwarg; NEVER call
    from a context provider (providers are no-I/O by contract).
    """
    import config as app_config
    from api.agents._common import _get_execution_paths
    from storage import agent_store, subscription_store

    models_by_layer: dict[str, list[str]] = {}

    def _layer_models(path: str) -> list[str]:
        if path not in models_by_layer:
            try:
                models_by_layer[path] = [
                    m["model_id"]
                    for m in subscription_store.list_models(path)
                    if m.get("enabled") and m.get("model_id")
                ]
            except Exception:
                models_by_layer[path] = []
        return models_by_layer[path]

    roster: dict[str, list[dict]] = {}
    for slug in delegation_targets:
        data = agent_store.get_agent(slug)
        if not data:
            continue
        try:
            default_model = app_config.resolve_agent_model(slug)
        except Exception:
            default_model = ""
        layers: list[dict] = []
        for i, path in enumerate(_get_execution_paths(data)):
            models = _layer_models(path)
            # Default first so the cap can never hide it.
            if default_model in models:
                models = [default_model] + [
                    m for m in models if m != default_model
                ]
            layers.append({
                "path": path,
                "is_default": i == 0,
                "local_only": path == "direct-llm",
                "default_model": default_model,
                "models": models[:_ROSTER_MODEL_CAP],
                "more": max(0, len(models) - _ROSTER_MODEL_CAP),
            })
        roster[slug] = layers
    return roster


def _fmt_roster_layers(layers: list[dict]) -> str:
    """One compact `layers:` line for an agent's roster entry."""
    rendered = []
    for layer in layers:
        parts = []
        if layer["is_default"]:
            parts.append("default")
        if layer["local_only"]:
            parts.append("local only")
        if layer["models"]:
            shown = ", ".join(
                f"{m} [default]" if m == layer["default_model"] else m
                for m in layer["models"]
            )
            if layer["more"]:
                shown += f", +{layer['more']} more"
            parts.append(f"models: {shown}")
        rendered.append(
            f"{layer['path']} ({' | '.join(parts)})" if parts
            else layer["path"]
        )
    return " · ".join(rendered)


def _delegation_mcp_context(
    agent_name: str,
    delegation_targets: list[str] | None = None,
    **kwargs: Any,
) -> str | None:
    """Inject available agents section into the prompt.

    Lists self + delegation targets with descriptions, plus each agent's
    enabled execution layers/models when the caller pre-resolved them
    (``delegation_roster`` kwarg — built off-loop by the config builders via
    ``build_delegation_roster``; this provider itself stays no-I/O beyond
    the pre-existing agent-row reads). This section is shared context — also
    used by meetings-mcp if enabled. A session-start "active parallel
    sessions" block rides along — it covers layers with no per-turn prelude
    injection (PTY, remote) on their first turn.
    """
    sibling_block = ""
    from core.session import sibling_awareness
    block = sibling_awareness.context_block(
        agent_name, kwargs.get("user_sub", "") or "")
    if block:
        sibling_block = block

    if not delegation_targets:
        return sibling_block or None

    from storage import agent_store

    roster: dict[str, list[dict]] = kwargs.get("delegation_roster") or {}

    def _agent_lines(slug: str, data: dict, *, is_self: bool) -> list[str]:
        name = data.get("display_name", slug)
        desc = data.get("description", "")
        tag = " *(this is you)*" if is_self else ""
        out = [f"- **{name}** (`{slug}`){tag}{f' — {desc}' if desc else ''}"]
        layers = roster.get(slug) or []
        if layers:
            out.append(f"  layers: {_fmt_roster_layers(layers)}")
        return out

    # Build agent roster: self first, then targets
    lines = [
        "## Available Agents\n",
        "The following agents are available for cross-agent collaboration:\n",
    ]

    # Self
    self_data = agent_store.get_agent(agent_name)
    if self_data:
        lines.extend(_agent_lines(agent_name, self_data, is_self=True))

    # Delegation targets (excluding self if present)
    for slug in delegation_targets:
        if slug == agent_name:
            continue
        data = agent_store.get_agent(slug)
        if data:
            lines.extend(_agent_lines(slug, data, is_self=False))

    lines.append("")
    lines.append(
        "**Delegation**: Use `delegate(agent=\"...\", surface=...)` to delegate work to another agent. "
        "Use `continue_id` with a prior task/chat id for multi-turn conversations."
    )
    if roster:
        lines.append(
            "Model/layer overrides on `delegate()` must come from the "
            "target's `layers:` line above; omit both to inherit the "
            "worker's defaults (recommended). Ignored with `continue_id`."
        )
    if sibling_block:
        lines.extend(["", sibling_block])
    return "\n".join(lines)


register("delegation-mcp", _delegation_mcp_context)


def _meetings_mcp_context(
    agent_name: str,
    delegation_targets: list[str] | None = None,
    **kwargs: Any,
) -> str | None:
    """Inject meeting capability note when meetings-mcp is assigned."""
    if not delegation_targets:
        return None

    from storage import agent_store as _agent_store

    peer_agents = [t for t in delegation_targets if t != agent_name]
    if not peer_agents:
        return None

    peer_names = []
    for slug in peer_agents:
        data = _agent_store.get_agent(slug)
        name = (data or {}).get("display_name", slug)
        peer_names.append(f"{name} (`{slug}`)")

    lines = [
        "## Meeting Rooms\n",
        "You can start multi-agent meetings using `start_meeting(topic, agents)`.",
        f"Available meeting participants: {', '.join(peer_names)}",
        "",
        "Use meetings for collaborative discussions when multiple perspectives are needed.",
    ]
    return "\n".join(lines)


register("meetings-mcp", _meetings_mcp_context)


def _ssh_hosts_context(
    agent_name: str,
    is_remote: bool = False,
    target_admin_paired: bool = False,
    target_os: str = "",
    **kwargs: Any,
) -> str | None:
    """Inject the authorized SSH host list for the ssh-hosts MCP.

    ssh-hosts is context-only: agents use plain ``ssh``/``scp``/``rsync``
    from bash against admin-configured hosts. Each authorizing instance
    renders as a ready-to-run command line; the referenced private keys are
    materialized per session at ``$OTO_SSH_KEY_DIR`` — locally by
    ``session_config_dir.materialize_ssh_keys_for_sandbox``, on admin-paired
    satellites via the session-file broker. Any other remote target gets
    nothing (``build_session_mcp_config`` excludes the MCP there with a
    visible reason — infra key material never reaches user-paired machines).
    """
    if is_remote and not target_admin_paired:
        return None

    from storage import mcp_store

    instances = mcp_store.get_mcp_instances_for_agent("ssh-hosts", agent_name)
    if not instances:
        return None

    # Connection multiplexing: agent sessions burst short ssh commands, and
    # each fresh TCP connect to :22 looks like a scan to IDS gateways
    # (Suricata ET SCAN 2001219 killed a legit flow mid-command, 2026-07-06).
    # ControlMaster reuses one authenticated connection; ControlPersist keeps
    # the master ≤60s past last use so nothing authenticated outlives session
    # teardown by much. Windows OpenSSH has no unix-socket mux — omit there,
    # and omit when the satellite predates the os capability ("" = unknown,
    # conservative).
    #
    # Socket path: NOT under $OTO_SSH_KEY_DIR — on satellites that dir nests
    # in the session-secrets tree and `cm-%C` (40-hex) overflowed the 108-byte
    # sun_path limit (ssh exits 255 before connecting; hit live 2026-07-11).
    # The shell expands the chain to the OS's short PRIVATE runtime dir:
    # XDG_RUNTIME_DIR (/run/user/<uid>, 0700, systemd Linux) → TMPDIR
    # (per-user 0700 /var/folders/… on macOS) → /tmp (inside the local
    # sandbox /tmp is mount-namespaced private; the chain only lands on a
    # shared /tmp for exotic non-systemd admin-paired hosts).
    mux_capable = (not is_remote) or (target_os or "").lower() in ("linux", "darwin")

    lines = [
        "## SSH Hosts\n",
        "You have direct SSH access to the following admin-configured hosts "
        "from your shell. The referenced private keys are already provisioned "
        "at `$OTO_SSH_KEY_DIR` (mode 0600) — use standard `ssh` / `scp` / "
        "`rsync` commands. The `StrictHostKeyChecking=accept-new` option makes "
        "the first connect record the host key automatically (later connects "
        "verify against it) — reuse it on `scp`/`rsync` too."
        + (" The ControlMaster options multiplex repeated commands over one "
           "authenticated connection — reuse them too so command bursts don't "
           "open a new TCP connection each time." if mux_capable else "")
        + "\n",
    ]
    for inst in instances:
        fv = inst.get("field_values", {}) or {}
        command = format_ssh_host_command(fv, mux=mux_capable)
        if command is None:
            continue
        name = (fv.get("name") or "").strip() or (fv.get("host") or "").strip()
        lines.append(f"- **{name}** — `{command}`")
    if len(lines) == 2:
        return None  # every instance was missing its host
    # The block above is static prompt text rendered once at session build —
    # long sessions lose it to attention decay, so point at the queryable twin.
    lines.append(
        "\nIf you are no longer sure which hosts or keys are available, call "
        "the `list_ssh_hosts` tool — it returns this same list on demand."
    )
    return "\n".join(lines)


register("ssh-hosts", _ssh_hosts_context)


_SSH_MUX_OPTS = (
    " -o ControlMaster=auto"
    ' -o ControlPath="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/oto-cm-%C"'
    " -o ControlPersist=60s"
)


def format_ssh_host_command(field_values: dict, *, mux: bool) -> str | None:
    """One authorized ssh-hosts instance → its ready-to-run ssh command.

    Single source for the SSH Hosts prompt block above AND the
    ``GET /v1/agents/{name}/ssh-hosts`` endpoint behind the ``list_ssh_hosts``
    tool — the two surfaces must never drift. Returns ``None`` when the
    instance has no host. ``mux`` gates the ControlMaster options (see the
    provider comment: no unix-socket mux on Windows / unknown targets).
    """
    fv = field_values or {}
    host = (fv.get("host") or "").strip()
    if not host:
        return None
    username = (fv.get("username") or "").strip()
    port = str(fv.get("port") or "22").strip() or "22"
    key_name = (fv.get("key_name") or "").strip()
    target = f"{username}@{host}" if username else host
    key_part = f' -i "$OTO_SSH_KEY_DIR/{key_name}"' if key_name else ""
    mux_part = _SSH_MUX_OPTS if mux else ""
    # accept-new: without it the first connect dies on ssh's TOFU check in
    # a non-interactive shell ("Host key verification failed"). Hosts are
    # admin-configured and often reachable only from the machine the
    # session runs on, so a platform-side pre-scan can't replace this
    # (deliberate: reachability from the proxy is NOT assumed).
    return (
        f"ssh{key_part} -o StrictHostKeyChecking=accept-new"
        f"{mux_part} -p {port} {target}"
    )

# memory-mcp has no dynamic_context provider — memory reaches the prompt
# via the dedicated # Memory sections (``config._render_memory_sections``:
# topic files inline under the budget, generated index past it), so a
# session restart picks up new entries via the normal prompt build.
