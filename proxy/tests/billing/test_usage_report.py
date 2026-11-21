"""A session's own report of its account's windows is a claim by that session.

The engine declares where its live sessions report (``UsageProfile.in_band``):
a headless stream the platform reads from the engine's own output is a
reading, a terminal's status line post or a rollout line is a hint, which the
agent could have written. A hint is stored and shown, asks for at most one
confirming poll, and never moves a session, rests an account or ends a rest
(``subscription_windows.record_for_session``). The route a terminal posts to
(``POST /v1/hooks/usage-report``) files a report under the session's own
binding and no other, and takes one report per session in five seconds.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import config
from api.hooks import usage as route
from auth.external_endpoints import (
    is_external_endpoint_allowed,
    is_session_endpoint_allowed,
)
from auth.session_token import create_session_token
from core.layers.cli import usage as claude_usage
from core.layers.codex import usage as codex_usage
from services.engines import subscription_pool as sp, subscription_windows as sw

_STORE = "storage.billing.subscription_store"
_POOL = "services.engines.subscription_pool"
NOW = datetime.now(timezone.utc)
_CLAUDE = {"id": "sub-a", "layer": "claude-code-cli", "auth_type": "oauth"}
_CODEX = {"id": "sub-x", "layer": "codex-cli", "auth_type": "oauth"}


def _windows(source: str, five: float, seven: float = 10.0) -> sw.Windows:
    specs = sw.window_specs("claude-code-cli")
    w = sw.Windows(specs=specs, source=source, observed_at=NOW)
    w.windows["five_hour"] = sw.Window(five, NOW + timedelta(hours=2))
    w.windows["seven_day"] = sw.Window(seven, NOW + timedelta(days=3))
    return w


def _trusted_row(five: float) -> dict:
    return {"observed_at": (NOW - timedelta(minutes=1)).isoformat(), "source": "claude_event",
            "five_hour_pct": five, "five_hour_resets_at": (NOW + timedelta(hours=2)).isoformat(),
            "seven_day_pct": 10.0, "seven_day_resets_at": (NOW + timedelta(days=3)).isoformat(),
            "data": {}}


@pytest.fixture(autouse=True)
def _clean():
    sp._session_moved_at.clear()
    sw.reset_poll_state()
    route._last_report.clear()
    yield
    sp._session_moved_at.clear()
    sw.reset_poll_state()
    route._last_report.clear()


class _Spies:
    def __init__(self, row: dict, trusted: dict | None = None):
        self.insert = MagicMock(return_value=True)
        self.rebalance = MagicMock()
        self.clear_rests = MagicMock()
        self.request_poll = MagicMock(return_value=True)
        self._row = row
        self._trusted = trusted

    def __enter__(self):
        trusted = self._trusted
        self._patches = [
            patch.object(sw, "is_enabled", return_value=True),
            patch(f"{_STORE}.insert_window_sample", self.insert),
            patch(f"{_STORE}.latest_window_samples",
                  side_effect=lambda ids, **kw: {} if kw.get("hints") or trusted is None
                  else {i: trusted for i in ids}),
            patch(f"{_STORE}.get_subscription", return_value=self._row),
            patch(f"{_POOL}.get_session_subscription", return_value=self._row["id"]),
            patch(f"{_POOL}.schedule_rebalance", self.rebalance),
            patch(f"{_POOL}.clear_rests_with_headroom", self.clear_rests),
            patch.object(sw, "request_poll", self.request_poll),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


# ---------------------------------------------------------------------------
# The trust rule
# ---------------------------------------------------------------------------

def test_a_hint_never_moves_a_session_rests_an_account_or_ends_a_rest():
    with _Spies(_CLAUDE) as s:
        assert sw.record_for_session("s1", _windows("claude_statusline", 97.0))
    assert s.insert.call_args.kwargs["hint"] is True
    assert s.insert.call_args.args == ("sub-a",)
    s.rebalance.assert_not_called()
    s.clear_rests.assert_not_called()
    # It disagrees with the account's reading (none past the mark): one confirming poll.
    s.request_poll.assert_called_once()
    assert s.request_poll.call_args.args == ("sub-a",)
    assert s.request_poll.call_args.kwargs["floor_s"] == sw._HINT_POLL_FLOOR_S == 300.0


def test_a_hint_that_agrees_with_the_reading_asks_for_no_poll():
    with _Spies(_CLAUDE, trusted=_trusted_row(96.0)) as s:
        sw.record_for_session("s1", _windows("claude_statusline", 97.0))
    s.request_poll.assert_not_called()
    s.rebalance.assert_not_called()


def test_a_hint_below_the_mark_of_an_account_past_it_asks_for_a_poll_and_ends_nothing():
    with _Spies(_CLAUDE, trusted=_trusted_row(96.0)) as s:
        sw.record_for_session("s1", _windows("claude_statusline", 20.0))
    s.request_poll.assert_called_once()
    s.clear_rests.assert_not_called()


def test_a_reading_from_the_stream_is_acted_on():
    with _Spies(_CLAUDE) as s:
        assert sw.record_for_session("s1", _windows("claude_event", 97.0))
    assert s.insert.call_args.kwargs.get("hint", False) is False
    s.rebalance.assert_called_once()
    s.clear_rests.assert_called_once()
    s.request_poll.assert_not_called()


def test_a_rollout_line_is_a_hint():
    snapshot = {"primary": {"used_percent": 99.0, "window_minutes": 300, "resets_at": int(time.time()) + 3600},
                "secondary": {"used_percent": 10.0, "window_minutes": 10080,
                              "resets_at": int(time.time()) + 86400}}
    with _Spies(_CODEX) as s:
        assert codex_usage.record_snapshot("s1", snapshot)
    assert s.insert.call_args.kwargs["source"] == "codex_rollout"
    assert s.insert.call_args.kwargs["hint"] is True
    s.rebalance.assert_not_called()


def test_a_source_the_accounts_engine_does_not_declare_is_dropped():
    with _Spies(_CODEX) as s:
        assert not sw.record_for_session("s1", _windows("claude_statusline", 97.0))
        assert not sw.record_for_session("s1", _windows("poll", 97.0))
    s.insert.assert_not_called()


def test_a_report_right_after_the_session_moved_is_dropped():
    sp._session_moved_at["s1"] = time.monotonic()
    with _Spies(_CLAUDE) as s:
        assert not sw.record_for_session("s1", _windows("claude_event", 97.0))
    s.insert.assert_not_called()
    sp._session_moved_at["s1"] = time.monotonic() - sw._MOVED_DROP_S - 1
    with _Spies(_CLAUDE) as s:
        assert sw.record_for_session("s1", _windows("claude_event", 97.0))


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def _limits(five: float = 42, seven: float = 17) -> dict:
    return {"five_hour": {"used_percentage": five, "resets_at": int(time.time()) + 3600},
            "seven_day": {"used_percentage": seven, "resets_at": int(time.time()) + 86400}}


async def _post(session_id: str, token: str, limits: dict | None = None):
    req = route.UsageReportRequest(session_id=session_id, rate_limits=limits or _limits())
    return await route.hook_usage_report(req, authorization=f"Bearer {token}")


@pytest.fixture
def _holder_ok():
    async def _ok(payload):
        return True
    with patch("api.sessions.sessions.token_holder.holder_ok", _ok):
        yield


@pytest.mark.asyncio
async def test_the_route_files_a_report_under_the_sessions_own_account(_holder_ok):
    with _Spies(_CLAUDE) as s:
        out = await _post("sess-1", create_session_token("sess-1", "alpha", "user-1"))
    assert out == {"ok": True, "recorded": True}
    assert s.insert.call_args.args == ("sub-a",)
    assert s.insert.call_args.kwargs["source"] == "claude_statusline"
    assert s.insert.call_args.kwargs["hint"] is True
    assert s.insert.call_args.kwargs["five_hour_pct"] == 42


@pytest.mark.asyncio
async def test_a_report_for_another_session_is_refused(_holder_ok):
    with _Spies(_CLAUDE) as s:
        with pytest.raises(HTTPException) as e:
            await _post("sess-other", create_session_token("sess-1", "alpha", "user-1"))
    assert e.value.status_code == 403
    s.insert.assert_not_called()


@pytest.mark.asyncio
async def test_the_master_key_cannot_report(_holder_ok):
    with _Spies(_CLAUDE) as s:
        with pytest.raises(HTTPException) as e:
            await _post("sess-1", config.API_KEY)
    assert e.value.status_code == 403
    s.insert.assert_not_called()


def test_a_report_cannot_name_an_account():
    with pytest.raises(ValidationError):
        route.UsageReportRequest(session_id="sess-1", rate_limits=_limits(), subscription_id="sub-b")


@pytest.mark.asyncio
async def test_a_session_reports_once_in_five_seconds(_holder_ok):
    token = create_session_token("sess-1", "alpha", "user-1")
    with _Spies(_CLAUDE) as s:
        await _post("sess-1", token)
        with pytest.raises(HTTPException) as e:
            await _post("sess-1", token, _limits(43))
    assert e.value.status_code == 429
    assert s.insert.call_count == 1


@pytest.mark.asyncio
async def test_a_report_the_engine_does_not_know_is_refused(_holder_ok):
    with _Spies(_CODEX) as s:
        with pytest.raises(HTTPException) as e:
            await _post("sess-1", create_session_token("sess-1", "alpha", "user-1"))
    assert e.value.status_code == 422
    s.insert.assert_not_called()


@pytest.mark.asyncio
async def test_a_large_report_is_refused(_holder_ok):
    big = {**_limits(), "pad": {"used_percentage": 1, "x": "y" * 4000}}
    with _Spies(_CLAUDE), pytest.raises(HTTPException) as e:
        await _post("sess-1", create_session_token("sess-1", "alpha", "user-1"), big)
    assert e.value.status_code == 422


def test_the_route_is_on_a_sessions_list_and_not_an_outside_callers():
    assert is_session_endpoint_allowed("POST", "/v1/hooks/usage-report", "alpha")
    assert not is_session_endpoint_allowed("GET", "/v1/hooks/usage-report", "alpha")
    assert not is_external_endpoint_allowed("POST", "/v1/hooks/usage-report")


@pytest.mark.asyncio
async def test_the_rate_limit_is_each_sessions_own(_holder_ok):
    with _Spies(_CLAUDE) as s:
        await _post("sess-1", create_session_token("sess-1", "alpha", "user-1"))
        await _post("sess-2", create_session_token("sess-2", "alpha", "user-1"))
    assert s.insert.call_count == 2


def test_a_percentage_outside_its_range_is_no_window():
    specs = sw.window_specs("claude-code-cli")
    for bad in (float("nan"), float("inf"), -1, 1001):
        assert claude_usage.from_statusline({"five_hour": {"used_percentage": bad}}, specs) is None, bad
    assert claude_usage.from_statusline({"five_hour": {"used_percentage": 1000}}, specs) is not None


def test_a_rollout_line_dated_in_the_future_is_stored_as_now():
    snapshot = {"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": int(time.time()) + 3600},
                "secondary": {"used_percent": 100.0, "window_minutes": 10080, "resets_at": 4102444800}}
    with _Spies(_CODEX) as s:
        assert codex_usage.record_snapshot("s1", snapshot, "2099-01-01T00:00:00Z")
    stored = datetime.fromisoformat(s.insert.call_args.kwargs["observed_at"])
    assert stored <= datetime.now(timezone.utc) + timedelta(seconds=5)


def test_the_status_line_report_parses_both_windows():
    w = claude_usage.from_statusline(_limits(100, 17), sw.window_specs("claude-code-cli"))
    assert w.source == "claude_statusline"
    assert w.windows["five_hour"].pct == 100 and w.reached == "five_hour"
    assert claude_usage.from_statusline({"five_hour": {"used_percentage": "x"}}, sw.window_specs("claude-code-cli")) is None
    assert claude_usage.from_statusline(None, {}) is None


# ---------------------------------------------------------------------------
# The polls a report or a limit asks for
# ---------------------------------------------------------------------------

def _scheduled():
    ran: list = []

    def _on_loop(cb):
        cb()
        return True

    def _spawn(coro, **kw):
        ran.append(kw.get("name"))
        coro.close()
    return ran, _on_loop, _spawn


def test_a_requested_poll_keeps_its_floor_and_the_backoff():
    ran, on_loop, spawn = _scheduled()
    with patch.object(sw, "is_enabled", return_value=True), patch.object(sw, "_air_gapped", return_value=False), \
         patch("services.engines.token_fanout.on_loop", on_loop), patch.object(sw, "spawn", spawn):
        assert sw.request_poll("sub-a", floor_s=300, reason="t")
        assert not sw.request_poll("sub-a", floor_s=300, reason="t")
        assert sw.request_poll("sub-b", floor_s=300, reason="t")
        sw._note_failure("sub-c", time.monotonic())
        assert not sw.request_poll("sub-c", floor_s=0, reason="t")
    assert ran == ["requested_poll", "requested_poll"]


def test_a_tick_poll_counts_toward_the_floor():
    ran, on_loop, spawn = _scheduled()
    with sw._poll_lock:
        sw._last_asked["sub-a"] = time.monotonic()
    with patch.object(sw, "is_enabled", return_value=True), patch.object(sw, "_air_gapped", return_value=False), \
         patch("services.engines.token_fanout.on_loop", on_loop), patch.object(sw, "spawn", spawn):
        assert not sw.request_poll("sub-a", floor_s=300, reason="t")
    assert ran == []


def test_a_limit_error_asks_for_one_reading():
    from services.engines import pool_limits
    with patch.object(pool_limits, "get_session_subscription", return_value="sub-a"), \
         patch.object(pool_limits, "_rest_account", return_value=True), \
         patch.object(pool_limits, "schedule_rebalance"), \
         patch.object(sw, "request_poll") as asked:
        pool_limits.mark_subscription_throttled("s1")
        pool_limits.mark_subscription_throttled("s1", cooldown_s=30)   # an overload nudge asks nothing
    asked.assert_called_once()
    assert asked.call_args.args == ("sub-a",) and asked.call_args.kwargs["floor_s"] == 300.0


def test_limit_errors_never_poll_the_vendor_once_a_minute():
    """Ten limit errors a minute apart, on one account that stays in use: the
    vendor is asked at most once in five minutes, the tick's own rate."""
    from services.engines import pool_limits
    ran, on_loop, spawn = _scheduled()
    clock = [10_000.0]
    with patch.object(sw, "is_enabled", return_value=True), patch.object(sw, "_air_gapped", return_value=False), \
            patch("services.engines.token_fanout.on_loop", on_loop), patch.object(sw, "spawn", spawn), \
            patch.object(sw.time, "monotonic", lambda: clock[0]), \
            patch.object(pool_limits, "get_session_subscription", return_value="sub-a"), \
            patch.object(pool_limits, "_rest_account", return_value=True), \
            patch.object(pool_limits, "schedule_rebalance"):
        for _ in range(10):
            pool_limits.mark_subscription_throttled("s1")
            clock[0] += 61
    assert len(ran) == 2


@pytest.mark.asyncio
async def test_the_tick_leaves_out_an_account_asked_a_moment_ago():
    """A requested poll and the tick never ask for one account twice in a minute."""
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    rows = [{"id": i, "layer": "claude-code-cli", "auth_type": "oauth", "status": "active"}
            for i in ("just-asked", "stale")]
    old = {"observed_at": (now - timedelta(minutes=30)).isoformat(), "source": "poll"}
    asked: list[str] = []

    async def _poll_one(sub, client, **kw):
        asked.append(sub["id"])
        return True
    with sw._poll_lock:
        sw._last_asked["just-asked"] = time.monotonic() - 20
    with patch.object(sw, "is_enabled", return_value=True), patch.object(sw, "_air_gapped", return_value=False), \
         patch(f"{_STORE}.list_subscriptions", return_value=rows), \
         patch(f"{_STORE}.latest_window_samples", side_effect=lambda ids, **kw: {i: old for i in ids}), \
         patch(f"{_STORE}.prune_window_samples"), \
         patch(f"{_POOL}.bound_oauth_subscription_ids", return_value=set()), \
         patch.object(sw, "poll_one", _poll_one):
        await sw.poll_due(client=MagicMock(), now=now)
    assert asked == ["stale"]


@pytest.mark.asyncio
async def test_an_account_in_use_is_polled_every_fifteen_minutes():
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    rows = [{"id": i, "layer": "claude-code-cli", "auth_type": "oauth", "status": "active"}
            for i in ("used-old-poll", "used-fresh-poll", "idle")]
    fresh = {"observed_at": (now - timedelta(minutes=1)).isoformat(), "source": "claude_event"}

    def _latest(ids, *, source=None, hints=False):
        if source == "poll":
            return {"used-old-poll": {"observed_at": (now - timedelta(minutes=16)).isoformat()},
                    "used-fresh-poll": {"observed_at": (now - timedelta(minutes=5)).isoformat()}}
        return {i: fresh for i in ids}
    asked: list[str] = []

    async def _poll_one(sub, client, **kw):
        asked.append(sub["id"])
        return True
    with patch.object(sw, "is_enabled", return_value=True), patch.object(sw, "_air_gapped", return_value=False), \
         patch(f"{_STORE}.list_subscriptions", return_value=rows), \
         patch(f"{_STORE}.latest_window_samples", side_effect=_latest), \
         patch(f"{_STORE}.prune_window_samples"), \
         patch(f"{_POOL}.bound_oauth_subscription_ids", return_value={"used-old-poll", "used-fresh-poll"}), \
         patch.object(sw, "poll_one", _poll_one):
        n = await sw.poll_due(client=MagicMock(), now=now)
    assert asked == ["used-old-poll"] and n == 1


# ---------------------------------------------------------------------------
# What the cards show
# ---------------------------------------------------------------------------

def test_the_cards_show_a_newer_hint_raised_never_lowered():
    def row(source, five, seven, minutes_ago):
        return {"observed_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "source": source,
                "five_hour_pct": five, "five_hour_resets_at": (NOW + timedelta(hours=2)).isoformat(),
                "seven_day_pct": seven, "seven_day_resets_at": (NOW + timedelta(days=3)).isoformat(),
                "data": {}}
    trusted = {"sub-a": row("poll", 50.0, 30.0, 3)}
    hints = {"sub-a": row("claude_statusline", 60.0, 20.0, 1), "sub-b": row("claude_statusline", 5.0, 5.0, 1)}
    with patch(f"{_STORE}.latest_window_samples",
               side_effect=lambda ids, **kw: hints if kw.get("hints") else
               ({} if kw.get("source") else trusted)):
        shown = sw.shown([{"id": "sub-a", "layer": "claude-code-cli"}, {"id": "sub-b", "layer": "claude-code-cli"}],
                         NOW)
    assert shown["sub-a"].windows["five_hour"].pct == 60.0      # raised
    assert shown["sub-a"].windows["seven_day"].pct == 30.0      # never lowered
    assert shown["sub-b"].windows["five_hour"].pct == 5.0       # a hint alone is shown
    # The pool's own read never sees a hint.
    with patch(f"{_STORE}.latest_window_samples",
               side_effect=lambda ids, **kw: hints if kw.get("hints") else
               ({} if kw.get("source") else trusted)):
        assert sw.latest([{"id": "sub-a", "layer": "claude-code-cli"}], NOW)["sub-a"].windows["five_hour"].pct == 50.0


# ---------------------------------------------------------------------------
# The status line command
# ---------------------------------------------------------------------------

def test_the_script_posts_once_per_change_and_draws_nothing(tmp_path, monkeypatch, capsys):
    import importlib.util
    import io

    from tests._paths import PROXY_DIR
    spec = importlib.util.spec_from_file_location("usage_report_script", PROXY_DIR / "hooks" / "usage_report.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    posted: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def _urlopen(req, timeout):
        posted.append({"url": req.full_url, "body": json.loads(req.data), "auth": req.headers["Authorization"]})
        return _Resp()
    monkeypatch.setattr(script.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(script.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("OTO_SESSION_ID", "sess-1")
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:8401")
    monkeypatch.setenv("PROXY_API_KEY", "tok")
    inp = {"session_id": "cli-id", "model": {"id": "claude-sonnet-5-5"}, "rate_limits": _limits(42, 17),
           "cost": {"total_cost_usd": 1.0}}
    for _ in range(2):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(inp)))
        script.main()
    inp["rate_limits"]["five_hour"]["used_percentage"] = 43
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(inp)))
    script.main()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"model": {"id": "x"}})))
    script.main()
    assert [p["body"]["rate_limits"]["five_hour"]["used_percentage"] for p in posted] == [42, 43]
    assert posted[0]["url"] == "http://127.0.0.1:8401/v1/hooks/usage-report"
    assert posted[0]["body"]["session_id"] == "sess-1"        # the platform's id, never the CLI's
    assert set(posted[0]["body"]) == {"session_id", "rate_limits"}
    assert posted[0]["auth"] == "Bearer tok"
    assert capsys.readouterr().out == ""


def test_the_scripts_note_never_follows_a_link(tmp_path, monkeypatch):
    import importlib.util
    import io
    import os

    from tests._paths import PROXY_DIR
    spec = importlib.util.spec_from_file_location("usage_report_script2", PROXY_DIR / "hooks" / "usage_report.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"
    monkeypatch.setattr(script.urllib.request, "urlopen", lambda req, timeout: _Resp())
    monkeypatch.setattr(script.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("OTO_SESSION_ID", "sess-l")
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:8401")
    monkeypatch.setenv("PROXY_API_KEY", "tok")
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    os.symlink(target, script._last_path("sess-l"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"rate_limits": _limits(42, 17)})))
    script.main()
    assert target.read_text() == "keep"
