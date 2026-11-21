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
from collections.abc import Callable, Iterable
from typing import Any

import psycopg

from services.infra import external_data
from services.mcp import team_line

logger = logging.getLogger("otodock")

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
    *, account_choices: dict[str, str] | None = None,
    lookup_mcp_names: Iterable[str] | None = None,
    **kwargs,
) -> list[tuple[str, str]]:
    """Resolve all per-session prompt context for one session.

    ``lookup_mcp_names``: MCPs the session does not attach whose manifest
    builder blocks run all the same (a lookup a manager configured to run
    before an outside person's conversation, with the agent's own account);
    their Python providers and template-only blocks, which describe a tool
    the session lacks, do not.

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
    ``account_choices`` (the chat's account per tool) is what the ``account.*``
    tokens, builders and reach lines read.

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
        # The team line beside it (team_line.py): the assignments, off the loop.
        kwargs.setdefault("team", await asyncio.to_thread(team_line.data, agent_name))
    # The display provider names the apps a template seeded: resolved here,
    # off the loop, so the provider reads nothing.
    if "template_apps" not in kwargs and any(
            _providers.get(n) is _display_mcp_context for n in assigned_mcp_names):
        kwargs["template_apps"] = await asyncio.to_thread(_template_apps, agent_name, user_sub)
    # A session is told once where its GitHub reach stops (the gateway and
    # the credential helper refuse past it), by the scope it resolves in.
    reach_lines = await asyncio.to_thread(_reach_lines, agent_name, assigned_mcp_names, user_sub,
                                          account_choices)

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
            except Exception as e:  # noqa: BLE001  # a registered context provider failing must not drop the others
                logger.warning(
                    "Dynamic context provider '%s' failed: %s", mcp_name, e
                )

        # 2. Manifest agent_context blocks (template + optional builder)
        try:
            blocks = await _resolve_manifest_blocks(
                mcp_name, agent_name, user_sub, user_role, session_ctx,
                trigger_payload, account_choices,
            )
            for text in blocks:
                results.append((mcp_name, text))
        except Exception as e:  # noqa: BLE001  # agent_context blocks span the db and builder tools, the prompt goes on
            logger.warning(
                "agent_context resolution failed for '%s': %s", mcp_name, e
            )
        if mcp_name in reach_lines:
            results.append((mcp_name, reach_lines[mcp_name]))
    for mcp_name in lookup_mcp_names or ():
        if mcp_name in assigned_mcp_names:
            continue
        try:
            blocks = await _resolve_manifest_blocks(
                mcp_name, agent_name, user_sub, user_role, session_ctx,
                trigger_payload, account_choices, builders_only=True,
            )
            for text in blocks:
                results.append((mcp_name, text))
        except Exception as e:  # noqa: BLE001  # a lookup that fails leaves the prompt without it
            logger.warning("lookup blocks of '%s' failed: %s", mcp_name, e)
    return results


def _reach_lines(agent_name: str, mcp_names: list[str], user_sub: str = "",
                 account_choices: dict[str, str] | None = None) -> dict[str, str]:
    """The line a session reads for each MCP whose GitHub reach narrows the
    agent (``auth/github_reach``), in the scope its credentials resolve in
    (``account_context.reader_scope``): a session with a user resolves as
    that person, except on a Shared-only agent, whose chats resolve through
    the agent's own accounts, each with the chat's choice. ``user_sub`` is
    the credential user (none for an agent-scope task)."""
    from auth import github_reach
    from auth.oauth_providers import github_app
    from services.mcp import account_context, mcp_registry
    from services.oauth.credential_resolver import binding_reach, choice_kw
    github: list[str] = []
    for name in mcp_names:
        manifest = mcp_registry.get_manifest(name)
        oauth = (manifest.credentials.oauth or {}) if manifest and manifest.credentials else {}
        if str(oauth.get("provider_id") or "") == github_app.PROVIDER_ID:
            github.append(name)
    if not github:
        return {}
    scope, sub = account_context.reader_scope(agent_name, user_sub)
    out: dict[str, str] = {}
    for name in github:
        reach = binding_reach(name, agent_name, user_sub=sub or None, task_scope=scope,
                              **choice_kw(account_choices))
        if reach is not None:
            out[name] = github_reach.instructions_line(reach)
    return out


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
    account_choices: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the ``${ns.key} → value`` map for one (mcp, session).

    Both scopes resolve ``account.*`` via ``credential_resolver.pick_account``,
    in the scope the session's tools resolve in (``account_context.reader_scope``):
    user scope reads the chat's choice (``account_choices``) when the agent's
    set holds it, else the user's bound account; agent scope (a task, a call,
    every session of a Shared-only agent) the agent's own account, a
    Shared-only chat's choice first. No credential value enters the map: a
    block renders into the system prompt, where a secret must never land.
    ``user.*``: a session with a person only.

    Always populates ``agent.*`` (from ``agent_store``) and
    ``session.*`` (passthrough from ``session_ctx``). ``user.role``
    comes from the ``user_role`` kwarg regardless of scope.

    ``trigger.*`` flat tokens populated from ``trigger_payload``
    when supplied; raw body access (``trigger.body.*``) is resolved on
    demand in ``_substitute_tokens`` rather than precomputed.

    Tokens that cannot be resolved are simply absent from the map. The
    substitution loop renders absent tokens as the empty string.
    """
    from services.oauth import credential_resolver
    from storage import database as task_store
    from storage.agents import agent_store
    from storage.identity import credential_store

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

    from core.session.visibility import SCOPE_USER
    from services.mcp import account_context
    if account_context.reader_scope(agent_name, user_sub)[0] == SCOPE_USER:
        # User scope: bound user account.
        ref = credential_resolver.pick_account(
            mcp_name, agent_name, user_sub=user_sub, **credential_resolver.choice_kw(account_choices),
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
    else:
        # Agent scope: the agent's own account (a user's own account a
        # manager lent it), a Shared-only chat's choice first. owner_sub is
        # always a real user_sub — read their user_credentials directly.
        ref = credential_resolver.pick_account(
            mcp_name, agent_name, **credential_resolver.choice_kw(account_choices),
        )
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
            except psycopg.Error as e:
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

    # user.email / user.name: the person at the session, absent with none.
    if user_sub:
        user_row = task_store.get_user(user_sub) or {}
        tokens["user.email"] = str(user_row.get("email") or "")
        tokens["user.name"] = str(
            user_row.get("name") or user_row.get("display_name") or ""
        )

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
    except (psycopg.Error, OSError, ValueError) as e:
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
    account_choices: dict[str, str] | None = None,
    builders_only: bool = False,
) -> list[str]:
    """Render the ``agent_context`` blocks for one assigned MCP.
    ``builders_only`` renders the builder blocks alone (a lookup before an
    outside person's conversation): a template-only block describes a tool
    the session may lack.

    Returns an ordered list of fully-substituted markdown strings, one per
    block that passes the ``scope`` filter AND has all its ``requires``
    tokens resolved. Skipped blocks produce no output (no half-prompts).

    Template-only blocks render synchronously. Builder blocks are scheduled
    for parallel evaluation via ``asyncio.gather`` so total latency is
    ``max(per-block timeouts)`` rather than the sum.
    """
    from services.mcp import builder_executor, mcp_registry

    manifest = mcp_registry.get_manifest(mcp_name)
    if not manifest or not manifest.agent_context:
        return []

    from services.mcp import account_context
    current_scope = account_context.reader_scope(agent_name, user_sub)[0]
    tokens: dict[str, str] | None = None  # lazy — only build if needed

    # Pass 1: filter + render template-only blocks; collect builder coros.
    results: dict[int, str] = {}
    builder_jobs: list[tuple[int, asyncio.Future]] = []

    for idx, block in enumerate(manifest.agent_context):
        if block.scope and current_scope not in block.scope:
            continue
        if builders_only and block.builder is None:
            continue

        if tokens is None:
            tokens = _build_token_map(
                mcp_name, agent_name, user_sub, user_role, session_ctx,
                trigger_payload, account_choices,
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
            **({"account_choices": account_choices} if account_choices else {}),
        )
        builder_jobs.append((idx, asyncio.ensure_future(coro)))

    # Pass 2: await builders in parallel. ``return_exceptions=True`` keeps
    # one bad block from sinking the rest — execute_builder is supposed to
    # catch internally, but this is defence in depth.
    if builder_jobs:
        idxs = [j[0] for j in builder_jobs]
        coros = [j[1] for j in builder_jobs]
        builder_results = await asyncio.gather(*coros, return_exceptions=True)
        for idx, result in zip(idxs, builder_results, strict=False):
            if isinstance(result, BaseException):
                logger.warning(
                    "builder block %d on '%s' raised %s: %s",
                    idx, mcp_name, type(result).__name__, result,
                )
                continue
            if result:
                results[idx] = result

    # Return rendered blocks in original manifest order.
    return [results[i] for i in sorted(results)]



# The built-in Python providers (services/mcp/dynamic_providers.py): the
# engine above reads four of them by name, the tree reads the rest from
# this path.
from services.mcp.dynamic_providers import (  # noqa: F401
    PIPE_AGENT_NOTE,
    ROSTER_LEGEND,
    _delegation_mcp_context,
    _department_data,
    _display_mcp_context,
    _meetings_mcp_context,
    _prompt_word,
    _schedules_mcp_context,
    _ssh_hosts_context,
    _template_apps,
    build_delegation_roster,
    build_meetings_access,
    format_ssh_host_command,
)

register("delegation-mcp", _delegation_mcp_context)
register("display-mcp", _display_mcp_context)
register("schedules-mcp", _schedules_mcp_context)
register("meetings-mcp", _meetings_mcp_context)
register("ssh-hosts", _ssh_hosts_context)
