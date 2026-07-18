"""Refresh worker failure backoff.

A failed refresh leaves ``expires_at`` in the past, so without damping the
worker would retry the same dead token on every 60s tick — 1,440 vendor
calls/day (observed live: a revoked Google grant hammered the relay for six
days straight). These tests pin the damping added for that:

  * a failure schedules an exponentially-growing retry delay (base 120s,
    doubling, capped at 1h);
  * an identified ``invalid_grant`` (permanent — the vendor revoked the
    grant) stops retries entirely until the token file changes;
  * success / file rewrite / file removal clear the state.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
from auth.oauth_providers.base import TokenSet
from services.billing.relay_client import RelayError
from services.oauth import oauth_refresh_worker


def _write_token_file(path: Path, *, expires_in_seconds: int = 60) -> None:
    """A near-expiry standard-OAuth token file (worker will try to refresh)."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "provider": "slack",
        "account_id": "ACC-1",
        "access_token": "old-AT",
        "refresh_token": "my-refresh-token",
        "expires_at": expires_at,
        "scope": "chat:write",
        "client_id": "ci",
        "client_secret": "cs",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "extra": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


@pytest.fixture(autouse=True)
def _clean_state():
    """Isolate the module-level failure state + clock seam per test."""
    oauth_refresh_worker._failure_state.clear()
    orig = oauth_refresh_worker._monotonic
    yield
    oauth_refresh_worker._failure_state.clear()
    oauth_refresh_worker._monotonic = orig


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    base = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSIONS_DIR", base)
    return base


def _fake_clock(start: float = 1000.0):
    """Settable clock for oauth_refresh_worker._monotonic."""
    state = {"now": start}

    def now() -> float:
        return state["now"]

    return state, now


def _provider(refresh_mock) -> MagicMock:
    prov = MagicMock()
    prov.refresh = refresh_mock
    prov.token_url = "https://slack.com/api/oauth.v2.access"
    return prov


async def _tick_with(provider) -> None:
    with patch(
        "auth.oauth_providers.get_provider", return_value=provider,
    ), patch(
        "services.mcp.mcp_registry.get_mcps_by_provider", return_value=[],
    ):
        await oauth_refresh_worker._refresh_tick()


class TestFailureBackoff:
    @pytest.mark.asyncio
    async def test_failure_skips_next_tick_then_retries_after_delay(
        self, sessions_dir,
    ):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _write_token_file(f)
        clock, now = _fake_clock()
        oauth_refresh_worker._monotonic = now

        refresh = AsyncMock(side_effect=RuntimeError("vendor 500"))
        prov = _provider(refresh)

        await _tick_with(prov)
        assert refresh.call_count == 1

        # Immediately after (next 60s tick): still inside the 120s window.
        clock["now"] += 60
        await _tick_with(prov)
        assert refresh.call_count == 1

        # Past the first delay: retried.
        clock["now"] += 61
        await _tick_with(prov)
        assert refresh.call_count == 2

    @pytest.mark.asyncio
    async def test_delay_doubles_per_consecutive_failure(self, sessions_dir):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _write_token_file(f)
        clock, now = _fake_clock()
        oauth_refresh_worker._monotonic = now

        refresh = AsyncMock(side_effect=RuntimeError("vendor 500"))
        prov = _provider(refresh)

        await _tick_with(prov)                       # failure #1 → delay 120
        clock["now"] += 121
        await _tick_with(prov)                       # failure #2 → delay 240
        assert refresh.call_count == 2

        clock["now"] += 121                          # only 121s later
        await _tick_with(prov)
        assert refresh.call_count == 2               # still backing off

        clock["now"] += 120                          # 241s after failure #2
        await _tick_with(prov)
        assert refresh.call_count == 3

    @pytest.mark.asyncio
    async def test_success_clears_state(self, sessions_dir):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _write_token_file(f)
        clock, now = _fake_clock()
        oauth_refresh_worker._monotonic = now

        new_ts = TokenSet(
            access_token="new-AT", refresh_token="new-RT",
            expires_in=3600, scope="", token_type="Bearer", raw={},
        )
        refresh = AsyncMock(side_effect=[RuntimeError("vendor 500"), new_ts])
        prov = _provider(refresh)

        await _tick_with(prov)
        assert str(f) in oauth_refresh_worker._failure_state
        clock["now"] += 121
        await _tick_with(prov)
        assert refresh.call_count == 2
        assert oauth_refresh_worker._failure_state == {}

    @pytest.mark.asyncio
    async def test_state_dropped_when_file_removed(self, sessions_dir):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _write_token_file(f)
        oauth_refresh_worker._monotonic = _fake_clock()[1]

        refresh = AsyncMock(side_effect=RuntimeError("vendor 500"))
        await _tick_with(_provider(refresh))
        assert str(f) in oauth_refresh_worker._failure_state

        f.unlink()
        await _tick_with(_provider(refresh))
        assert oauth_refresh_worker._failure_state == {}


class TestInvalidGrantGiveUp:
    @pytest.mark.asyncio
    async def test_invalid_grant_stops_retries_until_file_changes(
        self, sessions_dir,
    ):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _write_token_file(f)
        clock, now = _fake_clock()
        oauth_refresh_worker._monotonic = now

        refresh = AsyncMock(
            side_effect=RuntimeError("slack token refresh failed: invalid_grant"),
        )
        prov = _provider(refresh)

        await _tick_with(prov)
        assert refresh.call_count == 1

        # Way past any backoff cap: dead tokens are still not retried.
        clock["now"] += 100 * 3600
        await _tick_with(prov)
        assert refresh.call_count == 1

        # Reconnect rewrites the file (new mtime) → the verdict is void.
        st = f.stat()
        os.utime(f, (st.st_atime, st.st_mtime + 10))
        await _tick_with(prov)
        assert refresh.call_count == 2

    def test_permanent_error_classification(self):
        is_perm = oauth_refresh_worker._is_permanent_refresh_error
        assert is_perm(RelayError("invalid_grant")) is True
        assert is_perm(RelayError("refresh_failed")) is False
        assert is_perm(RuntimeError("google token refresh failed: invalid_grant")) is True
        assert is_perm(RuntimeError("connection reset")) is False
        assert is_perm(ValueError("invalid_grant")) is False
