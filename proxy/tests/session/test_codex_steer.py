"""Mid-turn steering for the Codex app-server session (N-series).

``turn/steer`` carries the ACTIVE turn id as ``expectedTurnId``: accept means
the input is delivered exactly-once into the running turn (codex extends the
turn to consume pending input at the sampling-round boundary and persists it
to the rollout at accept time — see the session-N plan's pre-build probe).
Reject (no live turn, review/compaction turn, dead client) returns False and
the caller falls back to the post-turn queue.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/session/test_codex_steer.py -q
"""
import pytest

from core.layers.cli.layer import CLIExecutionLayer
from core.layers.codex.app_server_client import AppServerError
from core.layers.codex.session import CodexAppServerSession


class _SteerClient:
    def __init__(self, *, error: str | None = None):
        self.requests: list[tuple[str, dict]] = []
        self.error = error
        self.is_alive = True

    async def request(self, method, params=None, *, timeout=None):
        self.requests.append((method, params))
        if self.error:
            raise AppServerError(self.error)
        return {"turnId": params["expectedTurnId"]}


def _mk(*, error: str | None = None, sandbox_mode: str = "workspace-write") -> CodexAppServerSession:
    s = CodexAppServerSession(
        session_id="s1", agent_name="a", model="gpt-5.4",
        sandbox_mode=sandbox_mode, working_dir="/tmp",
        config_dir="/tmp/.codex", thread_id="thr-1",
    )
    s._started = True
    s._client = _SteerClient(error=error)
    s._current_turn_id = "turn-7"
    return s


def test_codex_capabilities_expose_plan_mode():
    """Plan mode needs BOTH the capability flag (FE shows the toggle) AND "plan"
    in permission_modes (the backend mode-change validator rejects anything not
    listed). Missing the latter silently drops mode_change('plan') for codex."""
    from core.layers.codex.layer import _CODEX_CAPABILITIES
    assert _CODEX_CAPABILITIES.supports_plan_mode is True
    assert "plan" in _CODEX_CAPABILITIES.permission_modes


class TestCollaborationMode:
    """Plan collaboration mode is DERIVED from the platform mode: `plan` is the
    only mode that maps codex to a read-only sandbox, so read-only ⟺ plan. The
    per-turn override uses the MINIMAL {mode:...} (not the full preset, whose
    reasoning_effort would clobber the user's effort)."""

    def test_read_only_is_plan(self):
        assert _mk(sandbox_mode="read-only")._collaboration_mode() == {"mode": "plan"}

    def test_workspace_write_is_default(self):
        assert _mk(sandbox_mode="workspace-write")._collaboration_mode() == {"mode": "default"}

    def test_danger_full_access_is_default(self):
        assert _mk(sandbox_mode="danger-full-access")._collaboration_mode() == {"mode": "default"}


class TestCodexSteer:
    @pytest.mark.asyncio
    async def test_steers_into_live_turn(self):
        s = _mk()
        assert await s.steer("do X too") is True
        (method, params), = s._client.requests
        assert method == "turn/steer"
        assert params["threadId"] == "thr-1"
        assert params["expectedTurnId"] == "turn-7"
        assert params["input"] == [
            {"type": "text", "text": "do X too", "text_elements": []},
        ]

    @pytest.mark.asyncio
    async def test_no_live_turn_refuses_without_rpc(self):
        s = _mk()
        s._current_turn_id = None
        assert await s.steer("x") is False
        assert s._client.requests == []

    @pytest.mark.asyncio
    async def test_rejection_returns_false(self):
        # The daemon rejects steers between turn/start and turn/started, after
        # turn end, and for review/compaction turns — all surface as an RPC
        # error and must fall back to the queue, never raise.
        s = _mk(error="-32600 no active turn to steer")
        assert await s.steer("x") is False

    @pytest.mark.asyncio
    async def test_dead_client_refuses_without_rpc(self):
        s = _mk()
        s._client.is_alive = False
        assert await s.steer("x") is False
        assert s._client.requests == []


class TestLayerSteerDefault:
    @pytest.mark.asyncio
    async def test_non_steering_layers_return_false(self):
        # ExecutionLayer.steer defaults to unsupported — Claude stream-json has
        # no upstream steering; the CLI layer inherits the default.
        assert await CLIExecutionLayer().steer("sid-x", "text") is False


# ---------------------------------------------------------------------------
# Manual compaction — thread/compact/start between turns (N3).
# ---------------------------------------------------------------------------

class _CompactClient:
    """request() captures calls; on thread/compact/start it feeds the
    session's temporary default consumer (stand-in for the router)."""

    def __init__(self, *, error: str | None = None):
        self.requests: list[tuple[str, dict]] = []
        self.is_alive = True
        self.error = error
        self.session = None            # wired by _mk_compact
        self.feed: list[tuple] = []

    async def request(self, method, params=None, *, timeout=None):
        self.requests.append((method, params))
        if self.error:
            raise AppServerError(self.error)
        if method == "thread/compact/start":
            for item in self.feed:
                self.session._default_consumer.put_nowait(item)
        return {}


def _mk_compact(*, error: str | None = None) -> CodexAppServerSession:
    s = CodexAppServerSession(
        session_id="s1", agent_name="a", model="gpt-5.4",
        sandbox_mode="workspace-write", working_dir="/tmp",
        config_dir="/tmp/.codex", thread_id="thr-1",
    )
    s._started = True
    client = _CompactClient(error=error)
    client.session = s
    s._client = client
    return s


class TestCodexCompact:
    @pytest.mark.asyncio
    async def test_compacts_on_canonical_item(self):
        # v2 (0.142+): completion = the contextCompaction ITEM; the post size
        # arrives behind it (core recomputes usage after the history swap).
        s = _mk_compact()
        s._client.feed = [
            ("turn/started", {}),
            ("item/completed", {"item": {"id": "i1", "type": "contextCompaction"}}),
            ("thread/tokenUsage/updated", {"tokenUsage": {
                "last": {"inputTokens": 1234},
                "modelContextWindow": 200000,
            }}),
            ("turn/completed", {"turn": {"id": "t1", "status": "completed"}}),
        ]
        assert await s.compact() == {"post_tokens": 1234}
        (method, params), = s._client.requests
        assert method == "thread/compact/start"
        assert params == {"threadId": "thr-1"}
        assert s._default_consumer is None       # consumer slot released

    @pytest.mark.asyncio
    async def test_legacy_thread_compacted_completes(self):
        s = _mk_compact()
        s._client.feed = [
            ("thread/compacted", {}),
            ("turn/completed", {"turn": {"id": "t1", "status": "completed"}}),
        ]
        assert await s.compact() == {"post_tokens": None}

    @pytest.mark.asyncio
    async def test_turn_end_without_item_is_failure(self):
        s = _mk_compact()
        s._client.feed = [
            ("turn/started", {}),
            ("turn/completed", {"turn": {"id": "t1", "status": "completed"}}),
        ]
        assert await s.compact() is None

    @pytest.mark.asyncio
    async def test_refuses_while_turn_active(self):
        s = _mk_compact()
        s._current_turn_id = "turn-1"
        assert await s.compact() is None
        assert s._client.requests == []

    @pytest.mark.asyncio
    async def test_stream_error_returns_none(self):
        s = _mk_compact()
        s._client.feed = [
            ("error", {"error": {"message": "boom", "willRetry": False}}),
        ]
        assert await s.compact() is None
        assert s._default_consumer is None

    @pytest.mark.asyncio
    async def test_rpc_rejection_returns_none(self):
        s = _mk_compact(error="not supported")
        assert await s.compact() is None
        assert s._default_consumer is None


class TestCompactedTranslation:
    def test_thread_compacted_is_first_class_context_compact(self):
        from core.events.common_events import CONTEXT_COMPACT
        from core.layers.codex.session import CodexEvent
        from core.layers.codex.translator import CodexEventTranslator

        tr = CodexEventTranslator(model="m")
        events = tr.translate(CodexEvent("thread/compacted", {}))
        assert [e.type for e in events] == [CONTEXT_COMPACT]
        assert events[0].data["phase"] == "completed"
        assert events[0].data["trigger"] == "auto"

    def test_context_compaction_item_is_first_class(self):
        # The canonical v2 signal — auto-compaction arrives as this item.
        from core.events.common_events import CONTEXT_COMPACT
        from core.layers.codex.session import CodexEvent
        from core.layers.codex.translator import CodexEventTranslator

        tr = CodexEventTranslator(model="m")
        events = tr.translate(CodexEvent("item/completed", {
            "item": {"id": "i9", "type": "contextCompaction"},
        }))
        assert [e.type for e in events] == [CONTEXT_COMPACT]
        assert events[0].data["phase"] == "completed"

    def test_post_compaction_token_usage_moves_the_gauge(self):
        # A compaction at/after turn end has no follow-up METADATA frame, so
        # the NEXT tokenUsage (the compacted prompt size) must reach the
        # context gauge as a one-shot "usage" phase — no chip, no waiting for
        # the next turn to complete.
        from core.events.common_events import CONTEXT_COMPACT
        from core.layers.codex.session import CodexEvent
        from core.layers.codex.translator import CodexEventTranslator

        tr = CodexEventTranslator(model="m")
        tr.translate(CodexEvent("item/completed", {
            "item": {"id": "i9", "type": "contextCompaction"},
        }))
        events = tr.translate(CodexEvent("thread/tokenUsage/updated", {
            "tokenUsage": {
                "last": {"inputTokens": 8123, "cachedInputTokens": 0,
                         "outputTokens": 42},
                "modelContextWindow": 258400,
            },
        }))
        assert [e.type for e in events] == [CONTEXT_COMPACT]
        assert events[0].data == {
            "phase": "usage", "post_tokens": 8123, "context_max": 258400,
        }
        # One-shot: the next tokenUsage is bookkeeping only, no event.
        again = tr.translate(CodexEvent("thread/tokenUsage/updated", {
            "tokenUsage": {"last": {"inputTokens": 9000},
                           "modelContextWindow": 258400},
        }))
        assert again == []

    def test_legacy_compacted_also_arms_the_gauge_update(self):
        from core.events.common_events import CONTEXT_COMPACT
        from core.layers.codex.session import CodexEvent
        from core.layers.codex.translator import CodexEventTranslator

        tr = CodexEventTranslator(model="m")
        tr.translate(CodexEvent("thread/compacted", {}))
        events = tr.translate(CodexEvent("thread/tokenUsage/updated", {
            "tokenUsage": {"last": {"inputTokens": 5555},
                           "modelContextWindow": 100000},
        }))
        assert [e.type for e in events] == [CONTEXT_COMPACT]
        assert events[0].data["phase"] == "usage"
        assert events[0].data["post_tokens"] == 5555
