"""Serve-time access rule for the capability-token routes (/v1/ui, /v1/media).

The 256-bit token is unguessable but bearer-like: any HOLDER of a leaked link
could open the resource. The serve routes therefore ALSO require an
authenticated platform user — the session cookie rides along on every
same-origin dashboard fetch, including the sandboxed artifact iframe's
document request (verified empirically: site-for-cookies computes over the
ANCESTOR chain, so the opaque-origin frame still gets Lax/Strict cookies) and
the Android app's DownloadManager handoff (MainActivity forwards WebView
cookies). Then the requester is checked against the token's provenance:

1. chat-bound rows (``chat_id`` set) → the chats API access rule: owner OR
   admin OR, for a shared-only agent's synthetic-owner chat, any user
   assigned to the agent.
2. chatless rows stamped with ``agent`` (workspace mints; task-run sessions,
   whose transcripts render in agent History) → agent access.
3. rows with neither stamp (minted before the columns existed) → any
   authenticated user. Restore-friendly coarse fallback; no backfill.

``owner_sub`` is stamped where a real user sub exists but not yet enforced —
explicit share tokens (the sharing era) will refine this rule with it.
"""

from auth.providers import UserContext
from storage import database as task_store


def can_serve_token(info: dict, user: UserContext | None) -> bool:
    """May ``user`` fetch the resource behind this ``media_tokens`` row?"""
    if user is None:
        return False
    chat_id = info.get("chat_id")
    if chat_id:
        from api.agents.chats import can_access_chat
        chat = task_store.get_chat(chat_id)
        if not chat:
            return False
        return can_access_chat(user, chat)
    agent = (info.get("agent") or "").strip()
    if agent:
        return user.is_admin or user.can_access_agent(agent)
    return True
