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
from datetime import datetime, timedelta, timezone
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

        # The verdict is written into the file: the card and the resolver
        # read it, and it survives a proxy restart.
        marked = json.loads(f.read_text())
        assert marked["extra"]["refresh_failed"] == "invalid_grant"
        assert marked["extra"]["refresh_failed_at"]

        # Way past any backoff cap: dead tokens are still not retried.
        clock["now"] += 100 * 3600
        await _tick_with(prov)
        assert refresh.call_count == 1

        # A reconnect rewrites the whole file (fresh content, no verdict,
        # new mtime) → the verdict is void. The mtime is bumped by hand: a
        # rewrite inside the same kernel clock tick keeps the old one.
        _write_token_file(f)
        st = f.stat()
        os.utime(f, (st.st_atime, st.st_mtime + 10))
        await _tick_with(prov)
        assert refresh.call_count == 2

    def test_permanent_error_classification(self):
        from auth.oauth_providers.base import OAuthTokenError
        code = oauth_refresh_worker._permanent_error_code
        assert code(RelayError("invalid_grant"), "relay") == "invalid_grant"
        assert code(RelayError("refresh_failed"), "relay") == ""
        assert code(RuntimeError("google token refresh failed: invalid_grant"), "standard") == "invalid_grant"
        assert code(RuntimeError("connection reset"), "standard") == ""
        assert code(ValueError("invalid_grant"), "standard") == ""
        # the typed error: the code decides, whatever the description says
        assert code(OAuthTokenError("p", "refresh", "invalid_grant", "Token has been revoked"), "standard") == "invalid_grant"
        assert code(OAuthTokenError("p", "refresh", "http_503", "", 503), "standard") == ""
        # invalid_client is permanent for the registered client only
        assert code(OAuthTokenError("p", "refresh", "invalid_client"), "mcp_authorization") == "invalid_client"
        assert code(OAuthTokenError("p", "refresh", "invalid_client"), "standard") == ""


def _far_file(path: Path, **fields) -> None:
    """A token file an hour from its expiry (the tick leaves it alone)."""
    _write_token_file(path, expires_in_seconds=3600)
    if fields:
        raw = json.loads(path.read_text())
        raw.update(fields)
        path.write_text(json.dumps(raw))


async def _renew_with(provider, path: Path, still=lambda raw: True) -> str:
    with patch(
        "auth.oauth_providers.get_provider", return_value=provider,
    ), patch(
        "services.mcp.mcp_registry.get_mcps_by_provider", return_value=[],
    ):
        return await oauth_refresh_worker.renew_now(path, still=still)


class TestRenewalOutOfTurn:
    """``renew_now``: the credential gateway's ask after the MCP server
    refused a token far from its expiry."""

    @pytest.fixture(autouse=True)
    def _no_reservations(self):
        oauth_refresh_worker._forced_at.clear()
        yield
        oauth_refresh_worker._forced_at.clear()

    def _renewed(self):
        return AsyncMock(return_value=TokenSet(
            access_token="new-AT", refresh_token="new-RT", expires_in=3600,
            scope="", token_type="Bearer", raw={},
        ))

    @pytest.mark.asyncio
    async def test_a_file_far_from_expiry_is_renewed_once_an_hour(self, sessions_dir):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _far_file(f)
        clock, now = _fake_clock()
        oauth_refresh_worker._monotonic = now
        refresh = self._renewed()
        assert await _renew_with(_provider(refresh), f) == oauth_refresh_worker.RENEWED
        assert json.loads(f.read_text())["access_token"] == "new-AT"
        clock["now"] += 1800
        assert await _renew_with(_provider(refresh), f) == oauth_refresh_worker.NOT_NOW
        assert refresh.call_count == 1
        clock["now"] += 1801
        assert await _renew_with(_provider(refresh), f) == oauth_refresh_worker.RENEWED
        assert refresh.call_count == 2

    @pytest.mark.asyncio
    async def test_a_token_with_nothing_to_renew_it_says_so_without_a_call(self, sessions_dir):
        refresh = self._renewed()
        no_refresh = sessions_dir / "slack-tokens" / "alice" / "a.json"
        _far_file(no_refresh, refresh_token="")
        pat = sessions_dir / "slack-tokens" / "alice" / "b.json"
        _far_file(pat, extra={"flow": "personal_access_token"})
        no_client = sessions_dir / "slack-tokens" / "alice" / "c.json"
        _far_file(no_client, client_secret="")
        for f in (no_refresh, pat, no_client):
            assert await _renew_with(_provider(refresh), f) == oauth_refresh_worker.NOT_RENEWABLE
        assert refresh.call_count == 0
        assert oauth_refresh_worker._forced_at == {}

    def test_renewable_follows_the_arms(self):
        base = {"refresh_token": "RT", "client_id": "ci", "client_secret": "cs", "extra": {}}
        renewable = oauth_refresh_worker.renewable
        assert renewable(base)
        assert not renewable({**base, "refresh_token": ""})
        assert not renewable({**base, "client_secret": ""})
        assert renewable({**base, "client_secret": "", "extra": {"via_relay": True}})
        assert renewable({**base, "client_secret": "", "extra": {"flow": "mcp_authorization"}})
        assert not renewable({**base, "extra": {"flow": "personal_access_token"}})
        assert renewable({**base, "refresh_token": "", "extra": {"flow": "client_credentials"}})
        assert not renewable({"client_id": "", "client_secret": "", "extra": {"flow": "client_credentials"}})
        assert renewable({"extra": {"flow": "installation"}})

    @pytest.mark.asyncio
    async def test_a_file_that_changed_or_died_is_not_renewed(self, sessions_dir):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _far_file(f)
        refresh = self._renewed()
        assert await _renew_with(_provider(refresh), f, still=lambda raw: False) == oauth_refresh_worker.NOT_NOW
        _far_file(f, extra={"refresh_failed": "invalid_grant"})
        assert await _renew_with(_provider(refresh), f) == oauth_refresh_worker.GAVE_UP
        assert refresh.call_count == 0

    @pytest.mark.asyncio
    async def test_a_failure_backs_off_and_invalid_grant_gives_up(self, sessions_dir, caplog):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _far_file(f)
        oauth_refresh_worker._monotonic = _fake_clock()[1]
        refresh = AsyncMock(side_effect=RuntimeError("vendor 500"))
        assert await _renew_with(_provider(refresh), f) == oauth_refresh_worker.NOT_NOW
        assert oauth_refresh_worker._failure_state[str(f)]["failures"] == 1
        assert "default" not in caplog.text
        g = sessions_dir / "slack-tokens" / "alice" / "other.json"
        _far_file(g)
        refresh = AsyncMock(side_effect=RuntimeError("slack token refresh failed: invalid_grant"))
        assert await _renew_with(_provider(refresh), g) == oauth_refresh_worker.GAVE_UP
        assert json.loads(g.read_text())["extra"]["refresh_failed"] == "invalid_grant"


class TestGuardedRewrite:
    @pytest.mark.asyncio
    async def test_a_rewrite_writes_what_the_change_returns(self, sessions_dir):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _far_file(f)

        def mark(raw):
            return {**raw, "extra": {**raw["extra"], "seen": True}}

        assert await oauth_refresh_worker.rewrite(f, mark) is True
        assert json.loads(f.read_text())["extra"] == {"seen": True}
        assert await oauth_refresh_worker.rewrite(f, lambda raw: None) is False
        assert json.loads(f.read_text())["extra"] == {"seen": True}
        assert await oauth_refresh_worker.rewrite(f.with_name("gone.json"), mark) is False

    @pytest.mark.asyncio
    async def test_nothing_is_written_while_a_refreshed_token_waits(self, sessions_dir):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _far_file(f)
        oauth_refresh_worker._pending_writes[str(f)] = oauth_refresh_worker._PendingWrite(
            mtime=f.stat().st_mtime, payload={"access_token": "refreshed"})
        try:
            assert await oauth_refresh_worker.rewrite(f, lambda raw: {**raw, "x": 1}) is False
        finally:
            oauth_refresh_worker._pending_writes.clear()
        assert "x" not in json.loads(f.read_text())

    def test_the_reread_takes_the_mtime_of_the_file_it_read(self, sessions_dir):
        f = sessions_dir / "slack-tokens" / "alice" / "default.json"
        _far_file(f)
        os.utime(f, (1_000_000, 1_000_000))
        raw, mtime = oauth_refresh_worker._read_with_mtime(f)
        assert raw is not None and raw["access_token"] == "old-AT" and mtime == 1_000_000
        f.write_text("{not json")
        assert oauth_refresh_worker._read_with_mtime(f) == (None, 0.0)
        assert oauth_refresh_worker._read_with_mtime(f.with_name("gone.json")) == (None, 0.0)
