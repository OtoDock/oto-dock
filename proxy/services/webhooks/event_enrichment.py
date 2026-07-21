"""Manifest-driven event enrichment — ID → name lookups.

Webhook payloads carry raw vendor IDs (slack: actor ``U…``, team ``T…``),
so trigger notifications read like "U0B9ZA98C2W reacted in T0B9SUXDTK5".
Manifests may declare ``credentials.webhooks.enrichment.lookups`` — small
vendor API calls (slack ``users.info`` / ``conversations.info``) made with
the subscription's bound OAuth token just before a MATCHED trigger fires.

Strictly best-effort / fail-open: any miss (no token, vendor error,
timeout, shape mismatch) leaves the event untouched and the trigger still
fires with raw IDs. Results land in the ``NormalizedEvent``'s
actor/subject/target dicts, so notification templates AND task prompt
contexts both see them (``{{actor.name}}``, ``{{target.channel_name}}``).

Lookup results are cached per (subscription identity, rendered URL) with a
per-lookup TTL (default 900s) — a busy channel doesn't re-fetch the same
user on every message.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from auth.webhook_providers.base import NormalizedEvent
from services.webhooks.event_normalizer import walk_path

logger = logging.getLogger("claude-proxy.event-enrichment")

_LOOKUP_TIMEOUT_SECONDS = 3.0     # per vendor call
_EVENT_BUDGET_SECONDS = 5.0       # whole-event ceiling
_DEFAULT_TTL_SECONDS = 900
_CACHE_MAX_ENTRIES = 512

_TOKEN_RE = re.compile(r"\$\{([^}]++)\}")  # possessive — see webhook_template

# (cache_key) -> (outputs_snapshot, monotonic_expiry)
_cache: dict[tuple, tuple[dict[str, str], float]] = {}
_cache_lock = asyncio.Lock()


async def enrich_event(
    event: NormalizedEvent, *, row: dict, webhooks_block: dict,
) -> None:
    """Run the manifest's enrichment lookups against ``event`` in place.
    NEVER raises."""
    lookups = (webhooks_block.get("enrichment") or {}).get("lookups") or []
    if not lookups:
        return
    try:
        await asyncio.wait_for(
            _enrich(event, row=row, lookups=lookups), _EVENT_BUDGET_SECONDS,
        )
    except Exception:
        logger.debug("event enrichment failed (ignored)", exc_info=True)


async def _enrich(event: NormalizedEvent, *, row: dict, lookups: list[dict]) -> None:
    token = ""
    token_loaded = False
    identity = (
        row.get("provider_id", ""), row.get("scope", ""),
        row.get("owner") or "", row.get("account_label", ""),
    )

    for lookup in lookups:
        value = _read_field(event, str(lookup.get("source_field", "")))
        if not value:
            continue
        request = lookup.get("request") or {}
        url = _substitute(str(request.get("url_template", "")), value=value, token="")
        if not url:
            continue

        ttl = int(lookup.get("ttl_seconds") or _DEFAULT_TTL_SECONDS)
        cache_key = (*identity, url)
        cached = await _cache_get(cache_key)
        if cached is not None:
            _write_outputs(event, cached)
            continue

        # Resolve the bound account's token once, lazily — a lookup whose
        # headers don't need it still works without an account.
        needs_token = "${account.access_token}" in str(
            request.get("headers", {}))
        if needs_token and not token_loaded:
            token_loaded = True
            try:
                from services.webhooks.subscription_manager import _resolve_token_or_raise
                token = _resolve_token_or_raise(
                    provider_id=row.get("provider_id", ""),
                    scope=row.get("scope", "user"),
                    owner=row.get("owner") or "",
                    agent=row.get("agent"),
                    mcp_name=row.get("mcp_name", ""),
                    account_label=row.get("account_label", ""),
                )
            except Exception:
                token = ""
        if needs_token and not token:
            continue  # no credential — skip this lookup, never block the fire

        headers = {
            str(k): _substitute(str(v), value=value, token=token)
            for k, v in (request.get("headers") or {}).items()
        }
        method = str(request.get("method", "GET")).upper()
        expected = request.get("expected_status") or [200]
        try:
            async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT_SECONDS) as client:
                resp = await client.request(method, url, headers=headers)
        except Exception:
            continue
        if resp.status_code not in expected:
            continue
        try:
            body = resp.json()
        except Exception:
            continue

        outputs: dict[str, str] = {}
        for out_field, paths in (lookup.get("outputs") or {}).items():
            candidates = [paths] if isinstance(paths, str) else list(paths or [])
            for path in candidates:
                resolved = walk_path(body=body, headers={}, path=str(path))
                if resolved:
                    outputs[str(out_field)] = resolved
                    break
        if not outputs:
            continue
        _write_outputs(event, outputs)
        await _cache_put(cache_key, outputs, ttl)


def _read_field(event: NormalizedEvent, field: str) -> str:
    """Read ``actor.id``-style dot paths from the normalized event."""
    head, _, sub = field.partition(".")
    container = getattr(event, head, None)
    if not isinstance(container, dict) or not sub:
        return ""
    val = container.get(sub, "")
    return str(val) if val else ""


def _write_outputs(event: NormalizedEvent, outputs: dict[str, str]) -> None:
    for field, value in outputs.items():
        head, _, sub = field.partition(".")
        container = getattr(event, head, None)
        if isinstance(container, dict) and sub:
            container[sub] = value


def _substitute(template: str, *, value: str, token: str) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key == "value":
            return value
        if key == "account.access_token":
            return token
        return ""
    return _TOKEN_RE.sub(repl, template)


async def _cache_get(key: tuple) -> dict[str, str] | None:
    async with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        outputs, expiry = hit
        if time.monotonic() > expiry:
            _cache.pop(key, None)
            return None
        return dict(outputs)


async def _cache_put(key: tuple, outputs: dict[str, str], ttl: int) -> None:
    async with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            # Drop the oldest insertion (plain dicts preserve order).
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = (dict(outputs), time.monotonic() + ttl)
