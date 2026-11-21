"""The store reads of the relay calls, an OAuth exchange, the webhook
subscription manager and the subscriptions routes stay off the event loop.

Each flow runs on the test thread's loop with ``loop_db_guard.recording()``
around the exercised call and has no allowlist row, so it records nothing
(``tests/core/test_loop_store_inventory.py``). The recorder is pinned to the
test thread, so a route flow calls the route function directly. The relay is
available through its real gate; only the HTTP post is stubbed. A write that a
cancel must not lose is proven by holding it before it reaches the lane: a job
already running in a thread survives a cancel with or without the shield.
"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.core.test_loop_store_inventory import _check, _record


@pytest.fixture
def relay(temp_db, monkeypatch):
    """``_relay_post`` answering ``answer``; ``posted`` holds what it was sent."""
    import config
    from services.billing import relay_client
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://relay.test")
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", False)
    rig = SimpleNamespace(posted=[], answer={"account_token": "acct-new", "receipt": "r", "access_token": "at"})

    async def post(path, payload, *, sign_with=""):
        rig.posted.append((path, payload))
        return rig.answer

    monkeypatch.setattr(relay_client, "_relay_post", post)
    return rig


def _relay_calls(rc) -> dict:
    return {
        "activate_license": lambda: rc.activate_license("k"),
        "license_check": lambda: rc.license_check("k"),
        "deactivate_license": lambda: rc.deactivate_license("k"),
        "relay_credits": lambda: rc.relay_credits("user-admin"),
        "oauth_authorize_url": lambda: rc.oauth_authorize_url(
            provider_id="slack", scopes=["a"], state="s", install_callback="https://i.test/cb"),
        "oauth_exchange": lambda: rc.oauth_exchange(
            provider_id="slack", code="c", redirect_uri="https://i.test/cb", code_verifier=None),
        "oauth_refresh": lambda: rc.oauth_refresh(provider_id="slack", refresh_token="r"),
        "oauth_revoke": lambda: rc.oauth_revoke(provider_id="slack", token="t"),
        "events_register": lambda: rc.events_register(provider_id="slack", events_url="https://i.test/e"),
        "events_unbind": lambda: rc.events_unbind(provider_id="slack", sign_with="fs"),
        "account_connect_authorize_url": lambda: rc.account_connect_authorize_url(
            state="s", install_callback="https://i.test/cb"),
        "account_connect_exchange": lambda: rc.account_connect_exchange(code="c"),
        "account_disconnect": rc.account_disconnect,
    }


def test_relay_calls(relay, loop_db_guard):
    """All thirteen on a new install: the first mints the install id."""
    from services.billing import relay_client as rc
    from storage import database as db
    calls = _relay_calls(rc)
    assert len(calls) == 13
    recorded: set = set()
    for call in calls.values():
        recorded |= _record(loop_db_guard, call)
    recorded |= _record(loop_db_guard, calls["relay_credits"])  # disconnected now: the stub
    _check("relay calls", recorded)
    paths = [path for path, _ in relay.posted]
    assert len(paths) == 13 and paths.count("/v1/relay/credits") == 1
    install_id = db.get_platform_setting("install_id")
    assert install_id and all(body["install_id"] == install_id for _, body in relay.posted)
    assert dict(relay.posted)["/v1/relay/credits"]["account_token"] == "acct-new"
    assert dict(relay.posted)["/v1/connect/disconnect"]["account_token"] == "acct-new"
    assert rc.get_account_token() == ""


def test_relay_gate_refuses_off_the_loop(relay, monkeypatch, loop_db_guard):
    """The refusal itself reads the licence; a disconnect needs no relay and
    still clears the token."""
    import config
    from services.billing import relay_client as rc
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "")
    calls = _relay_calls(rc)
    disconnect = calls.pop("account_disconnect")
    recorded: set = set()
    for call in calls.values():
        async def refused(call=call):
            with pytest.raises(rc.RelayNotConfigured):
                await call()
        recorded |= _record(loop_db_guard, refused)
    rc.store_account_token("acct-old")
    recorded |= _record(loop_db_guard, disconnect)
    _check("relay gate", recorded)
    assert relay.posted == [] and rc.get_account_token() == ""


def test_two_first_relay_calls_share_one_install_id(temp_db, monkeypatch):
    """Both threads read no id before either writes: a licence binds to the
    id its activation sent, so the two answers and the row are one id."""
    from services.billing import relay_client as rc
    from storage import database as db
    real = db.get_platform_setting
    both_read = threading.Barrier(2, timeout=10)

    def read(key):
        value = real(key)
        if key == "install_id" and not value:
            both_read.wait()
        return value

    monkeypatch.setattr(db, "get_platform_setting", read)
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda _: rc.get_install_id(), range(2)))
    monkeypatch.setattr(db, "get_platform_setting", real)
    assert ids[0] and ids[0] == ids[1] == db.get_platform_setting("install_id")
    assert rc.get_install_id() == ids[0]


def test_an_empty_install_id_row_counts_as_absent(temp_db):
    from services.billing import relay_client as rc
    from storage import database as db
    db.set_platform_setting("install_id", "")
    minted = rc.get_install_id()
    assert minted and db.get_platform_setting("install_id") == minted


def _hold_job(monkeypatch, module, job) -> SimpleNamespace:
    """``module.run_db`` keeping ``job`` from its lane until ``go`` is set: the
    wait of a full lane, where a cancel of an unshielded await drops the job."""
    real = module.run_db
    hold = SimpleNamespace(reached=asyncio.Event(), go=asyncio.Event(), real=real)

    async def run_db(fn, /, *args, **kwargs):
        if fn is job:
            hold.reached.set()
            await hold.go.wait()
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(module, "run_db", run_db)
    return hold


async def _cancel_at_the_held_job(hold, start) -> None:
    task = asyncio.create_task(start())
    await asyncio.wait_for(hold.reached.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    hold.go.set()


async def _until(read, wanted) -> None:
    for _ in range(500):
        if await asyncio.to_thread(read) == wanted:
            return
        await asyncio.sleep(0.01)
    assert await asyncio.to_thread(read) == wanted


@pytest.mark.parametrize("start", ["activate_license", "account_connect_exchange"])
def test_a_token_the_relay_answered_survives_a_cancel(relay, monkeypatch, start):
    """The relay answers an account token once."""
    from services.billing import relay_client as rc
    calls = _relay_calls(rc)

    async def scenario():
        hold = _hold_job(monkeypatch, rc, rc.store_account_token)
        await _cancel_at_the_held_job(hold, calls[start])
        await _until(rc.get_account_token, "acct-new")

    asyncio.run(scenario())


def test_a_disconnect_clears_the_token_through_a_cancel(relay, monkeypatch):
    from services.billing import relay_client as rc
    rc.store_account_token("acct-old")

    async def scenario():
        hold = _hold_job(monkeypatch, rc, rc.clear_account_token)
        await _cancel_at_the_held_job(hold, rc.account_disconnect)
        await _until(rc.get_account_token, "")

    asyncio.run(scenario())


def test_a_disconnect_cancelled_during_the_revoke_still_clears(relay, monkeypatch):
    from services.billing import relay_client as rc
    rc.store_account_token("acct-old")

    async def scenario():
        reached = asyncio.Event()

        async def stuck(path, payload, *, sign_with=""):
            reached.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(rc, "_relay_post", stuck)
        task = asyncio.create_task(rc.account_disconnect())
        await asyncio.wait_for(reached.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(rc.get_account_token, "")

    asyncio.run(scenario())


def test_sync_provider_soon(relay, monkeypatch, loop_db_guard):
    """The gate reads the licence, inside the task: with a relay the register
    runs, with none the task ends with nothing sent."""
    import config
    from services.webhooks import relay_events
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://inst.test")

    async def soon():
        await relay_events.sync_provider_soon("slack")

    recorded = _record(loop_db_guard, soon)
    assert [path for path, _ in relay.posted] == ["/v1/events/register"]
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "")
    recorded |= _record(loop_db_guard, soon)
    _check("sync provider soon", recorded)
    assert len(relay.posted) == 1
    assert relay_events.sync_provider_soon("") is None
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    assert relay_events.sync_provider_soon("slack") is None


async def _settled(make_coro) -> None:
    """The call, then the relay's background registers it started: they run
    inside the recording too."""
    from core import background
    await make_coro()
    while tasks := background.running("relay-events"):
        await asyncio.gather(*tasks, return_exceptions=True)


def _oauth_rig(relay, monkeypatch, tmp_path, *, default_mode: str) -> SimpleNamespace:
    """A connect through a manifest whose mode is ``default_mode``: the vendor's
    calls are stubbed, the store reads and the persist are real."""
    from unittest.mock import AsyncMock

    import auth.oauth_providers as providers_pkg
    import config
    from auth.oauth_providers.base import TokenSet, UserInfo
    from auth.oauth_providers.slack import SlackOAuthProvider
    from services.mcp import mcp_registry
    from services.oauth import oauth_engine
    from storage.identity import credential_store
    from tests.fixtures.ws_dashboard_harness import set_username
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://inst.test")
    set_username("user-admin", "ada")
    provider = SlackOAuthProvider()
    provider.fetch_userinfo = AsyncMock(return_value=UserInfo(
        email="ada@acme.slack.com", name="ada", account_id="U1", raw={}))
    provider.exchange_code = AsyncMock(return_value=TokenSet(access_token="self-managed", raw={}))
    monkeypatch.setattr(providers_pkg, "get_provider", lambda pid: provider)
    credential_store.set_infra_credentials("inv-slack-app", {"INV_CLIENT_ID": "cid", "INV_CLIENT_SECRET": "sec"})
    manifest = SimpleNamespace(
        name="inv-slack", hosted=SimpleNamespace(oauth_app=SimpleNamespace(available=True, default_mode=default_mode)),
        credentials=SimpleNamespace(webhooks=None, oauth={
            "provider_id": "slack", "app_credential": "inv-slack-app",
            "app_credential_fields": [{"key": "INV_CLIENT_ID"}, {"key": "INV_CLIENT_SECRET"}],
        }))
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda name: manifest)

    async def exchange():
        token = oauth_engine.create_state(
            user_sub="user-admin", mcp_name="inv-slack", provider_id="slack",
            services=["channels"], redirect_uri="https://i.test/cb")
        return await oauth_engine.do_oauth_exchange(code="c", state_token=token)

    return SimpleNamespace(provider=provider, exchange=exchange)


def test_oauth_exchange_hosted(relay, monkeypatch, tmp_path, loop_db_guard):
    """The relay's exchange, then its provider's register in the background."""
    rig = _oauth_rig(relay, monkeypatch, tmp_path, default_mode="hosted")
    relay.answer = {"access_token": "xoxp-root", "token_type": "user", "raw": {
        "ok": True, "access_token": "xoxp-root", "token_type": "user", "via_relay": True,
        "team": {"id": "T_X", "name": "Acme"}, "authed_user": {"id": "U1", "scope": "chat:write"}}}
    recorded = _record(loop_db_guard, lambda: _settled(rig.exchange))
    _check("oauth exchange", recorded)
    assert [path for path, _ in relay.posted] == ["/v1/oauth/exchange", "/v1/events/register"]
    rig.provider.exchange_code.assert_not_called()


def test_oauth_exchange_self_managed(relay, monkeypatch, tmp_path, loop_db_guard):
    rig = _oauth_rig(relay, monkeypatch, tmp_path, default_mode="self")
    recorded = _record(loop_db_guard, lambda: _settled(rig.exchange))
    _check("oauth exchange", recorded)
    assert relay.posted == []
    sent = rig.provider.exchange_code.call_args.kwargs
    assert (sent["client_id"], sent["client_secret"]) == ("cid", "sec")


AUTO, MANUAL, HOSTED, APP = "inv-auto", "inv-manual", "inv-hosted", "inv-app"
VENDOR, RELAYED, APP_VENDOR = "invvendor", "invrelayed", "invapp"
AGENT = "inv-agent"
OWNER = "user-admin"


def _hooks_manifest(mode: str, provider: str, **block) -> SimpleNamespace:
    call = {"method": "POST", "url_template": "https://v.test/hooks", "expected_status": [200],
            "response_id_path": "id"}
    return SimpleNamespace(credentials=SimpleNamespace(oauth={"provider_id": provider}, webhooks={
        "available": True, "provider_id": provider,
        "event_catalog": [{"key": "push", "required_scopes": ["repo"]}],
        "vendor_target_spec": {"kind": "free_text", "label": "Target", "account_extra_key": "team_id"},
        "signature": {"per_subscription_secret": True},
        "registration": {"mode": mode, "lifetime_seconds": 3600, "create": call, "renew": call, "delete": call},
        **block,
    }))


def _token_file(provider: str, label: str, *, token: str = "tok", **extra) -> None:
    import json

    from services.oauth.oauth_account_store import account_token_path, get_token_dir
    path = account_token_path(get_token_dir("ada", provider_id=provider), label)
    path.write_text(json.dumps({"access_token": token, "scopes": ["repo"], "extra": extra}))


@pytest.fixture
def hooks(relay, monkeypatch, tmp_path):
    """The manager on real stores and token files; ``_call_vendor`` raises
    ``refuse`` or answers an id, and ``sync_provider_locked`` notes the relay
    rows it saw."""
    import config
    from services.mcp import mcp_registry
    from services.webhooks import relay_events, subscription_manager
    from storage.automation import webhook_subscription_store as store
    from tests.fixtures.ws_dashboard_harness import set_username
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://inst.test")
    set_username(OWNER, "ada")
    manifests = {
        AUTO: _hooks_manifest("auto", VENDOR),
        MANUAL: _hooks_manifest("manual", VENDOR),
        HOSTED: _hooks_manifest("manual", RELAYED, workspace_id_path="body.team_id"),
        APP: _hooks_manifest("manual", APP_VENDOR, app_webhook={"installation_id_path": "body.installation.id"}),
    }
    monkeypatch.setattr(mcp_registry, "get_manifest", manifests.get)
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: dict(manifests))
    _token_file(VENDOR, "acct", team_id="T1")
    _token_file(RELAYED, "hosted", via_relay=True)
    rig = SimpleNamespace(vendor=[], refuse=None, locked=[], relay=relay)

    async def call_vendor(*, call_block, row, access_token, extra_subs, account_extra=None, body_overrides=None):
        rig.vendor.append(SimpleNamespace(row=row, token=access_token, extra=account_extra))
        if rig.refuse is not None:
            raise rig.refuse
        return {"id": "hook-9"}

    async def sync_locked(provider_id):
        rows = await asyncio.to_thread(store.list_subscriptions, provider_id=provider_id, delivery_mode="relay")
        rig.locked.append((provider_id, len(rows)))

    monkeypatch.setattr(subscription_manager, "_call_vendor", call_vendor)
    monkeypatch.setattr(relay_events, "sync_provider_locked", sync_locked)
    return rig


def _create(mcp: str = AUTO, *, target: str = "o/r", label: str = "acct", scope: str = "user",
            agent: str | None = None):
    from services.webhooks import subscription_manager
    return subscription_manager.create_subscription(
        user_sub=OWNER, scope=scope, agent=agent, mcp_name=mcp, account_label=label,
        vendor_target=target, selected_events=["push"], caller_is_admin=True)


def _bind_agent(mcp: str = AUTO, label: str = "acct", **binding) -> None:
    from storage import database as db
    from storage.agents import agent_store
    from storage.identity import credential_store, service_account_set
    if agent_store.get_agent(AGENT) is None:
        agent_store.create_agent(AGENT, "Inv", created_by=OWNER)
        db.add_user_agent(OWNER, AGENT, "manager", "test")
    credential_store.set_user_credentials(OWNER, mcp, {"k": "v"}, account_label=label)
    assert service_account_set.add_service_account_to_agent(
        mcp, AGENT, account_label=label, owner_sub=OWNER, set_by=OWNER, **binding) == ""


def _seed_row(mcp: str = AUTO, *, target: str = "o/r", label: str = "acct", scope: str = "user",
              vendor_id: str = "hook-1", provider: str = VENDOR, **kw) -> str:
    """A row the vendor holds a hook for, as a finished create leaves it."""
    from storage.automation import webhook_subscription_store as store
    row = store.create_subscription(
        scope=scope, owner=OWNER if scope == "user" else "", agent=AGENT if scope == "service" else None,
        mcp_name=mcp, provider_id=provider, account_label=label, vendor_target=target,
        selected_events=["push"], selected_subevents={}, created_by=OWNER, signing_secret="s3cret", **kw)
    store.update_subscription_status(row["id"], store.ACTIVE, vendor_subscription_id=vendor_id or None)
    return row["id"]


def test_manager_create(hooks, loop_db_guard):
    """The three delivery modes, then an agent's: the binding is read too."""
    from storage.automation import webhook_subscription_store as store
    _bind_agent()
    recorded = _record(loop_db_guard, lambda: _create(AUTO))
    recorded |= _record(loop_db_guard, lambda: _create(MANUAL, target="m/r"))
    recorded |= _record(loop_db_guard, lambda: _create(HOSTED, label="hosted", target="h/r"))
    recorded |= _record(loop_db_guard, lambda: _create(AUTO, target="s/r", scope="service", agent=AGENT))
    _check("manager create", recorded)
    rows = {r["vendor_target"]: r for r in store.list_subscriptions()}
    assert {r["status"] for r in rows.values()} == {store.ACTIVE}
    assert rows["o/r"]["vendor_subscription_id"] == rows["s/r"]["vendor_subscription_id"] == "hook-9"
    assert rows["m/r"]["delivery_mode"] == "vendor" and rows["h/r"]["delivery_mode"] == "relay"
    assert hooks.locked == [(RELAYED, 1)] and len(hooks.vendor) == 2


def test_manager_create_refused(hooks, loop_db_guard):
    """``FAILED`` is on the row by the time the refusal reaches the caller."""
    from services.webhooks import subscription_manager as sm
    from storage.automation import webhook_subscription_store as store

    def attempt(refuse, raises, target):
        async def run():
            hooks.refuse = refuse
            with pytest.raises(raises):
                await _create(target=target)
            rows = await asyncio.to_thread(store.list_subscriptions, mcp_name=AUTO)
            assert {r["vendor_target"]: r["status"] for r in rows}[target] == store.FAILED
        return run

    refused = sm.VendorAPIError("no", vendor_status=403, vendor_body="no")
    recorded = _record(loop_db_guard, attempt(refused, sm.VendorAPIError, "a/r"))
    recorded |= _record(loop_db_guard, attempt(RuntimeError("down"), sm.SubscriptionError, "b/r"))
    _check("manager create", recorded)


def test_manager_delete(hooks, loop_db_guard):
    """A vendor row, a relay row and no row at all."""
    from services.webhooks import subscription_manager as sm
    from storage.automation import webhook_subscription_store as store
    vendor_row = _seed_row()
    relay_row = _seed_row(HOSTED, target="h/r", label="hosted", vendor_id="", provider=RELAYED, delivery_mode="relay")
    recorded: set = set()
    for sid in (vendor_row, relay_row, "no-such-row"):
        recorded |= _record(loop_db_guard, lambda sid=sid: _settled(
            lambda: sm.delete_subscription(subscription_id=sid)))
    _check("manager delete", recorded)
    assert store.list_subscriptions() == []
    assert [v.token for v in hooks.vendor] == ["tok"]
    assert [path for path, _ in hooks.relay.posted] == ["/v1/events/register"]


def test_manager_renew(hooks, loop_db_guard):
    """A renewal and a refused one: ``RENEW_FAILED`` is on the row by then."""
    from services.webhooks import subscription_manager as sm
    from storage.automation import webhook_subscription_store as store
    sid = _seed_row()

    async def refused():
        hooks.refuse = sm.VendorAPIError("no", vendor_status=403, vendor_body="no")
        with pytest.raises(sm.VendorAPIError):
            await sm.renew_subscription(sid)
        assert (await asyncio.to_thread(store.get_subscription, sid))["status"] == store.RENEW_FAILED

    async def unexpected():
        hooks.refuse = RuntimeError("down")
        with pytest.raises(sm.SubscriptionError):
            await sm.renew_subscription(sid)
        assert (await asyncio.to_thread(store.get_subscription, sid))["status"] == store.RENEW_FAILED

    recorded = _record(loop_db_guard, refused)
    recorded |= _record(loop_db_guard, unexpected)
    hooks.refuse = None
    recorded |= _record(loop_db_guard, lambda: sm.renew_subscription(sid))
    recorded |= _record(loop_db_guard, lambda: sm.renew_subscription("no-such-row"))
    _check("manager renew", recorded)
    row = store.get_subscription(sid)
    assert row["status"] == store.ACTIVE and row["expires_at"] and not row["last_error"]
    assert len(hooks.vendor) == 3


def test_manager_cleanup(hooks, loop_db_guard):
    """Every sweep: the vendor delete and the row delete are done by the time
    it returns, since the callers remove the token next."""
    from services.webhooks import subscription_manager as sm
    from storage.automation import webhook_subscription_store as store
    _bind_agent()
    _seed_row(target="u/r")
    recorded = _record(loop_db_guard, lambda: sm.cleanup_user_subscriptions(OWNER))
    assert len(hooks.vendor) == 1 and store.list_subscriptions() == []
    _seed_row(target="a/r", scope="service")
    recorded |= _record(loop_db_guard, lambda: sm.cleanup_agent_subscriptions(AGENT))
    assert len(hooks.vendor) == 2 and store.list_subscriptions() == []
    _seed_row(target="c/r")
    recorded |= _record(loop_db_guard, lambda: sm.cleanup_account_subscriptions(
        scope="user", owner=OWNER, mcp_name=AUTO, account_label="acct"))
    assert len(hooks.vendor) == 3 and store.list_subscriptions() == []
    _seed_row(target="l/r", scope="service")
    recorded |= _record(loop_db_guard, lambda: sm.cleanup_account_subscriptions(
        scope="service", owner="", mcp_name=AUTO, account_label="acct", agent=AGENT, token_owner_sub=OWNER))
    assert len(hooks.vendor) == 4 and store.list_subscriptions() == []
    _seed_row(target="m/r")
    recorded |= _record(loop_db_guard, lambda: sm.cleanup_mcp_subscriptions(AUTO))
    assert len(hooks.vendor) == 5 and store.list_subscriptions() == []
    _check("manager cleanup", recorded)


def test_create_keeps_its_order_around_the_vendor(hooks, monkeypatch):
    """The row with its secret is committed before the vendor is called (its
    first ping reads it), the token is read right before the call, and
    ``ACTIVE`` with the id lands before the final read."""
    from services.webhooks import subscription_manager as sm
    from storage.automation import webhook_subscription_store as store
    order: list[str] = []
    real_create, real_status, real_get = store.create_subscription, store.update_subscription_status, store.get_subscription
    real_token = sm._resolve_token_or_raise

    def create(**kw):
        row = real_create(**kw)
        order.append("row")
        return row

    def token(**kw):
        order.append("token")
        return real_token(**kw)

    def status(sid, new_status, **kw):
        order.append(f"{new_status}:{kw.get('vendor_subscription_id')}")
        return real_status(sid, new_status, **kw)

    def final(sid):
        order.append("final")
        return real_get(sid)

    async def call_vendor(*, row, **kw):
        seen = await asyncio.to_thread(real_get, row["id"])
        secret = await asyncio.to_thread(store.get_signing_secret, row["id"])
        order.append(f"vendor:{seen['status']}:{bool(secret)}")
        return {"id": "hook-9"}

    monkeypatch.setattr(store, "create_subscription", create)
    monkeypatch.setattr(store, "update_subscription_status", status)
    monkeypatch.setattr(store, "get_subscription", final)
    monkeypatch.setattr(sm, "_resolve_token_or_raise", token)
    monkeypatch.setattr(sm, "_call_vendor", call_vendor)
    asyncio.run(_create())
    assert order == ["row", "token", "vendor:creating:True", "active:hook-9", "final"]


def test_each_vendor_call_reads_the_token_of_its_moment(hooks):
    """A refresh between two calls: the next one sends the new token."""
    from services.webhooks import subscription_manager as sm
    from storage.automation import webhook_subscription_store as store
    asyncio.run(_create())
    sid = store.list_subscriptions()[0]["id"]
    _token_file(VENDOR, "acct", token="refreshed-1", team_id="T2")
    asyncio.run(sm.renew_subscription(sid))
    _token_file(VENDOR, "acct", token="refreshed-2", team_id="T3")
    asyncio.run(sm.delete_subscription(subscription_id=sid))
    assert [(v.token, v.extra["team_id"]) for v in hooks.vendor] == [
        ("tok", "T1"), ("refreshed-1", "T2"), ("refreshed-2", "T3")]


def test_the_vendor_id_survives_a_cancel(hooks, monkeypatch):
    """A cancel while the id's write waits for the lane: the id lands, and a
    delete reaches the vendor instead of reporting nothing left there."""
    from services.webhooks import subscription_manager as sm
    from storage.automation import webhook_subscription_store as store

    def vendor_id():
        rows = store.list_subscriptions(mcp_name=AUTO)
        return rows[0]["vendor_subscription_id"] if rows else None

    async def scenario():
        hold = _hold_job(monkeypatch, sm, store.update_subscription_status)
        await _cancel_at_the_held_job(hold, _create)
        await _until(vendor_id, "hook-9")
        sid = (await asyncio.to_thread(store.list_subscriptions, mcp_name=AUTO))[0]["id"]
        return await sm.delete_subscription(subscription_id=sid)

    assert asyncio.run(scenario()) == (True, True)
    assert len(hooks.vendor) == 2


def test_a_hosted_subscription_is_active_through_a_cancel(hooks, monkeypatch):
    """No vendor call to come back to: a row left ``creating`` receives nothing
    for good, and a retry gets a conflict."""
    from services.webhooks import subscription_manager as sm
    from storage.automation import webhook_subscription_store as store

    def status():
        rows = store.list_subscriptions(mcp_name=HOSTED)
        return rows[0]["status"] if rows else None

    async def scenario():
        hold = _hold_job(monkeypatch, sm, store.update_subscription_status)
        await _cancel_at_the_held_job(hold, lambda: _create(HOSTED, target="h/r", label="hosted"))
        await _until(status, store.ACTIVE)

    asyncio.run(scenario())


def test_a_relay_delete_syncs_on_the_loop_after_the_row(hooks, monkeypatch):
    """``sync_provider_soon`` starts a task: never from a store job."""
    from services.webhooks import relay_events, subscription_manager as sm
    from storage.automation import webhook_subscription_store as store
    sid = _seed_row(HOSTED, target="h/r", label="hosted", vendor_id="", provider=RELAYED, delivery_mode="relay")
    seen = []

    def soon(provider_id):
        seen.append((provider_id, threading.get_ident(), store.get_subscription(sid)))

    monkeypatch.setattr(relay_events, "sync_provider_soon", soon)
    assert asyncio.run(sm.delete_subscription(subscription_id=sid)) == (True, True)
    assert seen == [(RELAYED, threading.get_ident(), None)]


def _admin():
    from auth.providers import UserContext
    return UserContext(sub=OWNER, email="a@t.com", name="Admin", role="admin")


def _member(role: str):
    from auth.providers import UserContext
    return UserContext(sub="user-viewer", email="v@t.com", name="Viewer", role="member",
                       agents=[AGENT], agent_roles={AGENT: role})


def _stranger():
    from auth.providers import UserContext
    return UserContext(sub="user-viewer2", email="v2@t.com", name="Two", role="member")


def _answered(loop_db_guard, recorded: set, make_coro):
    """``_record`` keeping the call's answer."""
    answers: list = []

    async def call():
        answers.append(await make_coro())

    recorded.update(_record(loop_db_guard, call))
    return answers[0]


def _refused(loop_db_guard, recorded: set, make_coro) -> int:
    """``_record`` of a call the route refuses: its status."""
    from fastapi import HTTPException
    statuses: list = []

    async def call():
        with pytest.raises(HTTPException) as refused:
            await make_coro()
        statuses.append(refused.value.status_code)

    recorded.update(_record(loop_db_guard, call))
    return statuses[0]


def test_event_catalog(hooks, loop_db_guard):
    """A person's account, an account of the agent's set (its label checked,
    a manager's alone), a GitHub-App account's installations, and no account
    at all."""
    from api.events.subscriptions import get_event_catalog
    from storage import database as db
    from storage.identity import credential_store
    _bind_agent()
    db.add_user_agent("user-viewer", AGENT, "viewer", "test")
    installs = [{"id": 10, "account": "ada", "admin": True, "repository_selection": "all"}]
    _token_file(APP_VENDOR, "app")
    _bind_agent(APP, "app")
    credential_store.set_account_meta(OWNER, APP, "app", {"kind": "github_app", "hosted": True, "installations": installs})
    recorded: set = set()

    def ask(mcp=AUTO, user=None, **kw):
        args = {"account_label": "acct", "scope": "user", "agent": None, **kw}
        return lambda: get_event_catalog(mcp_name=mcp, user=user or _admin(), **args)

    own = _answered(loop_db_guard, recorded, ask())
    assert (own["vendor_target_prefill"], own["granted_scopes"], own["registration"]["mode"]) == ("T1", ["repo"], "auto")
    bound = _answered(loop_db_guard, recorded, ask(scope="service", agent=AGENT))
    assert (bound["vendor_target_prefill"], bound["granted_scopes"]) == ("T1", ["repo"])
    # An account of the agent's set is read for a manager alone.
    assert _refused(loop_db_guard, recorded, ask(user=_member("viewer"), scope="service", agent=AGENT)) == 403
    assert _refused(loop_db_guard, recorded, ask(account_label="other", scope="service", agent=AGENT)) == 400
    app = _answered(loop_db_guard, recorded, ask(APP, account_label="app", scope="service", agent=AGENT))
    assert app["target_kind"] == "installation" and app["vendor_target_spec"]["static_options"][0]["value"] == "10"
    own_app = _answered(loop_db_guard, recorded, ask(APP, account_label="app"))
    assert own_app["target_kind"] == "installation"
    bare = _answered(loop_db_guard, recorded, ask(account_label=None, scope=None))
    assert (bare["registration"]["mode"], bare["target_kind"], bare["vendor_target_prefill"]) == ("auto", "", "")
    _check("event catalog", recorded)


def test_subscriptions_list(hooks, loop_db_guard):
    """The full table and a member's view: the creators' names and the MCPs'
    state are read for each."""
    from api.events.subscriptions import list_subscriptions
    from storage import database as db
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Inv", created_by=OWNER)
    db.add_user_agent("user-viewer", AGENT, "viewer", "test")
    _seed_row(target="own/r")
    agents_row = _seed_row(target="agent/r", scope="service")
    recorded: set = set()

    def ask(user, **kw):
        args = {"scope": None, "agent": None, "mcp_name": None, "provider_id": None, "account_label": None, **kw}
        return lambda: list_subscriptions(user=user, **args)

    everything = _answered(loop_db_guard, recorded, ask(_admin()))["subscriptions"]
    assert len(everything) == 2 and {r["mcp_state"] for r in everything} == {"on"}
    assert {r["created_by_name"] for r in everything} == {"Admin User"}
    seen = _answered(loop_db_guard, recorded, ask(_member("viewer"), scope="service", mcp_name=AUTO,
                                                  provider_id=VENDOR, account_label="acct"))
    assert [r["id"] for r in seen["subscriptions"]] == [agents_row]
    _check("subscriptions list", recorded)


def test_subscription_routes(hooks, loop_db_guard):
    """Create (the agent's mode is read for a service row), get, renew, the
    signing secret and delete, with their refusals."""
    from api.events import subscriptions as routes
    from storage.automation import trigger_store, webhook_subscription_store as store
    _bind_agent()
    recorded: set = set()
    body = routes.CreateSubscriptionRequest(
        scope="service", agent=AGENT, mcp_name=AUTO, account_label="acct", vendor_target="o/r", selected_events=["push"])
    made = _answered(loop_db_guard, recorded, lambda: routes.create_subscription(body, user=_admin(), x_on_behalf_of=None))
    sid = made["id"]
    assert made["status"] == store.ACTIVE
    refused = routes.CreateSubscriptionRequest(
        scope="service", agent=AGENT, mcp_name=AUTO, account_label="acct", vendor_target="v/r", selected_events=["push"])
    assert _refused(loop_db_guard, recorded, lambda: routes.create_subscription(
        refused, user=_member("viewer"), x_on_behalf_of=None)) == 403

    got = _answered(loop_db_guard, recorded, lambda: routes.get_subscription(sid, user=_admin()))
    assert got["id"] == sid
    assert _refused(loop_db_guard, recorded, lambda: routes.get_subscription("no-such-row", user=_admin())) == 404
    assert _refused(loop_db_guard, recorded, lambda: routes.get_subscription(sid, user=_stranger())) == 403

    renewed = _answered(loop_db_guard, recorded, lambda: routes.renew_subscription(sid, user=_admin()))
    assert renewed["status"] == store.ACTIVE
    assert _refused(loop_db_guard, recorded, lambda: routes.renew_subscription(sid, user=_member("viewer"))) == 403

    secret = _answered(loop_db_guard, recorded, lambda: routes.get_subscription_signing_secret(sid, user=_admin()))
    assert secret["signing_secret"]
    assert _refused(loop_db_guard, recorded, lambda: routes.get_subscription_signing_secret(
        sid, user=_member("viewer"))) == 403

    trigger_store.create_trigger(
        slug="on-push", name="On push", scope="agent", agent=AGENT, created_by=OWNER, subscription_id=sid,
        notify_enabled=True, notify_title="t", notify_body="b")
    assert _refused(loop_db_guard, recorded, lambda: routes.delete_subscription(
        sid, force=False, user=_admin(), x_on_behalf_of=None)) == 409
    assert _answered(loop_db_guard, recorded, lambda: routes.delete_subscription(
        sid, force=True, user=_admin(), x_on_behalf_of=None)) == {"deleted": True, "vendor_detached": True}
    assert store.get_subscription(sid) is None
    assert len(hooks.vendor) == 3
    _check("subscription routes", recorded)


def test_billing_balance(temp_db, monkeypatch, loop_db_guard):
    """The balance route's gate reads the licence and the account token."""
    from api.billing import billing
    from services.billing import relay_client
    monkeypatch.setattr(billing, "require_auth", lambda user: user)
    monkeypatch.setattr(relay_client, "relay_credits", AsyncMock(return_value={"balance": 1}))
    user = SimpleNamespace(sub="user-admin")
    recorded = _record(loop_db_guard, lambda: billing.get_user_credits(user))
    _check("billing balance", recorded)


def test_the_relay_gates_read_off_the_loop(temp_db, monkeypatch, loop_db_guard):
    """``sync_all``, ``sync_all_soon``, ``sync_providers_of_mcp_soon`` and an
    account's unbind ask whether the relay is offered in a worker thread."""
    import config
    from services.billing import relay_client
    from services.webhooks import relay_events
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://example.test")
    monkeypatch.setattr(relay_client, "is_available", _refuses_the_loop_gate)
    monkeypatch.setattr(relay_events.mcp_registry, "get_manifest", lambda name: SimpleNamespace())
    monkeypatch.setattr(relay_events, "manifest_providers", lambda manifest: {"github"})

    async def scenario():
        await relay_events.sync_all()
        await relay_events.unbind_account("github", {"extra": {"via_relay": True}}, revoked=True)
        relay_events.sync_all_soon()
        relay_events.sync_providers_of_mcp_soon("github-mcp")
        await asyncio.sleep(0.05)
    recorded = _record(loop_db_guard, scenario)
    _check("relay gates", recorded)


def _refuses_the_loop_gate() -> bool:
    """A store read in disguise: on the event loop it would be the read this row moved."""
    from storage import database as db
    db.get_platform_setting("license_key")
    return False
