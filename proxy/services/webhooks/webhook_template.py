"""Webhook manifest template substitution.

Pure helpers extracted from ``services.webhooks.subscription_manager``: assemble the
``${...}`` substitution namespace from a subscription row + bound OAuth token,
render strings / nested structures, and walk a dotted path into a vendor's JSON
response. No I/O, no shared mutable state — kept as a standalone module so the
substitution rules stay testable independent of the create/delete/renew flow.

``subscription_manager`` re-exports these names for backwards compatibility,
so existing call sites (``_TOKEN_RE``, ``_build_substitutions``, …) are
unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

import config

# Possessive quantifier: an unclosed ``${`` can never match by giving
# characters back ("}" is excluded from the class), so backtracking on a
# long tail is pure ReDoS surface.
_TOKEN_RE = re.compile(r"\$\{([^}]++)\}")


def _build_substitutions(
    *,
    row: dict,
    access_token: str,
    extra: dict[str, Any] | None = None,
    account_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the substitution namespace for template rendering.

    Namespaces (keys are dotted paths so `${vendor_target}` etc. work):
      * top-level: ``vendor_target``, ``vendor_subscription_id``,
        ``subscription_id``, ``provider_id``
      * ``platform.webhook_base`` — externally-reachable platform URL
      * ``account.access_token`` — bound OAuth token
      * ``account.extra.<key>`` — flattened from the bound token file's
        ``extra`` dict (e.g. ``${account.extra.object_id}`` for Microsoft
        Graph's tenant-anchored user GUID)
      * caller-supplied extras (``selected_events``, ``signing_secret``, etc.)
    """
    subs: dict[str, Any] = {
        "vendor_target": row.get("vendor_target", ""),
        "vendor_subscription_id": row.get("vendor_subscription_id", "") or "",
        "subscription_id": row.get("id", ""),
        "provider_id": row.get("provider_id", ""),
        "platform.webhook_base": (config.DASHBOARD_PUBLIC_URL or "").rstrip("/"),
        "account.access_token": access_token or "",
    }
    for k, v in (account_extra or {}).items():
        if isinstance(k, str):
            subs[f"account.extra.{k}"] = v
    for k, v in (extra or {}).items():
        subs[k] = v
    return subs


def _substitute_string(template: str, subs: dict[str, Any]) -> str:
    """Replace `${key}` tokens in a string. Missing keys render empty."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        val = subs.get(key, "")
        if isinstance(val, (dict, list)):
            return json.dumps(val)
        return str(val)
    return _TOKEN_RE.sub(repl, template)


def _substitute_value(value: Any, subs: dict[str, Any]) -> Any:
    """Recursive substitution for dicts/lists/strings.

    For strings that are EXACTLY ``${key}`` and the resolved value is a
    list/dict, return the value verbatim (not stringified). This lets
    body_template fields like ``"events": "${selected_events}"`` produce
    JSON arrays in the request body.
    """
    if isinstance(value, str):
        m = _TOKEN_RE.fullmatch(value.strip())
        if m:
            key = m.group(1)
            if key in subs:
                return subs[key]
            return ""
        return _substitute_string(value, subs)
    if isinstance(value, dict):
        return {k: _substitute_value(v, subs) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_value(v, subs) for v in value]
    return value


def _walk_dot_path(data: Any, path: str) -> Any:
    """Walk a dotted path into a parsed JSON dict. Returns None on miss."""
    cursor = data
    for seg in path.split("."):
        if cursor is None:
            return None
        if isinstance(cursor, dict):
            cursor = cursor.get(seg)
        elif isinstance(cursor, list):
            try:
                cursor = cursor[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cursor
