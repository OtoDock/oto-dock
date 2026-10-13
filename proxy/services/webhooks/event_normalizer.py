"""Pure functions for vendor-payload normalization.

Three things live here:

* ``walk_path`` — JSONPath-subset resolver shared by event-id extraction
  AND payload normalization. Supports ``headers.<name>`` (case-insensitive),
  ``body.<dot.path>`` (with array indexes), and ``<a>+<b>`` composite keys
  for vendors that need to build dedup IDs from multiple fields.
* ``normalize_event`` — applies a manifest ``payload_normalization`` block
  to raw inbound (headers + body) → ``NormalizedEvent`` dataclass.
* ``match_event_filter`` — equality-dict matcher used at fan-out time to
  decide which triggers fire for a given event.

These are pure (no IO, no globals) so they're trivially testable AND
reusable from both the dispatcher (real events) and ``dynamic_context``
(building ``${trigger.*}`` tokens for downstream agent_context templates).
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from auth.webhook_providers.base import NormalizedEvent

logger = logging.getLogger("otodock.event-normalizer")


def walk_path(*, body: Any, headers: dict[str, str], path: str) -> str:
    """Resolve a JSONPath-subset string against headers + body.

    Path forms:
      * ``headers.X-GitHub-Delivery`` — case-insensitive header lookup
      * ``body.event.user.email`` — dot-walk into the parsed JSON body,
        with array indexes supported via numeric segments
        (``body.value.0.subscriptionId``)
      * ``<expr_a>+<expr_b>`` — both expressions resolved, joined with
        a literal ``+`` (for composite dedup keys; MS Graph)
      * Empty string in = empty string out

    Missing segments yield empty string (never raises). The walker is
    deliberately strict about path syntax (must start with ``headers.``
    or ``body.``); manifest validator enforces this at install time.
    """
    if not path:
        return ""

    # Composite key — recursively resolve each part and join with `+`.
    if "+" in path:
        parts = [walk_path(body=body, headers=headers, path=p) for p in path.split("+")]
        return "+".join(parts)

    if path.startswith("headers."):
        header_name = path[len("headers."):].lower()
        for k, v in (headers or {}).items():
            if k.lower() == header_name:
                return str(v) if v is not None else ""
        return ""

    if path.startswith("body."):
        segments = path[len("body."):].split(".")
        cursor: Any = body
        for seg in segments:
            if cursor is None:
                return ""
            if isinstance(cursor, dict):
                cursor = cursor.get(seg)
                continue
            if isinstance(cursor, list):
                try:
                    cursor = cursor[int(seg)]
                except (ValueError, IndexError):
                    return ""
                continue
            # Scalar walked into — path goes deeper than the data.
            return ""
        if cursor is None:
            return ""
        # Convert non-string scalars to string (numbers, bools).
        if isinstance(cursor, (dict, list)):
            # Path resolved to a container — manifest authors should walk deeper.
            # Return empty rather than dumping JSON; signals the path is wrong.
            return ""
        return str(cursor)

    # Unknown prefix — silently empty (manifest validator catches typos).
    return ""


def normalize_event(
    *,
    body: Any,
    headers: dict[str, str],
    manifest_block: dict,
    vendor_event_id: str = "",
    global_event_id: str = "",
) -> "NormalizedEvent":
    """Apply a manifest's ``payload_normalization`` block to raw inbound.

    Reads ``event_type_path`` plus optional ``actor``, ``subject``,
    ``target`` sub-blocks. Each sub-block's ``<field>_path`` keys resolve
    via ``walk_path``; literal ``type`` keys (e.g., ``target.type =
    "repository"``) pass through verbatim.

    Returns a fully populated ``NormalizedEvent``. Missing paths yield
    empty strings — callers should never see None.
    """
    # Local import avoids a circular dependency at module load time
    # (base.py is loaded before services.webhooks.event_normalizer in some paths).
    from auth.webhook_providers.base import NormalizedEvent

    event_type = walk_path(
        body=body, headers=headers,
        path=manifest_block.get("event_type_path", ""),
    )

    actor = _resolve_namespace(body=body, headers=headers,
                                block=manifest_block.get("actor"))
    subject = _resolve_namespace(body=body, headers=headers,
                                  block=manifest_block.get("subject"))
    target = _resolve_namespace(body=body, headers=headers,
                                 block=manifest_block.get("target"))

    return NormalizedEvent(
        event_type=event_type,
        vendor_event_id=vendor_event_id,
        global_event_id=global_event_id,
        actor=actor,
        subject=subject,
        target=target,
    )


def _resolve_namespace(
    *, body: Any, headers: dict[str, str], block: Any,
) -> dict[str, str]:
    """Resolve one of actor/subject/target into a flat dict[str, str].

    ``*_path`` values may be a single path string OR a list of fallback
    paths. For lists, the first path that resolves to a non-empty value
    wins — lets one manifest cover multiple vendor event shapes
    (e.g. GitHub's ``body.pull_request.title`` vs ``body.issue.title``
    vs ``body.release.name``).
    """
    if not isinstance(block, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in block.items():
        if key == "type" and isinstance(val, str):
            # Literal type (e.g., target.type = "repository").
            out["type"] = val
            continue
        if not key.endswith("_path"):
            continue
        short = key[: -len("_path")]  # strip "_path" suffix
        if isinstance(val, str):
            out[short] = walk_path(body=body, headers=headers, path=val)
        elif isinstance(val, list):
            out[short] = ""
            for path in val:
                if not isinstance(path, str):
                    continue
                v = walk_path(body=body, headers=headers, path=path)
                if v:
                    out[short] = v
                    break
    return out


def match_event_filter(
    *, event: "NormalizedEvent", event_filter: dict,
) -> bool:
    """Equality-dict match: every key in event_filter must equal the
    corresponding field in the NormalizedEvent.

    Filter syntax:
      * Empty dict ``{}`` → match all events (the intuitive default).
      * Top-level keys ``event_type``, ``vendor_event_id`` → match against
        the same-named NormalizedEvent attr.
      * Dot-path keys ``actor.id``, ``subject.type``, ``target.id`` → walk
        into the corresponding dict.
      * Values may be:
          - string → exact equality
          - list → any-of match (e.g., ``[\"opened\", \"reopened\"]``)
          - None → field must be absent or empty
    """
    if not event_filter:
        return True
    if not isinstance(event_filter, dict):
        return False
    for key, expected in event_filter.items():
        actual = _read_event_field(event, key)
        if not _value_matches(actual, expected):
            return False
    return True


def _read_event_field(event: "NormalizedEvent", key: str) -> str:
    """Read a NormalizedEvent field by dot-path.

    Top-level: ``event_type`` / ``vendor_event_id``. Nested:
    ``actor.<sub>`` / ``subject.<sub>`` / ``target.<sub>``. Unknown
    keys evaluate to empty string (filter will fail unless it expects
    empty/None).
    """
    if "." not in key:
        # Top-level field via getattr.
        return str(getattr(event, key, "") or "")
    head, _, sub = key.partition(".")
    container = getattr(event, head, None)
    if not isinstance(container, dict):
        return ""
    val = container.get(sub, "")
    return str(val) if val is not None else ""


def _value_matches(actual: str, expected: Any) -> bool:
    """One-step equality check for an event_filter value.

    * None → actual must be falsy (empty string).
    * list → actual must be in the list (any-of match).
    * str → exact equality.
    """
    if expected is None:
        return not actual
    if isinstance(expected, list):
        return actual in [str(e) for e in expected]
    if isinstance(expected, (str, int, float, bool)):
        return actual == str(expected)
    # Dict / nested-equality not supported — manifest authors flatten the path.
    return False


def resolve_catalog_keys(
    *, body: Any, headers: dict[str, str], raw_event_type: str,
    event_catalog: list[dict],
) -> list[str]:
    """Map a raw vendor event type to its catalog KEY(s).

    Catalog keys are SUBSCRIPTION names (slack's ``message.channels``) while
    payloads carry raw types (``event.type="message"`` + ``channel_type``).
    Entries with a ``match`` block match when ``raw_event_type`` equals
    ``match.event_type`` AND every ``match.conditions`` path (walked via
    :func:`walk_path`) equals the expected string / is in the expected list.
    Entries without ``match`` match when ``raw_event_type == key`` (today's
    behavior). Returns matches in catalog order — the FIRST is canonical.

    Note: condition paths are request-level (``body.``/``headers.``), so the
    mechanism fits single-event-per-request vendors; batched vendors
    (MS Graph ``value[]``) keep plain keys.
    """
    out: list[str] = []
    for entry in event_catalog or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "") or "")
        if not key:
            continue
        match = entry.get("match")
        if not isinstance(match, dict):
            if raw_event_type == key:
                out.append(key)
            continue
        if raw_event_type != str(match.get("event_type", "")):
            continue
        conditions = match.get("conditions") or {}
        matched = True
        for cpath, expected in conditions.items():
            actual = walk_path(body=body, headers=headers, path=str(cpath))
            if isinstance(expected, list):
                if actual not in [str(e) for e in expected]:
                    matched = False
                    break
            elif actual != str(expected):
                matched = False
                break
        if matched:
            out.append(key)
    return out


# A vendor key longer than this is hashed: the app-handler dedupe's unique
# index is a btree, and Graph resource ids run long.
_VENDOR_KEY_MAX = 200


def vendor_event_key(provider_id: str, global_event_id: str) -> str:
    """The ``event_id`` a vendor event fires its triggers with:
    ``<provider>:<the vendor's own event id>``, the same for every
    subscription that sees the event and for every redelivery of it, and
    distinct across providers. "" when the vendor gives no id (no
    cross-subscription dedupe, as before)."""
    if not provider_id or not global_event_id:
        return ""
    key = f"{provider_id}:{global_event_id}"
    if len(key) <= _VENDOR_KEY_MAX:
        return key
    digest = hashlib.sha256(global_event_id.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{provider_id}:sha256:{digest}"
