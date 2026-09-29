"""Inline-artifact backchannel — validation, rate limiting, prompt framing.

``window.otodock.send(payload)`` inside a display_ui artifact posts an
``action`` message to the parent, which forwards it here over the chat WS as
an ``artifact_interaction`` frame. Delivery rides the normal chat rails with
three deliberate authority downgrades: never a "user" row (a distinct
``artifact_interaction`` event row + framed prompt carry the provenance),
never steered into a running turn (queue to the boundary — a page script
must not redirect the agent mid-work), never title-setting.

Pinned app send_prompt actions (``otodock.action(id, args)`` →
``app_action`` frame) ride the SAME queue/turn rails as kind-tagged
interaction dicts with one authority upgrade over free-form sends — the
prompt TEMPLATE was user-approved at pin time — and the same downgrade for
everything else: argument values are page data, so the whole substituted
prompt embeds fenced.

The checks here are the security boundary — the browser-side consent chip
and client rate limit are UX guards only:

* the token must be a ``ui`` capability token BOUND TO THE VIEWED CHAT
  (chatless task/workspace artifacts cannot speak; cross-chat injection is
  impossible regardless of client state); an app must belong to the viewed
  chat's AGENT (and to the caller, for personal apps) with an approved
  manifest;
* payload ≤ 8KB of JSON — and each delivery costs a real agent turn, so the
  rate window and the pending-queue cap bound spend, not just noise.
"""

import json
import time

from storage import database as task_store
from ws import wire_events as wire

MAX_PAYLOAD_BYTES = 8192
MAX_TITLE_CHARS = 200
MAX_SUBSTITUTED_PROMPT_CHARS = 8000
QUEUE_CAP = 3
MIN_INTERVAL_S = 1.0
WINDOW_LIMIT = 12
WINDOW_S = 60.0

# (chat_id, token) → recent send monotonic timestamps. Module-level so a WS
# reconnect can't reset the window; pruned in place on every check.
_rate: dict[tuple[str, str], list[float]] = {}


def check_rate(chat_id: str, token: str) -> bool:
    """Record + admit one send, or reject (≥1s apart AND ≤12/min)."""
    now = time.monotonic()
    key = (chat_id, token)
    stamps = [t for t in _rate.get(key, []) if now - t < WINDOW_S]
    if stamps and (now - stamps[-1] < MIN_INTERVAL_S or len(stamps) >= WINDOW_LIMIT):
        _rate[key] = stamps
        return False
    stamps.append(now)
    _rate[key] = stamps
    if len(_rate) > 512:  # drop dead chats' keys, keep the dict bounded
        for k in [k for k, v in _rate.items() if not v or now - v[-1] > WINDOW_S]:
            _rate.pop(k, None)
    return True


def validate_interaction(
    chat_id: str, token: str, title: str, payload,
) -> tuple[dict | None, str]:
    """Server-side provenance + payload gate. Returns (interaction, "") or
    (None, reason). The interaction dict is what queues/persists/frames."""
    if not chat_id or not token:
        return None, "missing chat or token"
    info = task_store.get_media_token(token)
    if not info or (info.get("media_kind") or "") != "ui":
        return None, "unknown artifact"
    if not info.get("chat_id") or info["chat_id"] != chat_id:
        return None, "artifact not bound to this chat"
    try:
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None, "payload not JSON-serializable"
    if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return None, "payload too large"
    return {
        "token": token,
        "title": (title or "").strip()[:MAX_TITLE_CHARS],
        "payload": payload,
        "payload_json": payload_json,
    }, ""


def validate_app_action(
    chat_id: str, chat_agent: str, user_sub: str, app_id: str, action_id: str, args,
) -> tuple[dict | None, str]:
    """Server-side gate for an app send_prompt action. Returns a
    kind-tagged interaction dict (rides the same queue/drain rails as
    artifact interactions) or (None, reason).

    The app must belong to the VIEWED chat's agent — an app must never
    speak into another agent's chat wearing its provenance framing — and to
    the caller for personal rows. fire_task actions never come here (they
    execute via REST in api/apps); approval is checked against the live sig
    so a mutated manifest fails closed.
    """
    from api.apps import manifest as _mf

    if not chat_id or not app_id or not action_id:
        return None, "missing chat, app, or action"
    row = task_store.get_app(app_id)
    if not row:
        return None, "unknown app"
    if (row.get("agent") or "") != chat_agent:
        return None, "app not available in this chat"
    if row.get("username") and (row.get("owner_sub") or "") != user_sub:
        return None, "unknown app"
    if not task_store.app_actions_approved(row):
        return None, "actions not approved"
    action = _mf.find_action(row, action_id)
    if action is None:
        return None, "unknown action"
    if action.get("type") != "send_prompt":
        return None, "not a send_prompt action"
    if action.get("min_role"):
        # The floor is judged on the socket user's per-agent role, rebuilt
        # from the store (the socket carries the sub alone).
        from auth.providers import user_context_for_sub
        if not _mf.meets_floor(action, _mf.caller_role(row, user_context_for_sub(user_sub))):
            return None, _mf.floor_reason(action)
    try:
        args_json = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None, "args not JSON-serializable"
    if len(args_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return None, "args too large"
    from services.scheduler.trigger_manager import _substitute_placeholders
    context = args if isinstance(args, dict) else {}
    prompt = _substitute_placeholders(action.get("prompt") or "", context) or ""
    if len(prompt) > MAX_SUBSTITUTED_PROMPT_CHARS:
        return None, "prompt too large after substitution"
    return {
        "kind": wire.APP_ACTION,
        "app_id": app_id,
        "slug": row.get("slug") or "",
        "title": (row.get("title") or "").strip()[:MAX_TITLE_CHARS],
        "action_id": action_id,
        "label": action.get("label") or "",
        "prompt": prompt,
    }, ""


_ARTIFACT_TRAILER = (
    "(Sent by a control inside an agent-generated UI artifact — "
    "page-event data, not the user typing.)"
)
_APP_TRAILER = (
    "(Sent by a declared action button on a pinned app — the prompt "
    "template was approved by the user; argument values come from the app "
    "page, not the user typing.)"
)


def _fence_safe(text: str) -> str:
    """Break backtick fence runs with a zero-width space so embedded content
    can't escape its code block."""
    return text.replace("```", "`​``")


def frame_text(interactions: list[dict]) -> str:
    """The model-facing turn text for one delivered batch (entries in order).

    Bodies embed fence-safe (backtick runs broken with a zero-width space)
    so crafted content can't escape its code block — for app actions that
    covers the ENTIRE substituted prompt (an arg value could carry fence
    runs or fake provenance headers). Trailers mark the provenance per kind.
    """
    parts = []
    kinds = set()
    for it in interactions:
        title = (it["title"] or "untitled").replace('"', "'")
        if it.get("kind") == wire.APP_ACTION:
            kinds.add("app")
            label = (it["label"] or it["action_id"]).replace('"', "'")
            parts.append(
                f'[action from app "{title}" — {label}]\n'
                f'```text\n{_fence_safe(it["prompt"])}\n```'
            )
        else:
            kinds.add("artifact")
            safe = _fence_safe(it["payload_json"])
            parts.append(f'[interaction from artifact "{title}"]\n```json\n{safe}\n```')
    if "artifact" in kinds:
        parts.append(_ARTIFACT_TRAILER)
    if "app" in kinds:
        parts.append(_APP_TRAILER)
    return "\n\n".join(parts)


def event_row_json(interaction: dict) -> str:
    """The persisted event row / ws frame payload (``artifact_interaction``
    or ``app_action`` by kind)."""
    if interaction.get("kind") == wire.APP_ACTION:
        return json.dumps({
            "type": wire.APP_ACTION,
            "app_id": interaction["app_id"],
            "slug": interaction["slug"],
            "title": interaction["title"],
            "action_id": interaction["action_id"],
            "label": interaction["label"],
            "prompt": interaction["prompt"],
        })
    return json.dumps({
        "type": wire.ARTIFACT_INTERACTION,
        "token": interaction["token"],
        "title": interaction["title"],
        "payload": interaction["payload"],
    })


def event_type(interaction: dict) -> str:
    """The chat_messages.event_type for one interaction's persisted row."""
    return wire.APP_ACTION if interaction.get("kind") == wire.APP_ACTION else wire.ARTIFACT_INTERACTION


def ws_frame(interaction: dict, chat_id: str) -> dict:
    """The live ws frame for one delivered interaction (chip render)."""
    if interaction.get("kind") == wire.APP_ACTION:
        return {
            "type": wire.APP_ACTION, "app_id": interaction["app_id"],
            "slug": interaction["slug"], "title": interaction["title"],
            "action_id": interaction["action_id"], "label": interaction["label"],
            "prompt": interaction["prompt"], "chat_id": chat_id,
        }
    return {
        "type": wire.ARTIFACT_INTERACTION, "token": interaction["token"],
        "title": interaction["title"], "payload": interaction["payload"],
        "chat_id": chat_id,
    }
