"""Live status of a delegation lane's chat — derived fresh, nothing persisted.

``generating`` — the chat is working now (``_working``: a pump's turn
streaming, a live terminal inside its turn), the one reading the connect
snapshot, the Active-now list and the AI Engines cards take too.
``awaiting_user`` — the lane is blocked on a human (a permission dialog on
either lane kind, a parked question). ``idle`` — nothing live.

Reads the in-memory sources only: ``_chat_streaming_state`` for pump lanes
and the live interactive-session registry for PTY lanes. Cheap and
synchronous — safe to call per row when listing sessions.
"""

from core.session import interactive_session
from core.session.session_state import (
    _chat_streaming_state,
    _session_permission_requests,
    peek_subagent_registry,
)

STATUS_GENERATING = "generating"
STATUS_AWAITING_USER = "awaiting_user"
STATUS_IDLE = "idle"


def _sub_agent_pending(session_id: str) -> bool:
    reg = peek_subagent_registry(session_id)
    return reg is not None and reg.has_pending


def _working(sub_agents: bool):
    """(chat id, session id) of each session working now, headless turns
    first: a pump's turn streaming, a live terminal inside its turn; with
    ``sub_agents``, also one whose sub-agent is still pending."""
    for chat_id, st in list(_chat_streaming_state.items()):
        sid = st.get("session_id") or ""
        if st.get("streaming") or (sub_agents and sid and _sub_agent_pending(sid)):
            yield chat_id, sid
    for s in interactive_session.live_sessions():
        if s.chat_id and (s._turn_open or (sub_agents and _sub_agent_pending(s.session_id))):
            yield s.chat_id, s.session_id


def working_chat_ids() -> list[str]:
    """The chats working now, each once, headless turns first: what a chat's
    "generating" reads at a connect and in the Active-now list. On the loop."""
    return list(dict.fromkeys(cid for cid, _sid in _working(False) if cid))


def working_sessions(*, sub_agents: bool = False) -> dict[str, str]:
    """The sessions working now, session id to chat id, by the same facts as
    ``working_chat_ids``. ``sub_agents``: a session whose turn waits on a
    pending sub-agent counts too (it keeps calling the model on its
    account); a bare background command does not. On the loop."""
    return {sid: cid for cid, sid in _working(sub_agents) if sid}


def chat_status(chat_id: str) -> str:
    """The lane's live status: generating | awaiting_user | idle."""
    if not chat_id:
        return STATUS_IDLE

    # A pending permission outranks streaming — the turn is stalled on the
    # human either way.
    st = _chat_streaming_state.get(chat_id)
    if st and st.get("pending_permission"):
        return STATUS_AWAITING_USER

    live = interactive_session.find_live_for_chat(chat_id)
    if live is not None and (
            # The gates report "turn_open" before "permission_pending", and a
            # hook dialog usually appears MID-turn — probe the permission
            # table directly.
            _session_permission_requests.get(live.session_id)
            # A parked question (the AskUserQuestion / request_user_input
            # fold) CLOSES the turn while the chat waits on the human — the
            # chat row shows "needs your input"; the lane must match, not idle.
            or getattr(live, "question_parked", False)):
        return STATUS_AWAITING_USER

    return STATUS_GENERATING if any(cid == chat_id for cid, _sid in _working(False)) else STATUS_IDLE
