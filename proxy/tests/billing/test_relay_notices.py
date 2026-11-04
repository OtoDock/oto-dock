"""The relay's account-removed notice (``services/webhooks/relay_notices``
and its route in ``api/events/webhooks.py``): the signature before anything
else, the three refusals, the accounts the notice names (by the exchange's
pair or the derived one, bound before the removal), what goes, the answer
before the network, a notice seen twice. Token files live in a scratch
folder; the stores, the person's notice and the relay are faked."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

import config
from services.billing import relay_client
from services.mcp import mcp_registry
from services.webhooks import relay_notices, webhook_dispatcher
from storage import database as db
from storage.identity import credential_store

SECRET = "forward-secret-for-tests"
INSTALL = "inst-1"
NOTICE_ID = "0123456789abcdef0123456789abcdef"


def _manifest(name, provider, label):
    return SimpleNamespace(name=name, label=label,
                           credentials=SimpleNamespace(oauth={"provider_id": provider}, webhooks=None))


class Env:
    def __init__(self, monkeypatch, tmp_path):
        self.tmp = tmp_path
        self.settings: dict[str, str] = {}
        self.meta: dict[tuple, dict] = {}
        self.rows: dict[tuple[str, str], set[str]] = {}       # (sub, label) -> MCPs naming it
        self.agent_bindings: dict[tuple[str, str], list[dict]] = {}
        self.service_bindings: dict[str, list[dict]] = {}
        self.removed_bindings: list[tuple] = []
        self.cleanups: list[dict] = []
        self.notices: list[dict] = []
        self.ticks: list[int] = []
        self.manifests = {
            "slack-mcp": _manifest("slack-mcp", "slack", "Slack"),
            "fb-mcp": _manifest("fb-mcp", "facebook", "Facebook"),
            "ig-mcp": _manifest("ig-mcp", "facebook", "Instagram"),
            "zoom-mcp": _manifest("zoom-mcp", "zoom", "Zoom"),
        }
        mp = monkeypatch
        mp.setattr(config, "SESSIONS_DIR", tmp_path)
        mp.setattr(webhook_dispatcher, "_read_forward_secret", lambda: SECRET)
        webhook_dispatcher.reset_caches()
        mp.setattr(relay_client, "get_install_id", lambda: INSTALL)
        mp.setattr(relay_client, "is_available", lambda: True)

        async def no_relay(*a, **kw):
            raise AssertionError("a notice never calls the relay")
        mp.setattr(relay_client, "_relay_post", no_relay)
        mp.setattr(db, "get_platform_setting", lambda key: self.settings.get(key, ""))
        mp.setattr(db, "set_platform_setting", self.settings.__setitem__)
        mp.setattr(db, "get_user_sub_by_username", {"alice": "sub-a", "bob": "sub-b"}.get)
        mp.setattr(mcp_registry, "get_manifest", self.manifests.get)
        mp.setattr(mcp_registry, "get_mcps_by_provider",
                   lambda pid: [m for m in self.manifests.values() if m.credentials.oauth["provider_id"] == pid])
        mp.setattr(credential_store, "mcps_naming_label",
                   lambda sub, label: sorted(self.rows.get((sub, label), set())))

        def update_meta(sub, mcp, label, change):
            new = change(dict(self.meta.get((sub, mcp, label), {})))
            if new is not None:  # None leaves the row, as the store does
                self.meta[(sub, mcp, label)] = new
            return self.meta.get((sub, mcp, label), {})
        mp.setattr(credential_store, "update_account_meta", update_meta)
        mp.setattr(credential_store, "get_account_meta", lambda sub, mcp, label: dict(self.meta.get((sub, mcp, label), {})))
        mp.setattr(credential_store, "list_agent_account_bindings",
                   lambda sub, mcp: list(self.agent_bindings.get((sub, mcp), [])))
        mp.setattr(credential_store, "remove_account_agent_binding",
                   lambda sub, mcp, agent: self.removed_bindings.append(("agent", sub, mcp, agent)))
        mp.setattr(credential_store, "list_service_agent_bindings", lambda mcp: list(self.service_bindings.get(mcp, [])))
        mp.setattr(credential_store, "remove_service_agent_binding",
                   lambda mcp, agent, **kw: self.removed_bindings.append(("service", mcp, agent, kw)))

        from services.webhooks import subscription_manager

        async def cleanup(**kw):
            self.cleanups.append(kw)
            return 0
        mp.setattr(subscription_manager, "cleanup_account_subscriptions", cleanup)
        from services.notifications import notification_manager

        async def fire(**kw):
            self.notices.append(kw)
            return []
        mp.setattr(notification_manager, "fire_notification", fire)
        from core.remote import mcp_gateway_push
        mp.setattr(mcp_gateway_push, "tick_soon", lambda: self.ticks.append(1))

    def account(self, provider, user, label, extra, *, mcps=()):
        d = self.tmp / f"{provider}-tokens" / user
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{label}.json"
        path.write_text(json.dumps({"provider": provider, "access_token": "x", "account_id": "id",
                                    "extra": {"via_relay": True, **extra}}))
        sub = {"alice": "sub-a", "bob": "sub-b"}[user]
        self.rows[(sub, label)] = set(mcps)
        return path


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path)


def _body(**over):
    body = {"type": "account_removed", "notice_id": NOTICE_ID, "install_id": INSTALL, "provider": "slack",
            "scope_id": "T1", "user_key": "U1", "reason": "tokens_revoked", "removed_at": 1_700_000_000}
    body.update(over)
    return json.dumps(body).encode()


def _sign(raw, *, secret=SECRET, ts=None, template="v0:notice:{ts}:"):
    ts = str(int(time.time())) if ts is None else str(ts)
    mac = hmac.new(secret.encode(), template.format(ts=ts).encode() + raw, hashlib.sha256).hexdigest()
    return {"X-OtoDock-Notice-Timestamp": ts, "X-OtoDock-Notice-Signature": "v0=" + mac}


async def _drain():
    from core import background
    for _ in range(20):
        tasks = background.running("relay-notices")
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _receive(provider, raw, headers, *, drain=True):
    async def run():
        out = await relay_notices.receive(provider, raw, headers)
        if drain:
            await _drain()
        return out
    return asyncio.run(run())


SLACK = {"relay_binding": {"scope_id": "T1", "user_key": "U1", "bound_at": 1_600_000_000}}


# --- the signature, before anything else ------------------------------------------

def test_a_good_signature_is_taken(env):
    path = env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    raw = _body()
    status, body, _ = _receive("slack", raw, _sign(raw))
    assert (status, body) == (200, {"ok": True, "removed": 1})
    assert not path.exists()


@pytest.mark.parametrize("headers", [
    lambda raw: _sign(raw, secret="another-secret"),
    lambda raw: _sign(raw, ts=int(time.time()) - 301),
    lambda raw: _sign(raw, template="v0:{ts}:"),               # a forward's signature
    lambda raw: _sign(raw, template="v0:gone:{ts}:"),          # the 410 mark's shape
    lambda raw: {},
])
def test_a_bad_signature_is_401_and_touches_nothing(env, headers):
    path = env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    raw = _body()
    status, body, _ = _receive("slack", raw, headers(raw))
    assert status == 401 and body["error"] == "notice signature verification failed"
    assert path.exists() and env.settings == {}


@pytest.mark.parametrize("raw,error", [
    (_body(install_id="inst-other"), "the notice is for another install"),
    (_body(provider="zoom"), "provider mismatch"),
    (b"not json", "malformed notice"),
    (_body(type="other"), "malformed notice"),
    (_body(notice_id="ABC"), "malformed notice"),
    (_body(scope_id=""), "malformed notice"),
    (_body(removed_at=True), "malformed notice"),
    (_body(reason="Deauthorized!"), "malformed notice"),
    (_body(user_key=""), "malformed notice"),
    (b'["account_removed"]', "malformed notice"),
    (_body(removed_at=float("inf")), "malformed notice"),
    (_body(removed_at=float("-inf")), "malformed notice"),
    (_body(removed_at=float("nan")), "malformed notice"),
    (_body(removed_at=-5), "malformed notice"),
    (_body(removed_at="1700000000"), "malformed notice"),
    (_body(removed_at=1_700_000_000.5), "malformed notice"),
])
def test_the_three_refusals_are_400(env, raw, error):
    path = env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    status, body, _ = _receive("slack", raw, _sign(raw))
    assert status == 400 and body["error"] == error
    assert path.exists() and env.settings == {}


def test_removed_at_whole_seconds_as_a_float_is_taken(env):
    path = env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    raw = _body(removed_at=1_700_000_000.0)
    assert _receive("slack", raw, _sign(raw))[1] == {"ok": True, "removed": 1}
    assert not path.exists()


def test_a_removal_later_than_the_relays_receipt_is_refused(env):
    """The vendor's time is never later than the relay's receipt, which is
    at most the window before the signed timestamp: past it is a relay
    fault, answered 400 (final) before any file is read."""
    path = env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    now = int(time.time())
    raw = _body(removed_at=now + 1_000)
    status, body, _ = _receive("slack", raw, _sign(raw, ts=now))
    assert (status, body["reason"]) == (400, "bad removed_at")
    assert path.exists() and env.settings == {}
    raw = _body(removed_at=now + 100)
    assert _receive("slack", raw, _sign(raw, ts=now))[1] == {"ok": True, "removed": 1}


# --- the accounts the notice names --------------------------------------------------

def test_the_pair_from_the_exchange_or_the_derived_one(env):
    by_binding = env.account("slack", "alice", "A", SLACK, mcps=["slack-mcp"])
    derived = env.account("slack", "bob", "B", {"team": {"id": "T1"}, "authed_user": {"id": "U1"}}, mcps=["slack-mcp"])
    other = env.account("slack", "alice", "C", {"relay_binding": {"scope_id": "T1", "user_key": "U2"}})
    self_managed = env.tmp / "slack-tokens" / "alice" / "D.json"
    self_managed.write_text(json.dumps({"extra": {"team_id": "T1", "user_id": "U1"}}))
    raw = _body()
    assert _receive("slack", raw, _sign(raw))[1] == {"ok": True, "removed": 2}
    assert not by_binding.exists() and not derived.exists()
    assert other.exists() and self_managed.exists()


def test_zoom_by_its_derived_pair(env):
    path = env.account("zoom", "alice", "Z", {"account_id": "A1", "id": "Z1"}, mcps=["zoom-mcp"])
    raw = _body(provider="zoom", scope_id="A1", user_key="Z1", reason="deauthorized")
    assert _receive("zoom", raw, _sign(raw))[1]["removed"] == 1 and not path.exists()


def test_facebook_matches_only_through_its_relay_binding(env):
    old = env.account("facebook", "alice", "Old", {"id": "M1"}, mcps=["fb-mcp"])
    new = env.account("facebook", "bob", "New", {"relay_binding": {"scope_id": "app", "user_key": "M1"}},
                      mcps=["fb-mcp", "ig-mcp"])
    raw = _body(provider="facebook", scope_id="app", user_key="M1", reason="data_deletion")
    assert _receive("facebook", raw, _sign(raw))[1]["removed"] == 1
    assert old.exists() and not new.exists()


@pytest.mark.parametrize("bound_at,taken", [(1_699_999_999, True), (1_700_000_000, False),
                                             (1_700_000_500, False), (None, True)])
def test_a_file_connected_again_after_the_removal_keeps_its_grant(env, bound_at, taken):
    binding = {"scope_id": "T1", "user_key": "U1"}
    if bound_at is not None:
        binding["bound_at"] = bound_at
    path = env.account("slack", "alice", "Work", {"relay_binding": binding}, mcps=["slack-mcp"])
    raw = _body(removed_at=1_700_000_000)
    assert _receive("slack", raw, _sign(raw))[1]["removed"] == (1 if taken else 0)
    assert path.exists() is not taken


# --- what goes, and what is told after the answer -----------------------------------

def test_the_file_the_pending_unbind_the_row_and_the_agent_choice(env):
    env.account("facebook", "alice", "Ann", {"relay_binding": {"scope_id": "app", "user_key": "M1"}},
                mcps=["fb-mcp", "ig-mcp"])
    env.settings["relay_events_pending_unbinds"] = json.dumps([
        {"provider_id": "facebook", "scope_id": "app", "user_key": "M1"},
        {"provider_id": "slack", "scope_id": "T9", "user_key": "U9"}])
    env.agent_bindings[("sub-a", "fb-mcp")] = [{"agent_name": "pa", "account_label": "Ann"},
                                                {"agent_name": "other", "account_label": "Bob"}]
    env.service_bindings["ig-mcp"] = [{"agent_name": "sales", "account_owner_sub": "sub-a", "account_label": "Ann"}]
    raw = _body(provider="facebook", scope_id="app", user_key="M1", reason="deauthorized", removed_at=1_234)
    assert _receive("facebook", raw, _sign(raw))[1] == {"ok": True, "removed": 1}
    assert json.loads(env.settings["relay_events_pending_unbinds"]) == [
        {"provider_id": "slack", "scope_id": "T9", "user_key": "U9"}]
    for mcp in ("fb-mcp", "ig-mcp"):
        assert env.meta[("sub-a", mcp, "Ann")] == {relay_notices.REMOVED_REASON: {"reason": "deauthorized", "at": 1234}}
        assert relay_notices.removed_reason("sub-a", mcp, "Ann") == relay_notices.REMOVED_REASON
    assert relay_notices.removed_reason("sub-a", "fb-mcp", "Bob") == ""
    assert ("agent", "sub-a", "fb-mcp", "pa") in env.removed_bindings
    assert not any(b[:4] == ("agent", "sub-a", "fb-mcp", "other") for b in env.removed_bindings)
    assert ("service", "ig-mcp", "sales", {"owner_sub": "sub-a", "account_label": "Ann"}) in env.removed_bindings
    assert {(c["scope"], c["mcp_name"]) for c in env.cleanups} == {
        ("user", "fb-mcp"), ("user", "ig-mcp"), ("service", "ig-mcp")}
    assert len(env.notices) == 1
    n = env.notices[0]
    assert n["target"] == "sub-a" and n["scope"] == "user" and n["href"] == "/user-settings/integrations"
    assert n["title"] == "Ann was removed at Facebook"
    assert n["body"].startswith("OtoDock's app was removed from this account at Facebook.")
    assert env.ticks == [1]


def test_a_new_sign_in_clears_the_mark_on_every_row_of_its_file(env, monkeypatch):
    """The notice marks every MCP's row that reads the file (one login, the
    provider's MCPs). A Reconnect through one card writes that file anew, so
    no row reading it keeps saying the account was removed at the vendor,
    though only the card's own row is written: else a later loss of the
    file would show the notice for an account the vendor never removed."""
    from auth.oauth_providers.base import TokenSet, UserInfo
    from services.oauth import oauth_account_store
    env.account("facebook", "alice", "Ann", {"relay_binding": {"scope_id": "app", "user_key": "M1"}},
                mcps=["fb-mcp", "ig-mcp"])
    env.meta[("sub-a", "ig-mcp", "Ann")] = {"page": "kept"}
    raw = _body(provider="facebook", scope_id="app", user_key="M1", reason="deauthorized", removed_at=1_234)
    assert _receive("facebook", raw, _sign(raw))[1]["removed"] == 1
    assert relay_notices.removed_reason("sub-a", "ig-mcp", "Ann") == relay_notices.REMOVED_REASON

    monkeypatch.setattr(db, "get_username_by_sub", {"sub-a": "alice"}.get)
    monkeypatch.setattr(credential_store, "set_user_credentials", lambda *a, **kw: None)
    monkeypatch.setattr(credential_store, "set_account_display_email", lambda *a, **kw: None)
    oauth_account_store.persist_oauth_account(
        user_sub="sub-a", mcp_name="fb-mcp", provider_id="facebook", account_label="Ann",
        services=[], token_set=TokenSet(access_token="at-new", raw={
            "via_relay": True, "relay_binding": {"scope_id": "app", "user_key": "M1", "bound_at": 2_000}}),
        userinfo=UserInfo(email="ann@example.com", account_id="M1"),
        client_id="", client_secret="", token_url="")
    assert (env.tmp / "facebook-tokens" / "alice" / "Ann.json").exists()
    for mcp in ("fb-mcp", "ig-mcp"):
        assert relay_notices.removed_reason("sub-a", mcp, "Ann") == ""
    assert env.meta[("sub-a", "ig-mcp", "Ann")] == {"page": "kept"}


def test_the_answer_comes_before_anything_networked(env):
    env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    raw = _body()

    async def run():
        out = await relay_notices.receive("slack", raw, _sign(raw))
        before = (list(env.cleanups), list(env.notices), list(env.ticks))
        await _drain()
        return out, before
    (status, body, _), before = asyncio.run(run())
    assert status == 200 and body["removed"] == 1
    assert before == ([], [], [])
    assert env.cleanups and env.notices and env.ticks


def test_a_pass_past_the_budget_keeps_the_after_work_of_what_it_removed(env, monkeypatch):
    """Answered busy past the budget: the file already gone still gets its
    cleanup and its person's notice; the relay's next try finds the rest."""
    import threading
    files = {"sub-a": env.account("slack", "alice", "A", SLACK, mcps=["slack-mcp"]),
             "sub-b": env.account("slack", "bob", "B", SLACK, mcps=["slack-mcp"])}
    monkeypatch.setattr(relay_notices, "_LOCAL_DEADLINE_S", 0.3)
    real = relay_notices._still_matches
    slow = threading.Event()
    seen: list = []

    def still_matches(notice, path):
        seen.append(path)
        if len(seen) == 2:
            slow.wait(2.0)               # past the budget; then it leaves the file
            return False
        return real(notice, path)
    monkeypatch.setattr(relay_notices, "_still_matches", still_matches)
    raw = _body()
    status, body, _ = _receive("slack", raw, _sign(raw))
    slow.set()
    assert (status, body) == (503, {"error": "busy"})
    gone = [sub for sub, path in files.items() if not path.exists()]
    assert len(gone) == 1 and seen[0] == files[gone[0]]
    assert [n["target"] for n in env.notices] == gone
    assert [c["owner"] for c in env.cleanups] == gone
    assert "relay_notices_seen" not in env.settings


def test_the_same_notice_twice_removes_nothing_more(env):
    env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    raw = _body()
    assert _receive("slack", raw, _sign(raw))[1]["removed"] == 1
    again = env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    assert _receive("slack", raw, _sign(raw))[1] == {"ok": True, "removed": 0}
    assert again.exists() and len(env.notices) == 1


def test_no_account_matched_is_still_a_200_never_the_410(env):
    raw = _body(scope_id="T-none")
    status, body, headers = _receive("slack", raw, _sign(raw))
    assert (status, body) == (200, {"ok": True, "removed": 0})
    assert "x-otodock-gone" not in {k.lower() for k in headers}
    assert env.notices == [] and env.ticks == []


def test_the_seen_record_is_bounded(env, monkeypatch):
    monkeypatch.setattr(relay_notices, "_SEEN_MAX", 3)
    for i in range(5):
        relay_notices._remember(f"{i:032x}")
    assert json.loads(env.settings["relay_notices_seen"]) == [f"{i:032x}" for i in (2, 3, 4)]


# --- the route --------------------------------------------------------------------

def test_the_route_answers_and_counts_no_refusal(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.events import webhooks as route
    from auth import rate_limiter
    counted: list[str] = []
    monkeypatch.setattr(route, "client_address", lambda request: SimpleNamespace(bucket_key="203.0.113.9", shared=False))
    monkeypatch.setattr(rate_limiter, "check_rate_limit", lambda *a: (True, 0))
    monkeypatch.setattr(rate_limiter, "record_attempt", lambda *a: counted.append(a[1]))
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    env.account("slack", "alice", "Work", SLACK, mcps=["slack-mcp"])
    raw = _body()
    url = "/v1/webhooks/relay/slack/account-removed"
    assert client.post(url, content=raw, headers={**_sign(raw, secret="x"), "Content-Type": "application/json"}).status_code == 401
    bad = _body(install_id="nope")
    assert client.post(url, content=bad, headers={**_sign(bad), "Content-Type": "application/json"}).status_code == 400
    resp = client.post(url, content=raw, headers={**_sign(raw), "Content-Type": "application/json"})
    assert resp.status_code == 200 and resp.json() == {"ok": True, "removed": 1}
    assert counted == []
    # A notice is under a kilobyte: a larger body is never read past the cap.
    big = _body(reason="x" * (relay_notices.BODY_CAP + 10))
    assert client.post(url, content=big, headers={**_sign(big), "Content-Type": "application/json"}).status_code == 413
