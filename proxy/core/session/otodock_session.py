"""otodock-CLI — proxy-side orchestrator for satellite-initiated sessions.

A user runs ``otodock claude|codex`` on their remote machine. The satellite asks
the proxy (over its existing authenticated WS) to open a session — see
``satellite_connection.handle_message`` ``local_session_open``. This module is the
authoritative gate + builder for that request:

  1. Re-derive identity from the MACHINE binding (``remote_machines.registered_by``)
     — NEVER trust an identity hint in the frame (the frame is shaped by whoever
     reached the satellite's local socket, i.e. the OS user).
  2. Authorize: machine paired (user-paired, not admin-paired in v1), owner exists,
     owner has a role on the agent.
  3. Create the chat row (``origin='otodock'`` + the absolute ``work_cwd``) so the
     session surfaces on the owner's chat page and is resumable from there.
  4. Build the AgentConfig pinned to THIS machine, interactive, with the
     per-session allowed root = the cwd (config_builder), and drive the
     EXISTING remote interactive ``start_session`` (the satellite spawns the PTY).
  5. Exempt the session from the idle reaper while the local terminal is attached.

The actual terminal bytes flow satellite↔otodock over the local socket (the proxy
just brokers identity/chat/spawn); the transcript persists via the normal remote
transcript/rollout forwarding, and the dashboard can view/resume it like any
interactive chat.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

logger = logging.getLogger(__name__)

# Only the interactive CLIs make sense for an otodock TUI session.
_ALLOWED_PATHS = ("claude-code-cli", "codex-cli")


class OtodockSessionError(Exception):
    """A user-surfaceable reason the local session could not be opened. The
    message is relayed verbatim to the otodock terminal (``local_session_error``)."""


async def _owner_for_machine(machine_id: str):
    """Resolve + validate the machine owner. Returns ``(machine, owner_sub, owner)``
    or raises :class:`OtodockSessionError` with a user-facing reason."""
    from storage import remote_store, database as db
    machine = await asyncio.to_thread(remote_store.get_remote_machine, machine_id)
    if not machine:
        raise OtodockSessionError("this machine is not paired with the platform")
    # The session runs as the machine's REGISTERED owner — for a user-paired
    # machine that's the user; for an admin-paired machine the admin who paired it
    # (i.e. the operator who set the machine up). Either is correct: access is
    # gated by the local socket's OS-level perms (0600, owned by the satellite's
    # OS user), so only someone with shell access to THIS machine can open it.
    # ``pairing_scope`` only governs file-sync fan-out, not who may start a local
    # interactive session. The owner-has-a-role-on-the-agent check still applies.
    owner_sub = machine.get("registered_by") or ""
    if not owner_sub:
        raise OtodockSessionError("this machine has no registered owner")
    owner = await asyncio.to_thread(db.get_user, owner_sub)
    if not owner:
        raise OtodockSessionError("the machine owner's account no longer exists")
    return machine, owner_sub, owner


def _model_for_path(agent: str, execution_path: str, requested: str) -> str:
    """Resolve a model VALID for the chosen CLI. The client's explicit model wins;
    else the agent default — but if that default isn't valid for this execution
    path (the common case: a Claude default model on a `codex` session, which
    Codex rejects), fall back to the layer's first concrete model. The dashboard
    avoids this because its frontend auto-picks a layer-compatible model; otodock
    has no frontend, so we enforce it here."""
    import config
    m = (requested or "").strip()
    if not m:
        try:
            m = config.resolve_agent_model(agent)
        except Exception:
            m = ""
    if m and execution_path not in config.get_model_layers(m):
        opts = [o["value"] for o in config.get_layer_models(execution_path) if o.get("value")]
        m = opts[0] if opts else ""
    return m


async def _owner_role_for_agent(owner_sub: str, owner: dict, agent: str) -> str:
    """The owner's effective role on ``agent`` (admins → 'admin'); '' = no access."""
    from storage import database as db, agent_store
    if not await asyncio.to_thread(agent_store.get_agent, agent):
        raise OtodockSessionError(f"agent '{agent}' not found")
    if owner.get("role") == "admin":
        return "admin"
    roles = await asyncio.to_thread(db.get_user_agent_roles, owner_sub)
    return (roles or {}).get(agent, "")


def _chat_ids_with_messages(chat_ids: list) -> set:
    """The subset of ``chat_ids`` that have ≥1 chat_message (a real turn) — used to
    drop empty otodock sessions (no conversation → no transcript → not resumable)
    from the picker. One batched query."""
    if not chat_ids:
        return set()
    from storage import database as db
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM chat_messages WHERE chat_id = ANY(%s)",
            (list(chat_ids),),
        ).fetchall()
    return {r["chat_id"] for r in rows}


async def list_local_sessions(machine_id: str, args: dict) -> dict:
    """otodock-CLI ``--resume``: the owner's recent resumable chats for an agent,
    filtered to the requested CLI. Returns ``{"chats": [...]}`` (most-recent first)."""
    from storage import database as db
    _machine, owner_sub, _owner = await _owner_for_machine(machine_id)
    agent = (args.get("agent") or "").strip()
    execution_path = (args.get("execution_path") or "").strip()
    if not agent:
        raise OtodockSessionError("no agent specified")
    # Scope-aware history owner — the SAME resolver the dashboard chat list uses
    # (api/agents/chats.py), so otodock sees the same chats: a personal agent → the
    # owner's own chats; a Shared-only (agent-scoped) agent → the ONE shared
    # history across every assigned user (e.g. an admin on an admin machine
    # resuming a chat another user started). Shared-only agents are blocked from
    # user-paired machines (see open_local_session), so on a user machine this
    # only ever yields the user's own chats.
    from core.session.visibility import chat_history_owner
    history_owner = await asyncio.to_thread(chat_history_owner, agent, owner_sub)
    chats = await asyncio.to_thread(db.list_chats, history_owner, agent, 80)
    # Keep every chat whose CLI session files actually live on THIS machine and is
    # loadable by `--resume` — REGARDLESS of whether it started via the otodock CLI
    # or the dashboard (any session that RAN here is resumable here):
    #  - ran on THIS machine (execution_target == machine_id). Platform ('local')
    #    / other-machine chats have their files elsewhere → DB-fallback (deferred).
    #  - has a persisted session_id (else `--resume` has no id to load).
    #  - matches the requested CLI.
    candidates = [
        c for c in chats
        if (c.get("execution_target") or "local") == machine_id
        and c.get("session_id")
        and (not execution_path or (c.get("execution_path") or "") == execution_path)
    ]
    # ...and has at least one real message (an empty session wrote no transcript →
    # `--resume` would 404). One batched count query.
    have_msgs = await asyncio.to_thread(
        _chat_ids_with_messages, [c["id"] for c in candidates]
    )
    out = []
    for c in candidates:
        if c["id"] not in have_msgs:
            continue
        out.append({
            "chat_id": c.get("id"),
            "title": c.get("title") or "",
            "updated_at": c.get("updated_at") or "",
            "origin": c.get("origin") or "dashboard",
            "work_cwd": c.get("work_cwd") or "",
        })
        if len(out) >= 20:
            break
    return {"chats": out}


async def _mark_attached_read(chat_id: str) -> None:
    """An otodock terminal is now attached to ``chat_id``: the attach replay
    just rendered the transcript — any answer pending unread included — on the
    local terminal, so clear the unread marker + sidebar dot under the chat's
    history-owner identity (the chat row's user_sub carries it already).
    Best-effort: never blocks the open."""
    from storage import database as db
    from services.notifications import notification_manager
    try:
        chat = await asyncio.to_thread(db.get_chat, chat_id)
        owner = (chat or {}).get("user_sub") or ""
        if not owner:
            return
        await asyncio.to_thread(db.mark_chat_read, chat_id, owner)
        notification_manager.broadcast_chat_read(
            owner, chat_id, agent=(chat or {}).get("agent") or "",
        )
    except Exception:
        logger.exception("otodock attach: mark-read failed for chat %s", chat_id[:8])


async def open_local_session(machine_id: str, args: dict) -> dict:
    """Open (or resume) an interactive session requested by an `otodock` CLI on
    ``machine_id``. Returns ``{"session_id", "chat_id"}`` on success. Raises
    :class:`OtodockSessionError` with a user-facing reason on any authz/validation
    failure (the caller relays it). Other exceptions bubble as internal errors."""
    from storage import database as db
    from core.config.config_builder import build_agent_config
    from core.session.session_manager import get_execution_layer
    from core.concurrency import acquire_chat_slot, release_chat_slot
    from core.session import interactive_session

    model = (args.get("model") or "").strip()
    perm_mode = (args.get("mode") or "default").strip() or "default"
    resume_chat_id = (args.get("resume_chat_id") or "").strip()
    # The local otodock terminal forwards its $TERM so the remote PTY renders to
    # match it; empty → the satellite keeps its xterm-256color default.
    term = (args.get("term") or "").strip()

    _machine, owner_sub, owner = await _owner_for_machine(machine_id)

    if resume_chat_id:
        # --- RESUME an existing chat (identity-checked) ----------------------
        chat = await asyncio.to_thread(db.get_chat, resume_chat_id)
        # Identity check against the SAME scope-aware owner the --resume picker
        # listed under (visibility.chat_history_owner): a Shared-only agent's
        # chats live under the synthetic agent::{slug} owner, not the human's
        # sub — comparing against the human made every Shared-only resume fail
        # with "not found".
        resume_owner = None
        if chat:
            from core.session.visibility import chat_history_owner
            resume_owner = await asyncio.to_thread(
                chat_history_owner, chat.get("agent") or "", owner_sub,
            )
        if not chat or chat.get("user_sub") != resume_owner:
            raise OtodockSessionError("that chat was not found")
        # Its CLI session files must live on THIS machine — otherwise pinning the
        # resume here would force-spawn elsewhere's session on this satellite (and
        # `--resume` would 404). Resume an off-machine chat from the dashboard.
        if (chat.get("execution_target") or "local") != machine_id:
            raise OtodockSessionError(
                "that chat did not run on this machine — resume it from the dashboard"
            )
        agent = chat.get("agent") or ""
        execution_path = chat.get("execution_path") or "claude-code-cli"
        chat_id = resume_chat_id
        session_id = chat.get("session_id") or str(uuid.uuid4())
        codex_thread_id = chat.get("codex_thread_id") or ""
        model = chat.get("model") or model  # continue with the chat's model
        resume = True
        work_cwd_for_build = ""  # build_agent_config recovers it from the chat row
        created_chat = False
    else:
        # --- FRESH session in the given folder -------------------------------
        agent = (args.get("agent") or "").strip()
        execution_path = (args.get("execution_path") or "claude-code-cli").strip()
        work_cwd = (args.get("cwd") or "").strip()
        if not work_cwd:
            raise OtodockSessionError("no working directory provided")
        chat_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        codex_thread_id = ""
        resume = False
        work_cwd_for_build = work_cwd
        created_chat = False

    if not agent:
        raise OtodockSessionError("no agent specified")
    if execution_path not in _ALLOWED_PATHS:
        raise OtodockSessionError(f"unsupported execution path: {execution_path}")

    role = await _owner_role_for_agent(owner_sub, owner, agent)
    if not role:
        raise OtodockSessionError(f"you do not have access to agent '{agent}'")

    # Shared-only (agent-scoped) agents have ONE shared chat history across all
    # users — they belong on ADMIN machines, never a personal one (a user machine
    # must hold only that user's own scoped chats). Block them on user-paired
    # machines (assignment is blocked too — this is defense-in-depth + a clearer
    # message than the generic not-assigned-to-this-machine error below).
    if (_machine.get("pairing_scope") or "") == "user":
        from core.session import visibility
        if await asyncio.to_thread(visibility.is_shared_only, agent):
            raise OtodockSessionError(
                f"agent '{agent}' is shared-only (agent-scoped) and can't run on a "
                f"personal machine — run it from an admin machine"
            )

    # The agent must actually be CONFIGURED to run on THIS machine (the user-level
    # override or agent default resolves to it). Without this, `build_agent_config`
    # below would FORCE the agent onto this machine via `pinned_target` (which only
    # checks reachability, not assignment) → `start_session` then hangs provisioning
    # an unassigned agent here and the otodock client times out ("the platform did
    # not respond"). For a FRESH session only — a resume is already validated to
    # this machine (the chat ran here). Mirrors the dashboard's per-machine agent
    # assignment: only agents you've assigned to a machine run there.
    if not resume:
        from storage import remote_store
        nat_target, _reason = await asyncio.to_thread(
            remote_store.resolve_execution_target, agent, owner_sub, role,
        )
        if (nat_target or "local") != machine_id:
            raise OtodockSessionError(
                f"agent '{agent}' is not set to run on this machine — assign it to "
                f"this machine in the agent's execution settings to use it here"
            )

    # Resolve a model valid for the chosen CLI (codex must not get a Claude model).
    model = await asyncio.to_thread(_model_for_path, agent, execution_path, model)

    # dual-control: if a session for this chat is ALREADY LIVE on this machine
    # (open in the dashboard, or held by another otodock terminal), ATTACH to it
    # instead of killing + re-spawning — the in-flight turn survives (the heart of
    # the dual-control design). Placed AFTER the role + shared-only + model checks so attach can't
    # skip authz, and BEFORE close_for_chat/spawn. Skipped when the satellite is
    # mid-reconnect (PTY-grace): the proxy still holds the handle as `alive`, but a
    # restarted satellite may have lost the PTY — attaching then would hang (no
    # `pty_sessions` entry), so we fall through to a clean re-spawn. Held under the
    # registry lock so the live session can't be closed (reap / exit / supersede)
    # between the find and the mark. For FRESH sessions chat_id is brand-new → no
    # live match → falls through to the normal spawn path. (The satellite also
    # re-checks `pty_sessions[session_id]` and errors the client if it vanished, so
    # the residual race after this check can never silently hang — see local_socket.)
    from core.remote.satellite_connection import get_connection_manager
    async with interactive_session._get_lock():
        live = interactive_session.find_live_for_chat(chat_id, target=machine_id)
        if live is not None and not get_connection_manager().is_pty_in_grace(machine_id):
            live.evict_viewer(reason="superseded_otodock")  # kick the dashboard viewer
            live.otodock_attached = True                      # otodock is now the controller
            live._note_activity()                             # don't hand back a stale-clock session
            logger.info(
                "otodock session attached-to-live: machine=%s agent=%s chat=%s session=%s",
                machine_id[:8], agent, chat_id, live.session_id[:8],
            )
        else:
            live = None
    if live is not None:
        await _mark_attached_read(chat_id)  # DB round-trips stay outside the lock
        return {"session_id": live.session_id, "chat_id": chat_id, "attach": True}

    if not resume:
        # Persist up-front so the session surfaces (origin badge) + is resumable in
        # the SAME folder later. source_type stays 'chat' (chat page, not the
        # agent-settings Conversations tab); origin drives only the badge.
        # Scope-aware owner: a Shared-only agent's chats live under the synthetic
        # agent::{slug} owner (the SAME owner the dashboard list + the --resume
        # picker query) — writing the human's sub here made these chats invisible
        # in the dashboard and unresumable.
        from core.session.visibility import chat_history_owner
        chat_owner = await asyncio.to_thread(chat_history_owner, agent, owner_sub)
        await asyncio.to_thread(
            db.create_chat, chat_id, chat_owner, agent, perm_mode, model,
            execution_path, "chat", "interactive", "otodock", work_cwd_for_build,
        )
        created_chat = True

    async def _cleanup_chat():
        if created_chat:
            await asyncio.to_thread(db.delete_chat, chat_id)

    # Take over any live session for this chat (single live process per chat —
    # resuming a chat that's live in the dashboard / a duplicate otodock run).
    await interactive_session.close_for_chat(chat_id)

    try:
        agent_cfg = await build_agent_config(
            agent_name=agent, user=owner, user_sub=owner_sub, user_role=role,
            permission_mode=perm_mode, client_type="dashboard",
            model=model, execution_path=execution_path, resume=resume,
            codex_thread_id=codex_thread_id, chat_id=chat_id, session_id=session_id,
            pinned_target=machine_id, work_cwd=work_cwd_for_build, is_otodock=True,
            term=term,
        )
    except Exception:
        await _cleanup_chat()
        raise

    # Must run on THIS machine — refuse rather than silently spawn elsewhere.
    if (agent_cfg.execution_target or "local") != machine_id:
        await _cleanup_chat()
        raise OtodockSessionError(
            "could not target this machine for the session (check the agent's "
            "execution target)"
        )
    agent_cfg.interactive = True

    adm = await acquire_chat_slot(session_id, target=machine_id)
    if not adm:
        await _cleanup_chat()
        raise OtodockSessionError(adm.user_message)
    try:
        layer = get_execution_layer(
            agent, execution_path=execution_path, user_sub=owner_sub,
            role=role, execution_target=machine_id,
        )
        await layer.start_session(session_id, agent_cfg)
    except Exception:
        release_chat_slot(session_id)
        await _cleanup_chat()
        raise

    # Exempt from the idle reaper while the local terminal is attached.
    sess = interactive_session.get(session_id)
    if sess is not None:
        sess.otodock_attached = True

    if resume:
        # `--resume` replays the chat's transcript into the local terminal —
        # whatever was pending unread has now been seen. Fresh chats skip it
        # (just created, nothing to read).
        await _mark_attached_read(chat_id)

    # Persist the session_id (so a later `otodock --resume` can `--resume` it
    # natively; without it the chat row keeps session_id=NULL and resume invents a
    # random id → "No conversation found") AND the execution_target = this machine
    # (so the resume picker can show every chat whose CLI session files live HERE —
    # otodock chats + dashboard chats that ran on this same satellite).
    if not resume:
        await asyncio.to_thread(
            db.update_chat, chat_id, session_id=session_id, execution_target=machine_id,
        )

    logger.info(
        "otodock session %s: machine=%s agent=%s path=%s chat=%s session=%s",
        "resumed" if resume else "opened",
        machine_id[:8], agent, execution_path, chat_id, session_id[:8],
    )
    return {"session_id": session_id, "chat_id": chat_id}


def detach_local_session(session_id: str) -> None:
    """The local `otodock` terminal detached from this session — it disconnected on
    its own (orphan lifecycle), OR the dashboard took it over (dual-control:
    this is the CONFIRMATION of a ``pty_local_detach``, or the fallback timer when
    the satellite never confirmed). Clear the controller flag so the dashboard's
    input/resize apply again + the session reaps on the normal idle timeout. If a
    dashboard viewer is now in control, give it a clean re-render at ITS size via
    the reconnect rail (xterm reset + scrollback replay; the frontend then
    re-sends its resize so the remote TUI reflows for future output). Idempotent
    (best-effort; the PTY itself stays alive)."""
    from core.session import interactive_session
    sess = interactive_session.get(session_id)
    if sess is None or not sess.otodock_attached:
        return
    sess.otodock_attached = False
    timer = sess._otodock_kick_timer
    if timer is not None:
        with contextlib.suppress(Exception):
            timer.cancel()
        sess._otodock_kick_timer = None
    if sess.has_viewer:
        sess.notify_status("reconnected")
    # Server prompts held for the satellite injection path now belong to the
    # proxy path (the backstop would get there within seconds; this is prompt).
    if sess._prompt_queue:
        sess._loop.create_task(sess._try_drain_prompt_queue())
