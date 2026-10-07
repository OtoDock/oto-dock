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
from auth import roles
from core.placement import LOCAL_PLACEMENT, PlacementCapabilities
from core import host_os
from services.infra import external_data

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
    ``delegation_targets``, ``trigger_payload``, ``delegation_roster``,
    ``meetings_access``, ``nouser_reads``. All optional — missing
    kwargs mean the corresponding tokens (or Python provider behaviors)
    resolve to empty / no-op rather than erroring. Providers additionally
    receive ``assigned_mcps`` (this session's full assigned list) so a block
    can defer to another MCP's block instead of duplicating it.

    Async because manifest builder blocks invoke remote MCP tools.
    Python providers stay sync (no I/O) and are called directly inside
    this coroutine.
    """
    results: list[tuple[str, str]] = []
    user_sub = kwargs.get("user_sub", "") or ""
    user_role = kwargs.get("user_role", "") or ""
    session_ctx = kwargs.get("session_ctx") or {}
    trigger_payload = kwargs.get("trigger_payload") or None
    # The delegation provider's department line reads the department row
    # (and the agent cache, cold after an invalidation): resolved here, off
    # the loop, and handed to the provider, which then reads nothing.
    if "department" not in kwargs and any(
            _providers.get(n) is _delegation_mcp_context for n in assigned_mcp_names):
        kwargs["department"] = await asyncio.to_thread(_department_data, agent_name)

    for mcp_name in assigned_mcp_names:
        # 1. Python provider (iterative / computed context)
        builder = _providers.get(mcp_name)
        if builder:
            try:
                text = builder(
                    agent_name=agent_name,
                    # A provider may need to know what ELSE is assigned — the
                    # schedules block defers its model list to the delegation
                    # block when both are present, instead of printing the
                    # same layers line twice.
                    assigned_mcps=assigned_mcp_names,
                    **kwargs,
                )
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

    Both scopes resolve ``account.*`` via ``credential_resolver.pick_account``
    — user scope reads the user's bound account, agent scope reads the
    per-agent binding (a user's own account a manager designated as the
    agent's service identity). No credential value enters the map: a block
    renders into the system prompt, where a secret must never land.
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
    from storage.agents import agent_store
    from storage.identity import credential_store
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

    # ----- account.*, user.* (scope-branched) -----
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
    *,
    fence: bool = True,
) -> str:
    """Replace every ``${ns.key}`` in ``template`` with ``tokens[ns.key]``.

    Tokens not present in the map render as the empty string — matches
    the soft-empty behavior the ``requires`` semantics rely on.

    ``${trigger.body.<dot.path>}`` tokens fall through to a
    direct walk of ``trigger_payload['body']`` so manifests can read raw
    webhook fields without an entry in the flat token map.

    The rendered text is a prompt: every ``trigger.*`` value (what a
    caller or a webhook sender supplied) is fenced, and so is every
    ``result.*`` value when a trigger fired the session (the builder's
    tool ran on the sender's values), and the block says what the fence
    means (``services/infra/external_data.py``). ``fence=False`` is for
    tool arguments (``substitute_in_json``), never a prompt.
    """
    outside = ("trigger.", "result.") if trigger_payload is not None else ("trigger.",)
    fenced = False

    def replace(match: re.Match) -> str:
        nonlocal fenced
        key = match.group(1)
        if key in tokens:
            value = tokens[key]
        elif key.startswith("trigger.body.") and trigger_payload is not None:
            value = _walk_body_path(trigger_payload, key[len("trigger.body."):])
        else:
            return ""
        if fence and value and key.startswith(outside):
            fenced = True
            return external_data.fence(value)
        return value

    out = _TOKEN_RE.sub(replace, template)
    return external_data.with_note(out) if fenced else out


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
        return _substitute_tokens(value, tokens, trigger_payload, fence=False)
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
    from storage.agents import agent_store
    from storage.billing import subscription_store

    models_by_layer: dict[str, list[dict]] = {}

    def _layer_models(path: str) -> list[dict]:
        # Tier order (untiered last), then registry order: the line an
        # agent reads is the ranking it must not get backwards.
        if path not in models_by_layer:
            try:
                rows = [
                    m for m in subscription_store.list_models(path)
                    if m.get("enabled") and m.get("model_id")
                ]
                rows.sort(key=app_config.model_catalog_sort_key)
                models_by_layer[path] = [{
                    "model_id": m["model_id"],
                    "tier": m.get("tier"),
                    "tier_label": app_config.MODEL_TIER_LABELS.get(m.get("tier") or 0, ""),
                    "good_at": m.get("good_at") or "",
                } for m in rows]
            except Exception:
                models_by_layer[path] = []
        return models_by_layer[path]

    roster: dict[str, list[dict]] = {}
    for slug in delegation_targets:
        data = agent_store.get_agent(slug)
        if not data:
            continue
        layers: list[dict] = []
        for i, path in enumerate(_get_execution_paths(data)):
            # Per path: the agent default is honoured only where that
            # layer serves it, else the layer's own first choice (the same
            # rule the task runner applies), so a non-default layer never
            # shows no default at all.
            try:
                default_model = app_config.resolve_agent_model(slug, layer=path)
            except Exception:
                default_model = ""
            catalog = _layer_models(path)
            models = [m["model_id"] for m in catalog]
            # Default first so the cap can never hide it.
            if default_model in models:
                models = [default_model] + [
                    m for m in models if m != default_model
                ]
            layers.append({
                "path": path,
                "is_default": i == 0,
                "local_only": not _supports_remote(path),
                "default_model": default_model,
                "models": models[:_ROSTER_MODEL_CAP],
                "more": max(0, len(models) - _ROSTER_MODEL_CAP),
                "tiers": {m["model_id"]: m["tier"] for m in catalog},
                "catalog": catalog,
            })
        roster[slug] = layers
    return roster


def _tier_tag(tier) -> str:
    return f"t{int(tier)}" if tier else "t?"


def _fmt_model_tiers(roster: dict[str, list[dict]]) -> list[str]:
    """The tier legend rendered ONCE per prompt: every model any roster
    layer offers, grouped by tier, with its one-line "good at"."""
    import config as app_config
    seen: dict[str, dict] = {}
    for layers in roster.values():
        for layer in layers:
            for m in layer.get("catalog") or []:
                seen.setdefault(m["model_id"], m)
    if not seen:
        return []
    by_tier: dict[int | None, list[dict]] = {}
    for m in seen.values():
        by_tier.setdefault(m.get("tier") or None, []).append(m)
    lines = [
        "",
        "**Model tiers** (most capable first; complex, open-ended or "
        "judgement-heavy work belongs on tier 1, only mechanical, well-defined "
        "work should run lower; never assume a newer or bigger-sounding id "
        "is stronger):",
    ]
    for tier in (*sorted(t for t in by_tier if t), None):
        rows = by_tier.get(tier)
        if not rows:
            continue
        label = (f"t{tier} {app_config.MODEL_TIER_LABELS.get(tier, '')}" if tier
                 else "t? untiered (a local or custom model nobody rated)")
        # Models that share a line share it once — the ids first, then the
        # line — so two vendors' models of one tier read as the same thing.
        groups: dict[str, list[str]] = {}
        for m in rows:
            groups.setdefault(m.get("good_at") or "", []).append(f"`{m['model_id']}`")
        items = ", ".join(
            f"{', '.join(ids)} ({good_at})" if good_at else ", ".join(ids)
            for good_at, ids in groups.items()
        )
        lines.append(f"- {label}: {items}")
    return lines


def build_meetings_access(user_sub: str, user_role: str) -> list[dict]:
    """Resolve the agents the acting USER can access, with their per-agent
    role — the meetings participant set (the meetings API gates on user
    access, NOT the delegation roster; see api/meetings/meetings.py).

    Returns ``[{slug, display_name, description, role}, …]`` sorted by slug.
    Empty for no-user sessions (``user_sub == ""``) — their reach is the
    roster instead, and ``_meetings_mcp_context`` falls back to it. Admins
    have no explicit rows; they get every agent tagged "admin".

    Sync DB reads — call via ``asyncio.to_thread`` from the config builders
    and pass the result through the ``meetings_access`` kwarg; NEVER call
    from a context provider (providers are no-I/O by contract).
    """
    from storage.agents import agent_store
    from storage import database as task_store

    if not user_sub:
        return []
    if roles.is_admin(user_role):
        by_agent: dict[str, str] = {
            slug: roles.ADMIN for slug in agent_store.get_agent_slugs()}
    else:
        by_agent = task_store.get_user_agent_roles(user_sub) or {}
    rows: list[dict] = []
    for slug in sorted(by_agent):
        data = agent_store.get_agent(slug)
        if not data:
            continue
        rows.append({
            "slug": slug,
            "display_name": data.get("display_name", slug),
            "description": data.get("description", "") or "",
            "role": by_agent[slug],
        })
    return rows


def _supports_remote(execution_path: str) -> bool:
    """Whether the engine can run on a satellite at all (the roster's
    ``local_only`` flag). An unregistered id reads as local."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(execution_path)
    return bool(caps and caps.runtime.supports_remote_execution)


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
            tiers = layer.get("tiers") or {}
            shown = ", ".join(
                f"{m} [default, {_tier_tag(tiers.get(m))}]"
                if m == layer["default_model"]
                else f"{m} [{_tier_tag(tiers.get(m))}]"
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


def _department_data(agent_name: str) -> dict | None:
    """The agent's department row, for ``_department_line``, and a warm
    agent cache (sync store reads: call off the loop). None when the agent
    is in no department."""
    from storage.agents import agent_store, db_departments
    self_data = agent_store.get_agent(agent_name) or {}
    dept_id = self_data.get("department_id") or ""
    if not dept_id or not self_data.get("department_level_id"):
        return None
    agent_store.get_all_agents()
    return db_departments.get_department(dept_id)


_DEPARTMENT_UNRESOLVED = object()


def _department_line(
    agent_name: str, self_data: dict, delegation_targets: list[str],
    department=_DEPARTMENT_UNRESOLVED,
) -> str:
    """One compact department-membership line for the roster, or ''.

    "You are in <Dept> at level <Head>. Same-level: a; level above: b;
    level below: c." Buckets follow the department's mode × reach (the
    wired targets are the input, so they already do): under 'both' adjacent
    = one level either way and subtree = the whole department; under the
    downward modes no level above appears (the buckets are capped so a big
    mesh can't bloat the prompt) and one clause says so, since a bottom-level
    agent would otherwise see no list at all and guess. Best-effort — a
    lookup failure must never break a prompt build."""
    dept_id = self_data.get("department_id") or ""
    level_id = self_data.get("department_level_id") or ""
    if not dept_id or not level_id:
        return ""
    try:
        from storage.agents import agent_store
        from storage.agents import db_departments
        # ``department`` is the row ``get_dynamic_contexts`` pre-resolved
        # off the loop; a direct caller without it reads here.
        dept = (db_departments.get_department(dept_id)
                if department is _DEPARTMENT_UNRESOLVED else department)
        if not dept:
            return ""
        levels = dept["levels"]
        level = next((lv for lv in levels if lv["id"] == level_id), None)
        if not level:
            return ""
        targets = set(delegation_targets)
        same_level: list[str] = []
        above: list[str] = []
        below: list[str] = []
        above_ids = {lv["id"] for lv in levels if lv["rank"] < level["rank"]}
        below_ids = {lv["id"] for lv in levels if lv["rank"] > level["rank"]}
        for a in (agent_store.get_all_agents() if targets else ()):
            slug = a["slug"]
            if slug == agent_name or slug not in targets:
                continue
            if (a.get("department_id") or "") != dept_id:
                continue
            member_level = a.get("department_level_id") or ""
            if member_level == level_id:
                same_level.append(slug)
            elif member_level in above_ids:
                above.append(slug)
            elif member_level in below_ids:
                below.append(slug)
        line = (
            f"\nYou are part of the **{dept['name']}** department "
            f"at level **{level['name']}**."
        )
        wiring = db_departments.MODE_WIRING[dept["mode"]]
        if wiring.down and not wiring.up:
            line += (
                " Delegation in this department runs downward and across "
                "your own level, never upward." if wiring.peers
                else " Delegation in this department runs downward only."
            )

        def _bucket(slugs: list[str], cap: int = 15) -> str:
            # A subtree (full-mesh) department can be large — cap the listing
            # so the prompt stays compact; the wired-targets roster above
            # carries the complete set anyway.
            shown = ", ".join(f"`{s}`" for s in sorted(slugs)[:cap])
            extra = len(slugs) - cap
            return shown + (f" (+{extra} more)" if extra > 0 else "")

        parts = []
        if same_level:
            parts.append("same-level: " + _bucket(same_level))
        if above:
            parts.append("level(s) above: " + _bucket(above))
        if below:
            parts.append("level(s) below: " + _bucket(below))
        if parts:
            line += " Department delegation — " + "; ".join(parts) + "."
        return line
    except Exception:
        logger.exception("department roster line failed for %s", agent_name)
        return ""


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
    the pre-existing agent-row reads). This block is the WIRED reach
    (delegation edges) only — meetings advertise the user-access set in
    their own block (``_meetings_mcp_context``). A session-start "active
    parallel sessions" block rides along — it covers layers with no
    per-turn prelude injection (PTY, remote) on their first turn.

    Deliberately NO session-start notice for received files (removed
    2026-08-14 after the first live test): a stamped once-only notice is
    fragile against warmup/parallel config builds racing to consume it,
    while the prompt's workspace listing already shows
    ``workspace/inbox/<sender>/…`` on every session — presence + sender
    for free. Senders put context IN the files (README) when it matters.
    """
    sibling_block = ""
    from core.session import sibling_awareness
    block = sibling_awareness.context_block(
        agent_name, kwargs.get("user_sub", "") or "")
    if block:
        sibling_block = block

    from storage.agents import agent_store

    if not delegation_targets:
        # No roster to list (a bottom level under a downward mode, or a
        # person's narrowed view): the department line still tells the agent
        # its place.
        dept_line = _department_line(agent_name, agent_store.get_agent(agent_name) or {}, [],
                                     department=kwargs.get("department", _DEPARTMENT_UNRESOLVED))
        return "\n".join(p for p in (dept_line.strip(), sibling_block) if p) or None

    roster: dict[str, list[dict]] = kwargs.get("delegation_roster") or {}

    # No-user sessions read EVERY wired target (a delegation edge is the
    # read grant — merged observe semantics); the config builders pass
    # False for user-backed sessions, which read via their user's access.
    nouser_reads = bool(kwargs.get("nouser_reads"))

    def _agent_lines(slug: str, data: dict, *, is_self: bool) -> list[str]:
        name = data.get("display_name", slug)
        desc = data.get("description", "")
        tag = " *(this is you)*" if is_self else ""
        out = [f"- **{name}** (`{slug}`){tag}{f' — {desc}' if desc else ''}"]
        layers = roster.get(slug) or []
        if layers:
            out.append(f"  layers: {_fmt_roster_layers(layers)}")
        return out

    # Build agent roster: self first, then targets. This is the WIRED set
    # (delegation edges) — the meetings block advertises the user-access set
    # separately, so the two reaches never blur.
    lines = [
        "## Available Agents\n",
        "The following agents are wired as your delegation targets"
        " (`delegate()` / `send_files()`):\n",
    ]

    # Self
    self_data = agent_store.get_agent(agent_name)
    if self_data:
        lines.extend(_agent_lines(agent_name, self_data, is_self=True))

    # Department framing (agents map): one compact line when assigned. Peer
    # lists are drawn ONLY from delegation_targets (already access-filtered
    # and edge-backed), so the prompt never claims reach that spawn authz
    # would refuse — with the department's mode off, dept-mates simply
    # don't appear. Buckets are capped inside _department_line (a subtree
    # mesh can be the whole department under mode 'both').
    dept_line = _department_line(agent_name, self_data or {}, delegation_targets,
                                 department=kwargs.get("department", _DEPARTMENT_UNRESOLVED))
    if dept_line:
        lines.append(dept_line)

    # Delegation targets (excluding self if present)
    for slug in delegation_targets:
        if slug == agent_name:
            continue
        data = agent_store.get_agent(slug)
        if data:
            lines.extend(_agent_lines(slug, data, is_self=False))

    lines.extend(_fmt_model_tiers(roster))
    lines.append("")
    lines.append(
        "**Delegation**: Use `delegate(agent=\"...\", surface=...)` to delegate work to another agent. "
        "Use `continue_id` with a prior task/chat id for multi-turn conversations."
    )
    if roster:
        lines.append(
            "Model/layer overrides on `delegate()` must come from the "
            "target's `layers:` line above (the `[t1]`..`[t4]` tags are the "
            "capability tiers listed under Model tiers); omit both to "
            "inherit the worker's defaults (recommended). Ignored with "
            "`continue_id`."
        )
    lines.append(
        "`send_files(target_agent, paths, …)` copies workspace files into a "
        "target's `workspace/inbox/` — hand-off without making it act; "
        "follow with `delegate()` when it should."
    )
    if nouser_reads:
        lines.append(
            "This session runs without a user: every delegation target "
            "listed above is READABLE — pass its slug as the `agent` "
            "argument of the list tools (schedules / triggers / "
            "notifications / delegation `list_sessions`+`peek_session`) to "
            "read its scheduled tasks, run history, sessions, triggers, "
            "and notifications — agent-scope only, strictly read-only "
            "(fire/mutations stay on your own agent; `delegate()` when "
            "they should act)."
        )
    if sibling_block:
        lines.extend(["", sibling_block])
    return "\n".join(lines)


register("delegation-mcp", _delegation_mcp_context)


def _schedules_mcp_context(
    agent_name: str,
    **kwargs: Any,
) -> str | None:
    """Tell the agent which models/layers its own scheduled tasks may pin.

    ``create_scheduled_task`` / ``create_one_time_task`` / ``edit_task`` accept
    a per-task ``model``/``layer`` that overrides the agent's default for that
    task alone. The valid ids are this agent's OWN enabled layers — the same
    set ``spawn_authz.validate_spawn_overrides`` enforces — which until now
    were rendered only by the delegation block. An agent with schedules-mcp
    and no delegation-mcp could not see them at all.

    Renders the agent's own ``layers:`` line from the pre-resolved
    ``delegation_roster`` (self is always in it: both config builders prepend
    the agent's own slug). When ``delegation-mcp`` is ALSO assigned, that
    block already prints the same line under *(this is you)* — emit a pointer
    instead of a second copy.
    """
    roster: dict[str, list[dict]] = kwargs.get("delegation_roster") or {}
    layers = roster.get(agent_name) or []
    if not layers:
        return None

    lines = ["## Scheduled-task Execution\n"]
    if "delegation-mcp" in (kwargs.get("assigned_mcps") or []):
        lines.append(
            "Your own `layers:` line in **Available Agents** above lists the "
            "execution layers and model ids your scheduled tasks may pin, "
            "and the Model tiers list there ranks them."
        )
    else:
        lines.append(f"Your layers: {_fmt_roster_layers(layers)}")
        lines.extend(_fmt_model_tiers({agent_name: layers}))

    lines.append(
        "\nTasks run on your default model unless pinned. `model` / `layer` "
        "on `create_scheduled_task`, `create_one_time_task` and `edit_task` "
        "pin ONE task's runs to a different model or layer, leaving your "
        "default untouched everywhere else — the lever for keeping a lighter "
        "default while one demanding task runs on a stronger model (or the "
        "reverse). Pin by tier, never by how an id sounds: complex, "
        "open-ended or judgement-heavy work belongs on a tier 1 model. Values "
        "outside the list above are "
        "rejected. Ask the user before pinning unless they asked for it; "
        "`edit_task(model=\"\")` clears a pin. `get_task` reads a task back "
        "with its prompt."
    )
    return "\n".join(lines)


register("schedules-mcp", _schedules_mcp_context)


# Cap on agents rendered into the meetings participant list — a big install's
# full catalog belongs in the dashboard, not every system prompt.
_MEETINGS_ACCESS_CAP = 24


def _meetings_mcp_context(
    agent_name: str,
    delegation_targets: list[str] | None = None,
    **kwargs: Any,
) -> str | None:
    """Inject the meeting capability note when meetings-mcp is assigned.

    Participants follow the acting USER's access (the ``meetings_access``
    kwarg, pre-resolved off-loop by the config builders via
    ``build_meetings_access``) — the meetings API never gated on the
    delegation roster. No-user sessions have no user to derive access from;
    their reach IS the roster (the create endpoint clamps to it), so they
    fall back to the roster-derived list.
    """
    from storage.agents import agent_store as _agent_store

    access: list[dict] = kwargs.get("meetings_access") or []
    if access:
        peers = [a for a in access if a["slug"] != agent_name]
        if not peers:
            return None
        shown = peers[:_MEETINGS_ACCESS_CAP]
        lines = [
            "## Meeting Rooms\n",
            "You can start multi-agent meetings with `start_meeting(topic, agents)`.",
            "Participants follow YOUR USER'S access (their role travels with "
            "you) — not your delegation roster. You can invite:",
        ]
        for a in shown:
            desc = a["description"].strip()
            if len(desc) > 140:
                desc = desc[:140].rstrip() + "…"
            lines.append(
                f"- **{a['display_name']}** (`{a['slug']}`) ({a['role']})"
                f"{f' — {desc}' if desc else ''}"
            )
        if len(peers) > len(shown):
            lines.append(f"- …and {len(peers) - len(shown)} more (see the dashboard's agents page).")
        lines.extend([
            "",
            "Meetings are deliberate, observable communication — every turn "
            "lands in a visible transcript. To hand WORK to another agent, "
            "use `delegate()` (wired targets only), not a meeting.",
        ])
        return "\n".join(lines)

    # No-user fallback: roster-derived (the create endpoint clamps a no-user
    # session's participants to roster ∪ self).
    if not delegation_targets:
        return None
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
        "You can start multi-agent meetings with `start_meeting(topic, agents)`.",
        "This session has no user, so participants are limited to your wired "
        f"delegation targets: {', '.join(peer_names)}",
        "",
        "Meetings are deliberate, observable communication — every turn "
        "lands in a visible transcript. To hand WORK to another agent, "
        "use `delegate()`, not a meeting.",
    ]
    return "\n".join(lines)


register("meetings-mcp", _meetings_mcp_context)


def _ssh_hosts_context(
    agent_name: str,
    placement: PlacementCapabilities = LOCAL_PLACEMENT,
    **kwargs: Any,
) -> str | None:
    """Inject the authorized SSH host list for the ssh-hosts MCP.

    Agents use plain ``ssh``/``scp``/``rsync`` from bash against
    admin-configured hosts (the MCP's one tool, ``list_ssh_hosts``, only
    re-reads this list mid-session). Each authorizing instance
    renders as a ready-to-run command line; the referenced private keys are
    materialized per session at ``$OTO_SSH_KEY_DIR`` — locally by
    ``session_config_dir.materialize_ssh_keys_for_sandbox``, on admin-paired
    satellites via the session-file broker. Any other remote target gets
    nothing (``build_session_mcp_config`` excludes the MCP there with a
    visible reason: infra key material never reaches user-paired machines),
    and so does a session below the editor tier: it holds no keys
    (``session_config_dir.session_takes_ssh_keys``), so the block that names
    them is not rendered.
    """
    from auth import roles
    if not roles.can_edit(kwargs.get("user_role") or ""):
        return None
    if placement.is_remote and not placement.admin_paired:
        return None

    from storage.mcp import mcp_store

    instances = mcp_store.get_mcp_instances_for_agent("ssh-hosts", agent_name)
    if not instances:
        return None

    # Connection multiplexing: agent sessions burst short ssh commands, and
    # each fresh TCP connect to :22 looks like a scan to IDS gateways
    # (Suricata ET SCAN 2001219 killed a legit flow mid-command, 2026-07-06).
    # ControlMaster reuses one authenticated connection; ControlPersist keeps
    # the master ≤60s past last use so nothing authenticated outlives session
    # teardown by much. Windows OpenSSH has no unix-socket mux — omit there,
    # and omit when the machine reported no ``os`` ("" = unknown,
    # conservative — the RAW capability, not the path-shape family).
    #
    # Socket path: NOT under $OTO_SSH_KEY_DIR — on satellites that dir nests
    # in the session-secrets tree and `cm-%C` (40-hex) overflowed the 108-byte
    # sun_path limit (ssh exits 255 before connecting; hit live 2026-07-11).
    # The shell expands the chain to the OS's short PRIVATE runtime dir:
    # XDG_RUNTIME_DIR (/run/user/<uid>, 0700, systemd Linux) → TMPDIR
    # (per-user 0700 /var/folders/… on macOS) → /tmp (inside the local
    # sandbox /tmp is mount-namespaced private; the chain only lands on a
    # shared /tmp for exotic non-systemd admin-paired hosts).
    _row = host_os.of(placement.os)
    mux_capable = (not placement.is_remote) or bool(_row is not None and _row.posix)

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
