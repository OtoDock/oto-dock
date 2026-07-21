"""Harness for characterization tests of ``ws.dashboard.ws_dashboard_handler``.

Drives the REAL handler coroutine end-to-end: a scriptable fake WebSocket on
one side, the real test database + in-memory registries (warmup_registry,
stream pump, session_state, notification_manager) in the middle, and a
scriptable fake execution layer at the bottom. Only genuinely heavy or
non-deterministic collaborators are stubbed — always at THEIR OWN modules
(``session_manager._cli_layer``, ``config_builder`` internals,
``core.concurrency``), never by patching names inside ``ws.dashboard`` — so
the suite keeps passing unchanged while the handler itself is decomposed.

Starlette's synchronous TestClient deadlocks on a concurrent reader+pump
handler (see tests/audio/test_audio_tts_ws.py), so tests call the coroutine
directly under ``asyncio.run`` via ``run_ws_scenario``.
"""

import asyncio
import contextlib
import json
import uuid
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

# Every stub resolves models to this so frames are deterministic.
TEST_MODEL = "test-model"

_DISCONNECT = object()


class _Any:
    def __repr__(self):  # pragma: no cover - repr only used in assert messages
        return "<ANY>"


#: Wildcard for volatile frame fields (ids, timestamps) in ``assert_frame``.
ANY = _Any()


def assert_frame(frame: dict, expected: dict) -> dict:
    """Golden-master frame check: EXACT key set, exact values except ``ANY``."""
    assert set(frame) == set(expected), (
        f"frame keys {sorted(frame)} != expected keys {sorted(expected)}\n"
        f"frame: {frame}"
    )
    for key, want in expected.items():
        if want is ANY:
            continue
        assert frame[key] == want, (
            f"frame[{key!r}] = {frame[key]!r} != {want!r}\nframe: {frame}"
        )
    return frame


class FakeDashboardWebSocket:
    """Scriptable stand-in for the starlette WebSocket the handler drives.

    Server side (called by the handler): ``accept`` / ``receive_text`` /
    ``send_json`` / ``close`` — plus ``cookies`` for the auth gate.
    Client side (called by tests): ``client_send`` enqueues an inbound frame,
    ``client_disconnect`` makes the next receive raise ``WebSocketDisconnect``,
    ``next_frame``/``expect`` await recorded outbound frames in order.
    """

    def __init__(self, cookie: str | None):
        self.cookies: dict[str, str] = {}
        if cookie is not None:
            self.cookies["session"] = cookie
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.sent: list[dict] = []
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._outbound: asyncio.Queue = asyncio.Queue()

    # -- server-side surface -------------------------------------------------
    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        item = await self._inbound.get()
        if item is _DISCONNECT:
            # A disconnected socket stays disconnected for any later receive.
            self._inbound.put_nowait(_DISCONNECT)
            raise WebSocketDisconnect(code=1001)
        return item

    async def send_json(self, data: dict) -> None:
        # JSON round-trip: enforces serializability exactly like the real
        # socket AND snapshots the payload against later mutation.
        frame = json.loads(json.dumps(data))
        self.sent.append(frame)
        self._outbound.put_nowait(frame)
        # A real socket's send awaits network I/O; concurrent tasks (the pump)
        # get scheduled across sends. Without this yield the handler runs
        # send-to-send synchronously — orderings no real deployment has.
        await asyncio.sleep(0)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    # -- client-side helpers -------------------------------------------------
    def client_send(self, obj: dict) -> None:
        self._inbound.put_nowait(json.dumps(obj))

    def client_send_raw(self, raw: str) -> None:
        self._inbound.put_nowait(raw)

    def client_disconnect(self) -> None:
        self._inbound.put_nowait(_DISCONNECT)

    async def next_frame(self, timeout: float = 3.0) -> dict:
        return await asyncio.wait_for(self._outbound.get(), timeout)

    async def expect(self, expected: dict, timeout: float = 3.0) -> dict:
        """Await the next frame and golden-master check it."""
        return assert_frame(await self.next_frame(timeout), expected)

    def no_more_frames(self) -> None:
        assert self._outbound.empty(), (
            f"unexpected extra frames: {list(self._outbound._queue)}"
        )


class FakeExecutionLayer:
    """Scriptable execution layer (duck-typed against ``ExecutionLayer``).

    ``turn_events`` scripts what ``send_message`` yields: a list of
    ``CommonEvent``s replayed for every turn, or a callable
    ``(sid, prompt) -> iterable[CommonEvent]``. ``start_gate`` (when set)
    blocks ``start_session`` until the test releases it — makes the
    backgrounded warmup spawn deterministic relative to dispatcher frames.
    """

    def __init__(self):
        # The REAL CLI capability descriptor — mode/model/control handlers
        # branch on it, so a hand-rolled subset would drift.
        from core.layers.cli.layer import _CLI_CAPABILITIES
        self.capabilities = _CLI_CAPABILITIES
        self.alive: set[str] = set()
        self.dead_processes: set[str] = set()
        self.resumable: set[str] = set()
        self.started: list[tuple[str, object]] = []
        self.messages: list[tuple[str, str, dict]] = []
        self.mode_changes: list[tuple[str, str]] = []
        self.model_changes: list[tuple[str, str]] = []
        self.control_requests: list[tuple[str, str, dict]] = []
        self.closed_sessions: list[str] = []
        self.aborted: list[str] = []
        self.turn_events = []
        self.start_gate: asyncio.Event | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        # Wedged-pump reap scripting (resume_chat's stall path).
        self.idle_seconds: dict[str, float] = {}
        self.severed: set[str] = set()
        self.probed_dead: set[str] = set()
        self.prepared_resume: list[str] = []
        # Mid-turn steering scripting (N2): the real default is unsupported.
        self.steered: list[tuple[str, str]] = []
        self.steer_accepts = False
        # Graceful-abort scripting (N1): the real default is a hard kill.
        self.abort_graceful = False

    async def start_session(self, sid: str, agent_cfg) -> None:
        if self.start_gate is not None:
            await self.start_gate.wait()
        self.started.append((sid, agent_cfg))
        self.alive.add(sid)
        # Contract mirrored from CLIExecutionLayer.start_session: the layer
        # stamps the session's mode (+ security context) from the built config.
        from core.session.session_state import (
            set_session_mode, set_session_security,
        )
        set_session_mode(sid, agent_cfg.permission_mode)
        if getattr(agent_cfg, "security_context", None) is not None:
            set_session_security(sid, agent_cfg.security_context)

    async def is_session_alive(self, sid: str) -> bool:
        return sid in self.alive

    async def is_session_process_dead(self, sid: str) -> bool:
        return sid in self.dead_processes

    async def can_resume_session(self, sid: str, *, agent_name: str = "",
                                 username: str = "") -> bool:
        return sid in self.resumable

    async def close_session(self, sid: str) -> None:
        self.closed_sessions.append(sid)
        self.alive.discard(sid)

    async def abort(self, sid: str) -> bool:
        self.aborted.append(sid)
        return self.abort_graceful

    async def steer(self, sid: str, text: str) -> bool:
        self.steered.append((sid, text))
        return self.steer_accepts

    async def compact(self, sid: str) -> dict | None:
        return None

    async def prepare_resume(self, sid: str) -> None:
        self.alive.discard(sid)
        self.prepared_resume.append(sid)

    def remote_stream_severed(self, sid: str) -> bool:
        return sid in self.severed

    def session_idle_seconds(self, sid: str) -> float | None:
        return self.idle_seconds.get(sid)

    async def probe_session_process_dead(self, sid: str) -> bool:
        return sid in self.probed_dead or sid in self.dead_processes

    def session_lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    async def change_mode(self, sid: str, mode: str) -> None:
        self.mode_changes.append((sid, mode))
        from core.session.session_state import set_session_mode
        set_session_mode(sid, mode)

    async def change_model(self, sid: str, model: str) -> None:
        self.model_changes.append((sid, model))

    async def send_control_request(self, sid: str, subtype: str, **kwargs) -> None:
        self.control_requests.append((sid, subtype, kwargs))

    async def send_message(self, sid: str, prompt: str, **kwargs):
        self.messages.append((sid, prompt, kwargs))
        events = self.turn_events
        if callable(events):
            events = events(sid, prompt)
        # Real layers suspend on subprocess/pipe I/O between events; an
        # instantaneous generator would let the whole turn (pump included)
        # finish inside one scheduler window — orderings production never
        # produces (e.g. live_state gone before the viewer checks it).
        if hasattr(events, "__aiter__"):
            async for ev in events:
                await asyncio.sleep(0)
                yield ev
        else:
            for ev in events:
                await asyncio.sleep(0)
                yield ev


class FakeInteractiveSession:
    """Duck-typed stand-in for ``interactive_session.InteractiveSession`` —
    register it via ``monkeypatch.setitem(interactive_session._sessions, sid,
    fake)`` to drive the PTY-viewer paths."""

    def __init__(self, session_id: str, chat_id: str, *,
                 scrollback: bytes = b"", tui_theme: str = "dark"):
        self.session_id = session_id
        self.chat_id = chat_id
        self.alive = True
        self._turn_open = False  # read by streaming_chat_ids (connect snapshot)
        self.target = "local"
        self.tui_theme = tui_theme
        self.pty = None
        self.otodock_attached = False
        self._otodock_kick_timer = None
        self.scrollback = scrollback
        self.inputs: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.submitted: list[str] = []
        self.pending_seed: str | None = None
        self.closed_reasons: list[str] = []
        self.on_perm_event = None
        self.on_close = None
        self.on_status = None
        self.output_listener = None
        self.evict_cb = None

    @property
    def turn_open(self) -> bool:
        # Mirrors the real class's property (read by the resume/warmup
        # re-attach paths for the warmup_ready turn_open field).
        return self._turn_open

    def add_output_listener(self, listener, on_evict=None) -> bytes:
        self.output_listener = listener
        self.evict_cb = on_evict
        return self.scrollback

    def remove_output_listener(self, listener) -> None:
        if self.output_listener is listener:
            self.output_listener = None

    def write_input(self, data: bytes) -> None:
        self.inputs.append(data)

    def deliver_dashboard_input(self, data: bytes, composer: bool = False) -> None:
        # Mirror the real router's pass-through shape (the composer hold is
        # unit-tested on the real class in test_interactive_session).
        self.write_input(data)

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    def submit_prompt(self, text: str) -> None:
        self.submitted.append(text)

    def set_pending_seed(self, digest: str) -> None:
        self.pending_seed = digest

    async def close(self, *, reason: str = "closed") -> None:
        self.alive = False
        self.closed_reasons.append(reason)


def stub_dashboard_seams(monkeypatch, fake_layer: FakeExecutionLayer):
    """Patch the handler's heavy collaborators at their own modules.

    Returns a namespace recording concurrency-slot traffic. Everything else
    (DB stores, warmup_registry, stream pump, session_state,
    notification_manager, JWT validation) runs REAL against the test DB.
    """
    from core.session import session_manager as sm
    monkeypatch.setattr(sm, "_cli_layer", fake_layer)
    monkeypatch.setitem(sm._LAYERS, "claude-code-cli", fake_layer)

    from storage import remote_store
    monkeypatch.setattr(remote_store, "resolve_execution_target",
                        lambda *a, **k: ("local", None))
    monkeypatch.setattr(remote_store, "get_target_metadata",
                        lambda *a, **k: ("local", "Local"))

    from core.config import config_builder as cb
    monkeypatch.setattr(cb.mcp_registry, "build_session_mcp_config",
                        lambda *a, **k: (None, {}, {}, {}, set()))
    monkeypatch.setattr(cb.mcp_registry, "get_agent_mcps", lambda *a, **k: [])

    async def _no_dynamic_contexts(*a, **k):
        return []
    monkeypatch.setattr(cb.dynamic_context, "get_dynamic_contexts",
                        _no_dynamic_contexts)
    monkeypatch.setattr(cb.subscription_pool, "resolve_subscription_env",
                        lambda *a, **k: ("test-sub", {}))

    import config as cfg
    monkeypatch.setattr(cfg, "build_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(cfg, "get_cli_model", lambda *a, **k: TEST_MODEL)
    monkeypatch.setattr(cfg, "get_cli_effort", lambda *a, **k: "")

    import core.sandbox.session_config_dir as scd

    def _fake_persistent_dir(agent_name, *, username="", scope="user"):
        d = cfg.AGENTS_DIR / agent_name / ".test-persistent"
        d.mkdir(parents=True, exist_ok=True)
        return d
    monkeypatch.setattr(scd, "ensure_persistent_claude_dir", _fake_persistent_dir)
    monkeypatch.setattr(scd, "ensure_persistent_codex_dir", _fake_persistent_dir)

    # The cross-layer model guard consults the layer's served models.
    from storage import subscription_store
    monkeypatch.setattr(subscription_store, "list_models",
                        lambda path: [{"model_id": TEST_MODEL}])

    # Turn-start token guard: no subscription bound to fake sessions.
    from services.engines import subscription_pool as sub_pool
    monkeypatch.setattr(sub_pool, "session_token_expiry_ms", lambda sid: None)

    # LLM title upgrade + memory nudge are fired per turn — keep frames and
    # prompts deterministic (no LLM call, no nudge suffix).
    from services import title_generator
    async def _no_title(*a, **k):
        return None
    monkeypatch.setattr(title_generator, "request_chat_title", _no_title)
    from services.memory import memory_nudge
    monkeypatch.setattr(memory_nudge, "maybe_nudge", lambda sid: "")

    # Concurrency: always admit, record traffic for assertions.
    import core.concurrency as conc
    slots = SimpleNamespace(acquired=[], released=[])

    async def _admit(sid, **kwargs):
        slots.acquired.append((sid, kwargs))
        return conc.Admission(True)
    monkeypatch.setattr(conc, "acquire_chat_slot", _admit)
    monkeypatch.setattr(conc, "release_chat_slot",
                        slots.released.append)
    return slots


def session_cookie(sub: str = "user-admin", email: str = "admin@test.com",
                   name: str = "Admin User", role: str = "admin") -> str:
    """A REAL session-cookie JWT for a conftest-seeded user."""
    from auth.providers import create_session_jwt
    return create_session_jwt(sub, email, name, role)


def set_username(sub: str, username: str) -> None:
    """User-scoped chats require a username slug (conftest seeds none)."""
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username = %s WHERE sub = %s",
                     (username, sub))
        conn.commit()


def make_test_agent(slug: str | None = None, **kwargs) -> str:
    """Create a real agent row (+ dirs under the test AGENTS_DIR)."""
    from storage import agent_store
    slug = slug or f"wsdash-{uuid.uuid4().hex[:8]}"
    agent_store.create_agent(slug, "WS Dash Test", **kwargs)
    return slug


@contextlib.asynccontextmanager
async def dashboard_connection(cookie: str | None):
    """Run ``ws_dashboard_handler`` against a fake socket for the block's
    duration; on exit disconnect (if still open) and require a clean handler
    exit — a hung handler is a test failure, not a hang."""
    from ws.dashboard import ws_dashboard_handler

    ws = FakeDashboardWebSocket(cookie)
    task = asyncio.create_task(ws_dashboard_handler(ws))
    try:
        yield ws
    finally:
        if not task.done():
            ws.client_disconnect()
        try:
            await asyncio.wait_for(task, 5.0)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise AssertionError(
                "ws_dashboard_handler did not exit after disconnect"
            )


async def drain_startup(ws: FakeDashboardWebSocket) -> None:
    """Consume the three connect-time frames every authenticated socket gets.

    Golden-frame change note: ``chat_status_snapshot`` was ADDED deliberately —
    the connect-time authoritative "streaming right now" set that makes the
    sidebar live-dots re-derivable after missed frames (empty set when nothing
    is streaming, which still clears stale client dots)."""
    await ws.expect({"type": "notification_count", "count": 0})
    await ws.expect({"type": "satellite_update_sync", "inflight": []})
    await ws.expect({"type": "chat_status_snapshot", "chat_ids": []})


async def warm_new_chat(ws: FakeDashboardWebSocket, layer: FakeExecutionLayer,
                        slug: str, *, text: str = "") -> tuple[str, str]:
    """Drive a full new-chat warmup; returns (chat_id, session_id).

    Holds the backgrounded spawn on a gate until the dispatcher's
    notification_count frame is consumed, so the warmup_started →
    notification_count → warmup_ready order is deterministic (without the
    gate the spawn tail races the unread-count thread hop)."""
    caller_gate = layer.start_gate
    if caller_gate is None:
        layer.start_gate = asyncio.Event()
    msg: dict = {"type": "warmup", "agent": slug}
    if text:
        msg["text"] = text
    ws.client_send(msg)
    started = await ws.expect({
        "type": "warmup_started", "chat_id": ANY, "agent": slug,
        "execution_path": "claude-code-cli", "execution_target": "local",
    })
    await ws.expect({"type": "notification_count", "count": 0})
    if caller_gate is None:
        layer.start_gate.set()
    ready = await ws.expect({
        "type": "warmup_ready", "session_id": ANY,
        "chat_id": started["chat_id"], "mode": "default",
        "model": TEST_MODEL, "execution_path": "claude-code-cli",
        "execution_target": "local", "fallback_reason": None,
        "offline_machine_name": "", "interactive": False,
    })
    if caller_gate is None:
        layer.start_gate = None
    return started["chat_id"], ready["session_id"]


async def sync_dispatch(ws: FakeDashboardWebSocket) -> None:
    """Barrier: the main loop dispatches serially, so a pong proves every
    previously sent client message has been fully processed — use before
    asserting side effects that happen AFTER a frame was emitted."""
    ws.client_send({"type": "ping"})
    await ws.expect({"type": "pong"})


def run_ws_scenario(scenario, timeout: float = 15.0) -> None:
    """Run an async scenario to completion on a fresh loop (audio-WS pattern)."""
    asyncio.run(asyncio.wait_for(scenario(), timeout))
