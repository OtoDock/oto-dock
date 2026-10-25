"""The refresh worker's installation arm (``extra.flow == installation``,
the files under ``sessions/<provider>-tokens/_app/``): dispatched before
the relay arm (a hosted installation file carries ``via_relay`` too), the
re-minted payload written through the guarded write with GitHub's own
expiry, an orphan file deleted with its state, a permanent refusal marked
in the file and never retried, a passing one backed off.

The mint itself is a fake here (``github_apps.remint_file``); the real one
is exercised in test_github_app_service_binding.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from auth.oauth_providers.base import OAuthTokenError
from services.oauth import github_apps, oauth_refresh_worker
from storage.identity import oauth_app_kinds as kinds


def _iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write(path: Path, *, seconds: int = 60, hosted: bool = False) -> dict:
    body = {
        "provider": "github", "account_id": "10", "access_token": "ghs_old", "refresh_token": "",
        "token_url": "", "client_id": "", "client_secret": "", "scopes": [], "expires_at": _iso(seconds),
        "extra": {"flow": kinds.INSTALLATION_FLOW, "installation_id": 10, "app_id": "4242",
                  "account": "alice", **({"via_relay": True} if hosted else {})},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    return body


@pytest.fixture
def app_file(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    oauth_refresh_worker._failure_state.clear()
    oauth_refresh_worker._pending_writes.clear()
    return tmp_path / "sessions" / "github-tokens" / kinds.APP_TOKEN_DIR / "10.json"


async def _tick(path: Path) -> bool:
    with patch("services.mcp.mcp_registry.get_mcps_by_provider", return_value=[]):
        return await oauth_refresh_worker._maybe_refresh_token_file(
            token_file=path, username=kinds.APP_TOKEN_DIR, provider_id="github",
        )


class TestInstallationArm:
    @pytest.mark.asyncio
    async def test_a_hosted_file_takes_the_installation_arm_not_the_relay(self, app_file, monkeypatch):
        _write(app_file, hosted=True)
        from services.billing import relay_client
        monkeypatch.setattr(relay_client, "is_available", lambda: True)
        monkeypatch.setattr(relay_client, "oauth_refresh", lambda **kw: (_ for _ in ()).throw(AssertionError("relay arm")))
        seen: list = []

        def remint(path, raw, *, provider_id):
            seen.append((path, provider_id))
            return {**raw, "access_token": "ghs_new", "expires_at": "2027-01-01T00:00:00Z"}
        monkeypatch.setattr(github_apps, "remint_file", remint)
        assert await _tick(app_file) is True
        assert seen == [(app_file, "github")]
        written = json.loads(app_file.read_text())
        assert written["access_token"] == "ghs_new" and written["expires_at"] == "2027-01-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_a_fresh_file_is_left_alone(self, app_file, monkeypatch):
        _write(app_file, seconds=3000)
        monkeypatch.setattr(github_apps, "remint_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no mint")))
        assert await _tick(app_file) is False

    @pytest.mark.asyncio
    async def test_an_orphan_goes_with_its_state(self, app_file, monkeypatch):
        _write(app_file)
        oauth_refresh_worker._failure_state[str(app_file)] = {"failures": 1, "next_attempt": 0, "mtime": 0, "dead": False}

        def remint(path, raw, *, provider_id):
            path.unlink()
            return None
        monkeypatch.setattr(github_apps, "remint_file", remint)
        assert await _tick(app_file) is False
        assert not app_file.exists() and str(app_file) not in oauth_refresh_worker._failure_state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", sorted(kinds.INSTALLATION_REASONS))
    async def test_a_permanent_refusal_marks_the_file_dead(self, app_file, monkeypatch, code):
        _write(app_file)
        calls = []

        def remint(path, raw, *, provider_id):
            calls.append(1)
            raise OAuthTokenError("github", "mint", code)
        monkeypatch.setattr(github_apps, "remint_file", remint)
        assert await _tick(app_file) is False
        assert json.loads(app_file.read_text())["extra"]["refresh_failed"] == code
        assert await _tick(app_file) is False
        assert len(calls) == 1                          # dead: not retried

    @pytest.mark.asyncio
    async def test_a_passing_refusal_backs_off(self, app_file, monkeypatch):
        _write(app_file)
        clock = {"now": 1000.0}
        monkeypatch.setattr(oauth_refresh_worker, "_monotonic", lambda: clock["now"])
        calls = []

        def remint(path, raw, *, provider_id):
            calls.append(1)
            raise OAuthTokenError("github", "mint", "unreachable")
        monkeypatch.setattr(github_apps, "remint_file", remint)
        with patch("services.mcp.mcp_registry.get_mcps_by_provider", return_value=[]):
            await oauth_refresh_worker._refresh_tick()
        assert len(calls) == 1 and "refresh_failed" not in json.loads(app_file.read_text())["extra"]
        clock["now"] += 60
        with patch("services.mcp.mcp_registry.get_mcps_by_provider", return_value=[]):
            await oauth_refresh_worker._refresh_tick()
        assert len(calls) == 1
        clock["now"] += 61
        with patch("services.mcp.mcp_registry.get_mcps_by_provider", return_value=[]):
            await oauth_refresh_worker._refresh_tick()
        assert len(calls) == 2
