"""A tool's account token: in its bundle by reference, read fresh at every
fetch and read again by the tool when its hour ends, never the refresh token
(``core/credentials/mcp_gateway.TokenCredential``, ``mcp_broker``,
``api/hooks/credentials``, ``services/oauth/oauth_refresh_worker.refresh_if_due``)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.hooks.credentials import hook_mcp_credentials
from auth import tokens
from core.credentials import mcp_broker, mcp_gateway
from core.credentials.mcp_broker import SecretBundle
from core.credentials.mcp_gateway import TokenCredential, TokenRef
from services.oauth import oauth_refresh_worker

ACCESS = "ya29.the-access-token"
REFRESH = "1//the-refresh-token-no-session-reads"
SECRET = "the-client-secret"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    mcp_broker._store.clear()
    mcp_gateway.forget_memos()

    async def _nothing_due(_path):
        return False
    monkeypatch.setattr(oauth_refresh_worker, "refresh_if_due", _nothing_due)
    yield
    mcp_broker._store.clear()
    mcp_gateway.forget_memos()


def _expires(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _write(tmp_path: Path, label: str = "a@b.test", access: str = ACCESS, seconds: float = 3500) -> Path:
    path = tmp_path / f"{label}.json"
    path.write_text(json.dumps({
        "access_token": access, "refresh_token": REFRESH, "client_id": "cid", "client_secret": SECRET,
        "expires_at": _expires(seconds), "scopes": ["gmail.readonly", "drive"],
    }))
    return path


def _cred(tmp_path: Path, *, label: str = "a@b.test", renews: bool = True) -> TokenCredential:
    return TokenCredential(env_names=("GOOGLE_OAUTH_ACCESS_TOKEN",), token_ref=TokenRef(str(tmp_path), label),
                           manifest="", renews=renews)


def _ask(token: str) -> dict:
    return asyncio.run(hook_mcp_credentials(authorization=f"Bearer {token}"))


def _status(token: str) -> int:
    with pytest.raises(HTTPException) as e:
        _ask(token)
    return e.value.status_code


def _provision(tmp_path: Path, *, renews: bool = True, sid: str = "s1", mcp: str = "gws") -> SecretBundle:
    bundle = SecretBundle(env={"GOOGLE_OAUTH_ACCESS_TOKEN": "built", "STATIC_KEY": "static"},
                          token=_cred(tmp_path, renews=renews))
    mcp_broker.provision(sid, {mcp: bundle})
    return bundle


def _kill(path: Path) -> None:
    raw = json.loads(path.read_text())
    raw["extra"] = {"refresh_failed": "invalid_grant"}
    path.write_text(json.dumps(raw))


# ── what a token file gives a tool ─────────────────────────────────────────

def test_the_env_holds_the_access_token_and_what_the_tool_needs_nothing_more(tmp_path):
    _write(tmp_path)
    cred = _cred(tmp_path)
    token = mcp_gateway.resolve_token_credential(cred)
    env = mcp_gateway.token_env(cred, token)
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == ACCESS
    assert env[mcp_gateway.TOKEN_SCOPES_ENV] == "gmail.readonly drive"
    assert env[mcp_gateway.TOKEN_EXPIRES_ENV].endswith("Z")
    assert env[mcp_gateway.TOKEN_ACCOUNT_ENV] == "a@b.test"
    blob = json.dumps(env)
    assert REFRESH not in blob and SECRET not in blob


def test_a_token_with_no_expiry_carries_no_expiry_name(tmp_path):
    (tmp_path / "a@b.test.json").write_text(json.dumps({"access_token": ACCESS}))
    cred = _cred(tmp_path, renews=False)
    env = mcp_gateway.token_env(cred, mcp_gateway.resolve_token_credential(cred))
    assert env == {"GOOGLE_OAUTH_ACCESS_TOKEN": ACCESS, mcp_gateway.TOKEN_ACCOUNT_ENV: "a@b.test"}


def test_a_dead_account_and_a_missing_file_are_refused_with_the_reconnect_wording(tmp_path):
    _kill(_write(tmp_path))
    res = mcp_gateway.resolve_token_credential(_cred(tmp_path))
    assert isinstance(res, mcp_gateway.Refusal) and res.reason == "needs_reconnect"
    assert isinstance(mcp_gateway.resolve_token_credential(_cred(tmp_path, label="gone@b.test")),
                      mcp_gateway.Refusal)


# ── the renewal capability ─────────────────────────────────────────────────

def test_a_renewal_is_minted_only_for_a_token_that_expires(tmp_path):
    assert mcp_gateway.TOKEN_RENEWAL_ENV not in _provision(tmp_path, renews=False).env
    token = _provision(tmp_path, renews=True).env[mcp_gateway.TOKEN_RENEWAL_ENV]
    assert mcp_broker.verify_renewal(token) == ("s1", "gws")


def test_a_renewal_token_and_a_spawn_token_are_different_kinds():
    renewal, spawn = mcp_broker.mint_renewal("s1", "gws"), mcp_broker.mint_token("s1", "gws")
    assert mcp_broker.verify_renewal(spawn) is None
    assert mcp_broker.verify_token(renewal) is None
    assert tokens.verify(renewal, tokens.SESSION) is None
    assert tokens.verify(renewal, tokens.CLI) is None


def test_the_spawn_token_gets_the_bundle_with_the_token_read_fresh(tmp_path):
    _write(tmp_path)
    _provision(tmp_path)
    env = _ask(mcp_broker.mint_token("s1", "gws"))["env"]
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == ACCESS  # the file's, over the build's
    assert env["STATIC_KEY"] == "static"
    assert env[mcp_gateway.TOKEN_RENEWAL_ENV]
    assert REFRESH not in json.dumps(env) and SECRET not in json.dumps(env)


def test_the_spawn_token_keeps_the_built_value_when_the_fresh_read_refuses(tmp_path):
    _provision(tmp_path)
    env = _ask(mcp_broker.mint_token("s1", "gws"))["env"]
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == "built"


def test_a_renewal_answers_the_token_names_alone_never_a_static_secret(tmp_path):
    _write(tmp_path)
    bundle = _provision(tmp_path)
    env = _ask(bundle.env[mcp_gateway.TOKEN_RENEWAL_ENV])["env"]
    assert set(env) == {"GOOGLE_OAUTH_ACCESS_TOKEN", mcp_gateway.TOKEN_EXPIRES_ENV,
                        mcp_gateway.TOKEN_SCOPES_ENV, mcp_gateway.TOKEN_ACCOUNT_ENV}
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == ACCESS


def test_a_renewal_reads_the_new_token_after_the_refresh(tmp_path, monkeypatch):
    _write(tmp_path, seconds=100)
    bundle = _provision(tmp_path)

    async def _refreshes(_path):
        _write(tmp_path, access="ya29.the-new-one")
        return True
    monkeypatch.setattr(oauth_refresh_worker, "refresh_if_due", _refreshes)
    env = _ask(bundle.env[mcp_gateway.TOKEN_RENEWAL_ENV])["env"]
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == "ya29.the-new-one"


def test_the_renewal_always_asks_the_worker_for_the_one_file(tmp_path, monkeypatch):
    path = _write(tmp_path)
    bundle = _provision(tmp_path)
    asked = []

    async def _record(token_file):
        asked.append(token_file)
        return False
    monkeypatch.setattr(oauth_refresh_worker, "refresh_if_due", _record)
    _ask(bundle.env[mcp_gateway.TOKEN_RENEWAL_ENV])
    assert asked == [path]


def test_a_token_still_running_out_after_the_worker_answers_503(tmp_path):
    _write(tmp_path, seconds=100)
    bundle = _provision(tmp_path)
    assert _status(bundle.env[mcp_gateway.TOKEN_RENEWAL_ENV]) == 503


def test_a_refused_account_answers_409_with_the_wording(tmp_path):
    _kill(_write(tmp_path))
    bundle = _provision(tmp_path)
    with pytest.raises(HTTPException) as e:
        _ask(bundle.env[mcp_gateway.TOKEN_RENEWAL_ENV])
    assert e.value.status_code == 409 and "econnect" in str(e.value.detail)


def test_a_renewal_of_a_purged_session_or_another_tool_is_refused(tmp_path):
    _write(tmp_path)
    bundle = _provision(tmp_path)
    renewal = bundle.env[mcp_gateway.TOKEN_RENEWAL_ENV]
    mcp_broker.provision("s1", {"other": SecretBundle(env={"K": "v"})})
    assert _status(renewal) == 404  # the session holds no such tool now
    mcp_broker.provision("s1", {"gws": SecretBundle(env={"K": "v"})})
    assert _status(renewal) == 404  # the tool holds no token half
    _provision(tmp_path)
    mcp_broker.purge_session("s1")
    assert _status(renewal) == 404


def test_a_renewal_past_its_life_says_to_start_a_new_chat(tmp_path):
    _write(tmp_path)
    _provision(tmp_path)
    expired = tokens.mint(tokens.MCP_TOKEN, {"sid": "s1", "mcp": "gws"}, ttl_s=-5)
    with pytest.raises(HTTPException) as e:
        _ask(expired)
    assert e.value.status_code == 409
    assert e.value.detail == ("This tool has been running for more than a day, so its account token "
                              "cannot be renewed. Start a new chat, or close the app and open it again later.")


def test_only_a_genuine_renewal_past_its_life_has_ended():
    assert mcp_broker.renewal_ended(tokens.mint(tokens.MCP_TOKEN, {"sid": "s1", "mcp": "gws"}, ttl_s=-5))
    assert not mcp_broker.renewal_ended(mcp_broker.mint_renewal("s1", "gws"))
    assert not mcp_broker.renewal_ended(tokens.mint(tokens.MCP_CREDENTIALS, {"sid": "s1", "mcp": "gws"}, ttl_s=-5))
    assert not mcp_broker.renewal_ended("not-a-token")


def test_any_other_expired_token_stays_a_401():
    assert _status(tokens.mint(tokens.MCP_CREDENTIALS, {"sid": "s1", "mcp": "gws"}, ttl_s=-5)) == 401


# ── the worker's entry point ───────────────────────────────────────────────

def test_refresh_if_due_records_a_failure_in_the_backoff_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.undo()
    worker = oauth_refresh_worker
    path = tmp_path / "google-tokens" / "alice" / "a@b.test.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": _expires(10)}))
    seen = {}

    async def _boom(*, token_file, username, provider_id):
        seen.update(username=username, provider_id=provider_id, token_file=token_file)
        raise RuntimeError("the vendor is down")
    monkeypatch.setattr(worker, "_maybe_refresh_token_file", _boom)
    worker._failure_state.pop(str(path), None)
    try:
        assert asyncio.run(worker.refresh_if_due(path)) is False
        assert seen == {"username": "alice", "provider_id": "google", "token_file": path}
        assert worker._failure_state[str(path)]["failures"] == 1
    finally:
        worker._failure_state.pop(str(path), None)


# ── a restart ──────────────────────────────────────────────────────────────

def test_the_descriptor_keeps_the_token_half_without_a_value(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    cred = _cred(tmp_path)
    mcp_gateway.write_descriptor("s-1", {}, tokens={"gws": cred}, machine_id="m")
    doc = mcp_gateway.read_descriptor("s-1")
    assert doc["tokens"]["gws"].token_ref == cred.token_ref and doc["tokens"]["gws"].renews is True
    text = (config.SESSIONS_DIR / "mcp-gateway" / "s-1.json").read_text()
    assert ACCESS not in text and REFRESH not in text


def test_a_readopted_session_answers_its_tools_renewal(tmp_path, monkeypatch):
    import config
    from core.remote import mcp_gateway_push
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    _write(tmp_path)
    mcp_gateway.write_descriptor("s-1", {}, tokens={"gws": _cred(tmp_path)}, machine_id="m")
    renewal = mcp_broker.mint_renewal("s-1", "gws")
    assert asyncio.run(mcp_gateway_push.reprovision_adopted("s-1", "m")) == 0
    assert _ask(renewal)["env"]["GOOGLE_OAUTH_ACCESS_TOKEN"] == ACCESS


# ── the machine's disk ─────────────────────────────────────────────────────

# ── a person's own machine ─────────────────────────────────────────────────

def _bundles(token):
    return {"gws": SecretBundle(env={"T": "v"}, token=token)}


def test_a_bundle_whose_token_renews_stays_off_a_persons_machine(tmp_path, monkeypatch):
    from core.config import config_builder
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    monkeypatch.setattr(mcp_registry, "get_manifest_by_config_key", lambda key: SimpleNamespace(name="gws"))
    monkeypatch.setattr(credential_resolver, "carries_to_personal_machine", lambda m: True)
    target = SimpleNamespace(user_paired=True, machine_id="m")
    kept = config_builder.personal_machine_bundles(
        _bundles(_cred(tmp_path, renews=True)), target, True, "agent", who="w", owner="owner")
    assert kept == {}
    kept = config_builder.personal_machine_bundles(
        _bundles(_cred(tmp_path, renews=False)), target, True, "agent", who="w", owner="owner")
    assert set(kept) == {"gws"}


# ── a token the credential exit carries ────────────────────────────────────

def _covered(tmp_path: Path, *, sid: str = "s1", mcp: str = "gws") -> SecretBundle:
    """The bundle a build makes when the exit declares every name of the
    token: a placeholder in the token's place, the token by reference on
    the exit, no renewal."""
    from core.credentials import credential_exit
    from core.credentials.mcp_gateway import ExitCredential
    placeholder = credential_exit.new_placeholder()
    cred = _cred(tmp_path, renews=False)
    entry = ExitCredential(placeholder=placeholder, header="Authorization", prefix="Bearer ",
                           hosts=("gmail.googleapis.com",), allowlist_key=mcp, manifest=mcp,
                           env_name="GOOGLE_OAUTH_ACCESS_TOKEN", token_ref=cred.token_ref)
    bundle = SecretBundle(env={"GOOGLE_OAUTH_ACCESS_TOKEN": placeholder, "STATIC_KEY": "static"},
                          token=cred, exit=(entry,))
    mcp_broker.provision(sid, {mcp: bundle})
    return bundle


def test_the_spawn_env_keeps_the_placeholder_and_no_expiry_when_the_exit_carries_the_token(tmp_path):
    _write(tmp_path)
    bundle = _covered(tmp_path)
    env = _ask(mcp_broker.mint_token("s1", "gws"))["env"]
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == bundle.env["GOOGLE_OAUTH_ACCESS_TOKEN"]
    assert ACCESS not in json.dumps(env) and REFRESH not in json.dumps(env)
    assert mcp_gateway.TOKEN_EXPIRES_ENV not in env and mcp_gateway.TOKEN_RENEWAL_ENV not in env
    assert env["STATIC_KEY"] == "static"


def test_a_renewal_of_a_token_the_exit_carries_answers_404(tmp_path):
    """A tool that kept a renewal from before a rebuild asks: nothing to renew
    in the session, the same answer as for no credentials at all."""
    _write(tmp_path)
    _covered(tmp_path)
    assert _status(mcp_broker.mint_renewal("s1", "gws")) == 404
