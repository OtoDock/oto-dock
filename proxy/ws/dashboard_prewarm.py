"""The detached pre-warm: a session spawned OUTSIDE any dashboard connection
(the wake-word handler fires ``POST /v1/duplex/prewarm`` before the page
navigates to the chat), the rollback that gives an abandoned pre-warm back
its seat and slot, and the model-foreign check both pre-warm paths share.
Module-level functions, not controller methods; the ``WarmupController``
mixin (ws/dashboard_warmup.py) calls the rollback pair and the check, the
duplex router calls the spawn. Behavior is pinned by
tests/session/test_ws_prewarm_seat.py and tests/audio/test_duplex_session.py.

Import ``ws.dashboard`` before this module (as the warmup module needs): the
two helpers it takes from there are defined above the point where
``ws/dashboard.py`` assembles its mixins.
"""

import asyncio
import contextlib
import logging
import uuid

import config
from core import placement
from storage.pg import run_db
from core.session.session_state import get_user_tz, set_session_user_tz
from core.session.session_manager import get_execution_layer, resolve_execution_path
from core.config.config_builder import build_agent_config, release_config_seat
from services.engines import subscription_pool
# Imported by ws/dashboard.py AFTER its helpers are defined (through the
# warmup mixin) — safe intra-unit circularity (see the class assembly there).
from ws.dashboard import _resolve_session_interactive
from core.session import session_kind
from auth.providers import acting_role_of

logger = logging.getLogger("claude-proxy")


_ROLLBACK_TASKS: set[asyncio.Task] = set()


async def _pre_warmup_rollback(sid: str, build, agent_cfg, started: bool, layer) -> None:
    """Give back what an abandoned pre-warm holds: the chat slot, and either
    the session (spawned and bound — closing it releases the seat) or the
    seat its config acquired that nothing bound. ``build`` is the shielded
    config build a cancel interrupted: awaited first, the seat lives in its
    result. A cancel inside ``start_session`` can land after the bind and
    before ``started`` is set, so the binding decides, not the flag."""
    if agent_cfg is None and build is not None:
        with contextlib.suppress(Exception):
            agent_cfg = await build
    from core.concurrency import release_chat_slot
    release_chat_slot(sid)
    if started or subscription_pool.session_bound(sid):
        if layer is not None:
            with contextlib.suppress(Exception):
                await layer.close_session(sid)
    elif agent_cfg is not None:
        release_config_seat(sid, agent_cfg)


def _schedule_pre_warmup_rollback(sid: str, build, agent_cfg, started: bool, layer) -> None:
    task = asyncio.create_task(_pre_warmup_rollback(sid, build, agent_cfg, started, layer))
    _ROLLBACK_TASKS.add(task)
    task.add_done_callback(_ROLLBACK_TASKS.discard)


async def spawn_detached_prewarm(
    *, agent: str, user: dict, user_sub: str,
    requested_model: str = "", permission_mode: str = "default",
) -> str | None:
    """Pre-warm a session OUTSIDE any WS connection (wake-word detection fires
    this over HTTP before the dashboard navigates to the chat).

    Mirrors ``_handle_pre_warmup``'s spawn path minus the connection-scoped
    bookkeeping: the session is registered in the global pre-warm registry
    (with its model), and the next dashboard warmup for the same
    (user, agent, model, role, exec_path) claims it via ``claim_by_key`` in
    ``_spawn_tail``; unclaimed → the TTL reaper frees it. Returns the
    session_id, or None when skipped (remote/interactive) or not spawnable.
    """
    role = await run_db(acting_role_of, user_sub, agent, fallback_user=user)
    new_sid = str(uuid.uuid4())
    agent_cfg = None
    layer = None
    try:
        exec_path = resolve_execution_path(agent, "")
        if _model_foreign_to_engine(requested_model, exec_path):
            logger.info(
                f"detached pre-warm: skipped (model {requested_model} is not a "
                f"{exec_path} model) agent={agent}"
            )
            return None
        # Room for this session and one more, nobody waiting, the person
        # below their cap; otherwise skipped without a word.
        from core import concurrency
        if not concurrency.prewarm_allowed(exec_path, user_sub=user_sub):
            logger.info(f"detached pre-warm: skipped (no room for two) agent={agent}")
            return None
        agent_cfg = await build_agent_config(
            agent_name=agent, user=user, user_sub=user_sub,
            user_role=role, permission_mode=permission_mode,
            client_type=session_kind.DASHBOARD.name, resume=False,
            model=requested_model,
            execution_path=exec_path,
            session_id=new_sid,
        )
        # Same skips as the WS pre-warm: a remote pre-warm spends the
        # satellite's budget for a chat that may never come; an interactive
        # cold-start never reuses a -p pre-warm. Each skip returns the seat
        # the build acquired.
        if not placement.is_local(agent_cfg.execution_target):
            logger.info(f"detached pre-warm: skipped (remote target) agent={agent}")
            release_config_seat(new_sid, agent_cfg)
            return None
        if _resolve_session_interactive(agent_cfg):
            logger.info(f"detached pre-warm: skipped (interactive) agent={agent}")
            release_config_seat(new_sid, agent_cfg)
            return None
        from core.concurrency import acquire_chat_slot
        adm = await acquire_chat_slot(new_sid, execution_path=agent_cfg.execution_path,
                                      user_sub=user_sub, speculative=True)
        if not adm:
            logger.info(f"detached pre-warm: no slot ({adm.reason})")
            release_config_seat(new_sid, agent_cfg)
            return None
        # The layer must match the target the build resolved (local here):
        # resolving again without it could hand back the remote layer for an
        # agent whose machine the build had already fallen back from.
        layer = get_execution_layer(
            agent, execution_path=exec_path, user_sub=user_sub, role=role,
            execution_target=agent_cfg.execution_target,
        )
        await layer.start_session(new_sid, agent_cfg)
        _tz = get_user_tz(user_sub)
        if _tz:
            set_session_user_tz(new_sid, _tz)
        from core.session import prewarm_session_registry as _prewarm
        await _prewarm.register(new_sid, agent=agent, user_sub=user_sub,
                                role=role,
                                exec_path=resolve_execution_path(agent, ""),
                                model=agent_cfg.model)
        logger.info(
            f"detached pre-warm: created session={new_sid[:8]}, agent={agent}, "
            f"model={agent_cfg.model}, role={role}"
        )
        return new_sid
    except Exception as e:
        from core.sandbox.session_config_dir import AgentStateRefused
        if isinstance(e, AgentStateRefused):
            logger.info(f"detached pre-warm skipped (below the editor tier) agent={agent}")
        else:
            logger.error(f"detached pre-warm failed: {e}", exc_info=True)
        from core.concurrency import release_chat_slot
        release_chat_slot(new_sid)
        if layer is not None and await layer.is_session_alive(new_sid):
            # Spawned and bound, then the registration after it failed: the
            # session owns the seat — close it, which releases it.
            with contextlib.suppress(Exception):
                await layer.close_session(new_sid)
        elif agent_cfg is not None:
            release_config_seat(new_sid, agent_cfg)
        return None


def _model_foreign_to_engine(model: str, execution_path: str) -> bool:
    """True when ``model`` is KNOWN to run on other engines only — a
    pre-warm frame can pair the page's model with an engine the resolver
    then overrides (seen on T1: a Claude model on a Codex pre-warm, which
    filtered the Codex candidates by the wrong provider and refused). An
    unknown model (custom row, local endpoint) is trusted."""
    if not model or not execution_path:
        return False
    try:
        layers = config.get_model_layers(model)
    except Exception:
        return False
    return bool(layers) and execution_path not in layers
