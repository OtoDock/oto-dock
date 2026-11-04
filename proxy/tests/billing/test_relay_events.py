"""This install's relay endpoint (``services/webhooks/relay_events``): the
one predicate, the kinds union, the signed register ladder, the coalesced
syncs, the startup pass and the unbind. Stores and the relay are faked."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import config
from services.billing import relay_client
from services.mcp import mcp_registry
from services.webhooks import relay_events, subscription_manager, webhook_dispatcher
from storage import database as db
from storage.automation import webhook_subscription_store
from storage.identity import credential_store
from storage.mcp import mcp_store

SECRET_KEY = relay_client.EVENTS_FORWARD_SECRET_KEY
SLUG = relay_client.EVENTS_FORWARD_SECRET_SLUG

_SLACK = {"available": True, "provider_id": "slack", "workspace_id_path": "body.team_id",
          "event_catalog": [
              {"key": "message.channels", "match": {"event_type": "message"}},
              {"key": "message.im", "match": {"event_type": "message"}},
              {"key": "reaction_added"},
          ]}
_NOTION = {"available": True, "provider_id": "notion", "workspace_id_path": "body.workspace_id",
           "event_catalog": [{"key": "page.content_updated"}]}
_GITHUB = {"available": True, "provider_id": "github", "app_webhook": {"installation_id_path": "body.installation.id"},
           "event_catalog": [{"key": "issues"}, {"key": "push"}]}


def _manifest(name, webhooks, oauth_provider=""):
    return SimpleNamespace(name=name, credentials=SimpleNamespace(
        webhooks=webhooks, oauth={"provider_id": oauth_provider or webhooks["provider_id"]}))


class Env:
    def __init__(self, monkeypatch):
        self.rows: list[dict] = []
        self.manifests = {"slack-mcp": _manifest("slack-mcp", _SLACK),
                          "notion-mcp": _manifest("notion-mcp", _NOTION),
                          "github-mcp": _manifest("github-mcp", _GITHUB)}
        self.states: dict[str, bool] = {}
        self.hosted: set[str] = set()
        self.infra: dict = {}
        self.settings: dict[str, str] = {}
        self.calls: list[dict] = []
        self.unbinds: list[dict] = []
        self.answers: list = []          # popped per register: dict, or a RelayError
        self.unbind_answers: list = []
        mp = monkeypatch
        mp.setattr(config, "DASHBOARD_PUBLIC_URL", "https://inst.example")
        mp.setattr(relay_client, "is_available", lambda: True)
        mp.setattr(relay_events, "RETRY_DELAY_S", 0)
        mp.setattr(relay_events, "LATER_DELAYS_S", (0, 0))
        mp.setattr(relay_events, "HEAL_DELAY_S", 0)
        mp.setattr(relay_events, "_rotate_blocked_until", 0.0)
        mp.setattr(webhook_subscription_store, "list_subscriptions", self._list)
        mp.setattr(mcp_registry, "get_manifest", self.manifests.get)
        mp.setattr(mcp_registry, "get_all_manifests", lambda: dict(self.manifests))
        mp.setattr(mcp_store, "get_all_mcp_states", lambda: dict(self.states))
        self.hosted_of_leaver: set[str] = set()
        mp.setattr(credential_store, "hosted_app_account_mcps",
                   lambda exclude_user_sub="": set(self.hosted)
                   | (set() if exclude_user_sub == "leaver" else set(self.hosted_of_leaver)))
        mp.setattr(credential_store, "get_infra_credentials", lambda slug: dict(self.infra.get(slug) or {}))
        mp.setattr(credential_store, "set_infra_credentials",
                   lambda slug, creds: self.infra.setdefault(slug, {}).update(creds))
        mp.setattr(db, "get_platform_setting", lambda key: self.settings.get(key, ""))
        mp.setattr(db, "set_platform_setting", self.settings.__setitem__)
        mp.setattr(webhook_dispatcher, "forget_forward_secret", lambda: None)
        mp.setattr(relay_client, "events_register", self._register)
        mp.setattr(relay_client, "events_unbind", self._unbind)

    def _list(self, **kw):
        out = self.rows
        for k in ("provider_id", "delivery_mode", "mcp_name"):
            if kw.get(k) is not None:
                out = [r for r in out if r.get(k) == kw[k]]
        return list(out)

    def row(self, mcp, provider, events, status="active"):
        self.rows.append({"id": f"s{len(self.rows)}", "mcp_name": mcp, "provider_id": provider,
                          "delivery_mode": "relay", "selected_events": events, "status": status})

    def hold(self, secret="fs"):
        self.infra[SLUG] = {SECRET_KEY: secret}

    @property
    def secret(self):
        return (self.infra.get(SLUG) or {}).get(SECRET_KEY)

    async def _register(self, **kw):
        await asyncio.sleep(0)
        self.calls.append(kw)
        answer = self.answers.pop(0) if self.answers else {"ok": True, "forward_secret": None}
        if isinstance(answer, Exception):
            raise answer
        if kw.get("rotate_secret") and answer.get("forward_secret") is None:
            answer = {**answer, "forward_secret": f"minted-{len(self.calls)}"}
        return answer

    async def _unbind(self, **kw):
        self.unbinds.append(kw)
        answer = self.unbind_answers.pop(0) if self.unbind_answers else {"ok": True, "removed": 1}
        if isinstance(answer, Exception):
            raise answer
        return answer

    def pending(self):
        return json.loads(self.settings.get("relay_events_pending_unbinds") or "[]")


@pytest.fixture
def env(monkeypatch, tmp_path):
    # Token files of hosted accounts are read from here (none unless a test writes one).
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    return Env(monkeypatch)


def _hosted_file(provider, user="alice", label="a", **extra):
    d = config.SESSIONS_DIR / f"{provider}-tokens" / user
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{label}.json"
    path.write_text(json.dumps({"access_token": "x", "extra": {"via_relay": True, **extra}}))
    return path


async def _drain():
    """Let every relay-events background task finish."""
    from core import background
    for _ in range(50):
        tasks = background.running("relay-events")
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


# --- the kinds union -------------------------------------------------------------

def test_kinds_map_each_key_by_one_rule(env):
    env.row("slack-mcp", "slack", ["message.channels", "message.im"])
    env.row("slack-mcp", "slack", ["reaction_added"], status="disabled")
    assert relay_events.relay_kinds("slack") == ["message", "reaction_added"]


def test_kinds_leave_out_a_switched_off_or_removed_mcp(env):
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.row("gone-mcp", "slack", ["team_join"])
    env.states["slack-mcp"] = False
    assert relay_events.relay_kinds("slack") == []
    env.states["slack-mcp"] = True
    assert relay_events.relay_kinds("slack") == ["reaction_added"]


def test_kinds_widen_to_every_kind_for_an_empty_selection_or_a_refused_name(env):
    env.row("slack-mcp", "slack", [])
    assert relay_events.relay_kinds("slack") is None
    env.rows.clear()
    env.row("slack-mcp", "slack", ["Not A Kind"])
    assert relay_events.relay_kinds("slack") is None


def test_github_app_kinds_carry_the_lifecycle(env):
    lifecycle = ["github_app_authorization", "installation", "installation_repositories",
                 "installation_target"]
    env.hosted.add("github-mcp")
    assert relay_events.relay_kinds("github") == lifecycle
    env.row("github-mcp", "github", ["issues"])
    assert relay_events.relay_kinds("github") == sorted([*lifecycle, "issues"])


# --- the predicate ---------------------------------------------------------------

def test_the_predicate(env):
    assert relay_events.provider_gone("slack") is True
    env.row("slack-mcp", "slack", ["reaction_added"], status="failed")
    assert relay_events.provider_gone("slack") is False
    assert relay_events.provider_gone("github") is True
    env.hosted.add("github-mcp")
    assert relay_events.provider_gone("github") is False


def test_a_hosted_account_of_any_provider_keeps_it(env):
    """The relay sends its account-removed notice only to a registered
    endpoint: neither the switch-off nor the 410 while one is held."""
    assert relay_events.provider_gone("facebook") is True
    path = _hosted_file("facebook", relay_binding={"scope_id": "app", "user_key": "M1"})
    _hosted_file("google", via_relay=False)
    assert relay_events.provider_gone("facebook") is False
    assert relay_events.hosted_account_providers() == {"facebook"}
    assert relay_events.providers_in_use() == ["facebook"]
    path.unlink()
    assert relay_events.provider_gone("facebook") is True


def test_startup_registers_a_hosted_accounts_provider_with_its_kinds_or_none(env):
    env.hold()
    _hosted_file("facebook", relay_binding={"scope_id": "app", "user_key": "M1"})
    _hosted_file("slack", relay_binding={"scope_id": "T1", "user_key": "U1"})
    env.row("slack-mcp", "slack", ["reaction_added"])
    asyncio.run(relay_events.sync_all())
    calls = {c["provider_id"]: c for c in env.calls}
    assert sorted(calls) == ["facebook", "slack"]
    assert calls["facebook"]["enabled"] is True and calls["facebook"]["kinds"] == []
    assert calls["slack"]["kinds"] == ["reaction_added"]
    assert all(c["sign_with"] == "fs" for c in env.calls)


def test_the_receiver_never_answers_410_while_a_hosted_account_is_held(env):
    env.hold()
    _hosted_file("slack", relay_binding={"scope_id": "T1", "user_key": "U1"})
    status, body, _ = asyncio.run(webhook_dispatcher._gone_or_unmatched(
        "slack", {}, "fs", {"status": "no_subscriptions", "fired": 0}))
    assert status == 200


def test_a_hosted_account_whose_manifest_is_not_loaded_keeps_the_provider(env):
    env.hosted.add("github-mcp")
    env.manifests.pop("github-mcp")
    assert relay_events.provider_gone("github") is False
    # A workspace provider never comes from a GitHub App.
    assert relay_events.provider_gone("slack") is True


# --- the signed ladder -----------------------------------------------------------

def test_a_held_secret_signs_every_register_with_the_kinds(env):
    env.hold()
    env.row("slack-mcp", "slack", ["message.im"])
    asyncio.run(relay_events.sync_provider("slack"))
    assert env.calls == [{"provider_id": "slack", "events_url": "https://inst.example/v1/webhooks/relay/slack",
                          "enabled": True, "kinds": ["message"], "sign_with": "fs"}]


def test_a_gone_provider_is_switched_off_signed(env):
    env.hold()
    asyncio.run(relay_events.sync_provider("slack"))
    assert env.calls[-1]["enabled"] is False and env.calls[-1]["kinds"] is None
    assert env.calls[-1]["sign_with"] == "fs"


def test_a_fresh_install_mints_then_signs_every_provider_in_use(env):
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.row("notion-mcp", "notion", ["page.content_updated"])
    asyncio.run(relay_events.sync_provider("slack"))
    first, *rest = env.calls
    assert first["rotate_secret"] is True and "sign_with" not in first
    assert env.secret == "minted-1"
    assert [(c["provider_id"], c.get("sign_with")) for c in rest] == [
        ("slack", "minted-1"), ("notion", "minted-1")]
    assert all(not c.get("rotate_secret") for c in rest)


def test_a_switch_off_answer_carrying_a_secret_is_stored(env):
    env.answers.append({"ok": True, "forward_secret": "from-off"})
    env.hold("old")
    asyncio.run(relay_events.sync_provider("slack"))
    assert env.secret == "from-off"


def test_a_refused_signature_rotates_once_then_signs(env):
    env.hold("stale")
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.answers.append(relay_client.RelayError("bad_signature"))
    asyncio.run(relay_events.sync_provider("slack"))
    assert [bool(c.get("sign_with")) for c in env.calls] == [True, False, True]
    assert env.calls[1]["rotate_secret"] is True
    assert env.secret == "minted-2"


def test_a_second_refusal_of_a_new_secret_stops_and_blocks_rotation(env):
    """A clock off by more than five minutes on an install that never
    signed: the rotation is served, the signed register after it is
    refused, and the next sync does not rotate again."""
    env.hold("stale")
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.answers += [relay_client.RelayError("bad_signature"), {"ok": True},
                    relay_client.RelayError("bad_signature")]
    asyncio.run(relay_events.sync_provider("slack"))
    assert len(env.calls) == 3
    env.answers.append(relay_client.RelayError("bad_signature"))
    with pytest.raises(relay_client.RelayError) as exc:
        asyncio.run(relay_events.sync_provider("slack"))
    assert exc.value.code == "bad_signature" and len(env.calls) == 4


def test_a_refused_unsigned_rotation_asks_for_the_staff_reset(env):
    env.hold("stale")
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.answers += [relay_client.RelayError("bad_signature"), relay_client.RelayError("bad_signature")]
    with pytest.raises(relay_client.RelayError) as exc:
        asyncio.run(relay_events.sync_provider("slack"))
    assert exc.value.code == "events_reset_needed"
    assert "clock" not in relay_client.relay_error_message(exc.value.code)


def test_two_first_registrations_at_once_mint_once(env):
    env.row("slack-mcp", "slack", ["reaction_added"])

    async def both():
        await asyncio.gather(relay_events.sync_provider("slack"), relay_events.sync_provider("slack"))
    asyncio.run(both())
    assert sum(1 for c in env.calls if c.get("rotate_secret")) == 1


def test_a_delete_beside_a_create_ends_switched_on(env):
    """The create inserts its row under the lock before it registers, so the
    delete's switch-off, waiting for the lock, sees the new row."""
    env.hold()

    async def scenario():
        async with relay_events.registration_lock():
            delete = asyncio.create_task(relay_events.sync_provider("slack"))
            await asyncio.sleep(0)
            env.row("slack-mcp", "slack", ["reaction_added"])
            await relay_events.sync_provider_locked("slack")
        await delete
    asyncio.run(scenario())
    assert [c["enabled"] for c in env.calls] == [True, True]


# --- coalescing and the retry ----------------------------------------------------

def test_soon_coalesces_and_retries_a_busy_relay(env):
    env.hold()
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.answers.append(relay_client.RelayError(relay_client.RATE_LIMITED))

    async def scenario():
        for _ in range(5):
            relay_events.sync_provider_soon("slack")
        await _drain()
    asyncio.run(scenario())
    # The refused one, its retry, and the one more the burst asked for.
    assert 2 <= len(env.calls) <= 3
    assert env.calls[-1]["enabled"] is True


def test_a_request_while_a_run_waits_on_a_busy_relay_is_coalesced(env, monkeypatch):
    """Asked again while the run sleeps on its refusal: one more register,
    not two."""
    env.hold()
    env.row("slack-mcp", "slack", ["reaction_added"])
    monkeypatch.setattr(relay_events, "RETRY_DELAY_S", 0.05)
    env.answers.append(relay_client.RelayError(relay_client.RATE_LIMITED))

    async def scenario():
        first = relay_events.sync_provider_soon("slack")
        for _ in range(200):
            if env.calls:
                break
            await asyncio.sleep(0.005)
        assert relay_events.sync_provider_soon("slack") is first
        assert relay_events.sync_provider_soon("slack") is first
        await _drain()
    asyncio.run(scenario())
    assert len(env.calls) == 2 and env.calls[-1]["enabled"] is True


def test_a_busy_relay_at_boot_is_retried(env):
    env.hold()
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.answers.append(relay_client.RelayError(relay_client.RATE_LIMITED))

    async def scenario():
        await relay_events.sync_all()
        await _drain()
    asyncio.run(scenario())
    assert [c["provider_id"] for c in env.calls] == ["slack", "slack"]


def test_an_unreachable_relay_at_boot_is_tried_later(env):
    """An install whose first boot finds the relay down still signs (and is
    marked) a few minutes later."""
    import httpx
    env.hold()
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.answers += [httpx.ConnectError("down"), httpx.ConnectError("down")]

    async def scenario():
        await relay_events.sync_all()
        await _drain()
    asyncio.run(scenario())
    assert len(env.calls) == 3 and env.calls[-1]["sign_with"] == "fs"


def test_a_busy_relay_after_a_mint_is_retried(env):
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.row("notion-mcp", "notion", ["page.content_updated"])
    env.answers += [{"ok": True}, {"ok": True}, relay_client.RelayError(relay_client.RATE_LIMITED)]

    async def scenario():
        await relay_events.sync_provider("slack")
        await _drain()
    asyncio.run(scenario())
    assert [c["provider_id"] for c in env.calls] == ["slack", "slack", "notion", "notion"]
    assert env.calls[-1]["sign_with"] == "minted-1"


def test_a_staff_reset_heals_within_the_hour_without_a_new_rotation(env):
    """The install signed once and lost its secret: the unsigned rotation is
    refused, the admin reads the reset sentence; the hourly register, signed
    and never rotating, takes the secret the relay mints after the reset."""
    env.hold("stale")
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.answers += [relay_client.RelayError("bad_signature"), relay_client.RelayError("bad_signature"),
                    {"ok": True, "forward_secret": "after-reset"}]

    async def scenario():
        relay_events.sync_provider_soon("slack")
        await _drain()
    asyncio.run(scenario())
    signed = [bool(c.get("sign_with")) for c in env.calls]
    assert signed[:3] == [True, False, True] and env.calls[2].get("rotate_secret") is not True
    assert env.secret == "after-reset"
    assert env.calls[-1]["sign_with"] == "after-reset"       # the mint's own follow-up


def test_bulk_cleanup_syncs_once_per_provider(env, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(relay_events, "sync_provider_soon", asked.append)
    monkeypatch.setattr(webhook_subscription_store, "delete_subscription", lambda sid: True)
    rows = [{"id": f"r{i}", "delivery_mode": "relay", "provider_id": p}
            for i, p in enumerate(["slack", "slack", "notion", "slack"])]
    rows.append({"id": "v", "delivery_mode": "vendor", "provider_id": "github"})

    async def no_vendor(row, **kw):
        return True
    monkeypatch.setattr(subscription_manager, "_vendor_delete", no_vendor)
    assert asyncio.run(subscription_manager._cleanup_rows(rows)) == 5
    assert asked == ["notion", "slack"]


def test_an_mcps_uninstall_deletes_its_rows_vendor_first(env, monkeypatch):
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.rows.append({"id": "v1", "mcp_name": "slack-mcp", "provider_id": "slack", "delivery_mode": "vendor"})
    env.row("notion-mcp", "notion", ["page.content_updated"])
    order: list[str] = []

    async def vendor(row, **kw):
        order.append(f"vendor:{row['id']}")
        return True
    monkeypatch.setattr(subscription_manager, "_vendor_delete", vendor)
    monkeypatch.setattr(webhook_subscription_store, "delete_subscription",
                        lambda sid: order.append(f"row:{sid}") or True)
    monkeypatch.setattr(relay_events, "sync_provider_soon", lambda p: order.append(f"sync:{p}"))
    assert asyncio.run(subscription_manager.cleanup_mcp_subscriptions("slack-mcp")) == 2
    assert order == ["vendor:s0", "row:s0", "vendor:v1", "row:v1", "sync:slack"]


def test_a_switch_of_an_unused_providers_mcp_sends_nothing(env):
    """No relay row and no hosted account: switching an MCP off or on must
    not reach the relay (it would pin this install's host there)."""
    async def toggle(name):
        relay_events.sync_providers_of_mcp_soon(name)
        await _drain()
    asyncio.run(toggle("github-mcp"))
    asyncio.run(toggle("slack-mcp"))
    assert env.calls == []


def test_a_switch_registers_only_the_providers_in_use(env):
    env.hold()
    env.manifests["slack-mcp"] = _manifest("slack-mcp", _SLACK, oauth_provider="slack-oauth")
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.states["slack-mcp"] = False

    async def toggle():
        relay_events.sync_providers_of_mcp_soon("slack-mcp")
        await _drain()
    asyncio.run(toggle())
    assert [(c["provider_id"], c["kinds"]) for c in env.calls] == [("slack", [])]


def test_startup_registers_each_provider_in_use_once(env):
    env.hold()
    env.row("slack-mcp", "slack", ["reaction_added"])
    env.row("slack-mcp", "slack", ["message.im"])
    env.hosted.add("github-mcp")
    asyncio.run(relay_events.sync_all())
    assert sorted(c["provider_id"] for c in env.calls) == ["github", "slack"]


def test_startup_does_nothing_without_a_public_url(env, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    env.row("slack-mcp", "slack", ["reaction_added"])
    asyncio.run(relay_events.sync_all())
    assert env.calls == []


# --- the create path -------------------------------------------------------------

def _create_args():
    return {"scope": "user", "owner": "alice", "agent": None, "mcp_name": "slack-mcp",
            "provider_id": "slack", "account_label": "a", "vendor_target": "T1",
            "selected_events": ["reaction_added"], "selected_subevents": {},
            "created_by": "alice", "expires_at": None, "target_kind": ""}


def test_the_create_registers_with_its_row_already_counted(env, monkeypatch):
    env.hold()

    def create(**kw):
        row = {"id": "new", **kw, "status": "creating"}
        env.rows.append(row)
        return row
    monkeypatch.setattr(webhook_subscription_store, "create_subscription", create)
    row = asyncio.run(subscription_manager._create_relay_row(provider_id="slack", row_args=_create_args()))
    assert row["delivery_mode"] == "relay" and row["signing_secret"] == ""
    assert env.calls == [{"provider_id": "slack", "events_url": "https://inst.example/v1/webhooks/relay/slack",
                          "enabled": True, "kinds": ["reaction_added"], "sign_with": "fs"}]


def test_a_refused_create_deletes_its_row(env, monkeypatch):
    env.hold()
    deleted: list[str] = []
    monkeypatch.setattr(webhook_subscription_store, "create_subscription",
                        lambda **kw: env.rows.append({"id": "new", **kw}) or env.rows[-1])
    monkeypatch.setattr(webhook_subscription_store, "delete_subscription",
                        lambda sid: deleted.append(sid) or env.rows.clear() or True)
    env.answers.append(relay_client.RelayError("events_provider_unsupported"))
    assert asyncio.run(subscription_manager._create_relay_row(
        provider_id="slack", row_args=_create_args())) is None
    assert deleted == ["new"]
    env.answers.append(relay_client.RelayError(relay_client.RATE_LIMITED))

    async def refused():
        with pytest.raises(subscription_manager.SubscriptionError) as exc:
            await subscription_manager._create_relay_row(provider_id="slack", row_args=_create_args())
        await _drain()
        return exc.value
    err = asyncio.run(refused())
    assert err.status == 422 and "Try again in a minute" in str(err)
    assert deleted == ["new", "new"]


def test_deleting_a_relay_row_registers_the_provider_again(env, monkeypatch):
    env.hold()
    row = {"id": "s1", "delivery_mode": "relay", "provider_id": "slack"}
    monkeypatch.setattr(webhook_subscription_store, "get_subscription", lambda sid: row)
    monkeypatch.setattr(webhook_subscription_store, "delete_subscription", lambda sid: True)

    async def scenario():
        out = await subscription_manager.delete_subscription(subscription_id="s1")
        await _drain()
        return out
    assert asyncio.run(scenario()) == (True, True)
    assert env.calls[-1]["enabled"] is False and env.calls[-1]["provider_id"] == "slack"


# --- the unbind ------------------------------------------------------------------

def _slack_account(**extra):
    return {"access_token": "xoxp", "extra": {"via_relay": True, **extra}}


def _unbind(provider_id, account, *, revoked, **kw):
    """``unbind_account`` and the background sender it starts, to the end."""
    async def run():
        await relay_events.unbind_account(provider_id, account, revoked=revoked, **kw)
        await _drain()
    asyncio.run(run())


def test_the_unbind_names_the_exact_pair_from_the_exchange(env):
    env.hold()
    _unbind("slack", _slack_account(relay_binding={"scope_id": "T1", "user_key": "U1"}), revoked=True)
    assert env.unbinds == [{"provider_id": "slack", "scope_id": "T1", "user_key": "U1", "sign_with": "fs"}]
    assert env.pending() == []


def test_an_older_account_derives_its_pair(env):
    env.hold()
    _unbind("slack", _slack_account(team={"id": "T9"}, authed_user={"id": "U9"}), revoked=False)
    _unbind("notion", {"extra": {"via_relay": True, "workspace_id": "W1", "bot_id": "B1"}}, revoked=False)
    _unbind("zoom", {"extra": {"via_relay": True, "account_id": "A1", "id": "Z1"}}, revoked=False)
    assert [(u["provider_id"], u["scope_id"], u["user_key"]) for u in env.unbinds] == [
        ("slack", "T9", "U9"), ("notion", "W1", "B1"), ("zoom", "A1", "Z1")]


def test_a_facebook_disconnect_unbinds_its_relay_binding_pair(env):
    env.hold()
    _unbind("facebook", {"extra": {"via_relay": True, "relay_binding": {
        "scope_id": "app", "user_key": "M1", "bound_at": 1_900_000_000}}}, revoked=True)
    _unbind("facebook", {"extra": {"via_relay": True, "id": "M2"}}, revoked=True)
    assert [(u["provider_id"], u["scope_id"], u["user_key"]) for u in env.unbinds] == [("facebook", "app", "M1")]


def test_no_unbind_without_a_pair_or_for_a_self_managed_account(env):
    env.hold()
    _unbind("notion", {"extra": {"via_relay": True, "workspace_id": "W1"}}, revoked=False)
    _unbind("slack", {"extra": {"team_id": "T1", "user_id": "U1"}}, revoked=False)
    assert env.unbinds == [] and env.pending() == []


def test_a_pair_another_account_holds_stays(env, tmp_path, monkeypatch):
    env.hold()
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    base = tmp_path / "slack-tokens"
    (base / "bob").mkdir(parents=True)
    (base / "bob" / "b.json").write_text(json.dumps(
        {"access_token": "x", "extra": {"via_relay": True, "relay_binding": {"scope_id": "T1", "user_key": "U1"}}}))
    account = _slack_account(relay_binding={"scope_id": "T1", "user_key": "U1"})
    _unbind("slack", account, revoked=True)
    assert env.unbinds == []
    (base / "bob" / "b.json").unlink()
    _unbind("slack", account, revoked=True)
    assert len(env.unbinds) == 1


def test_github_unbinds_the_provider_only_after_a_failed_revoke_of_the_last_account(env):
    env.hold()
    account = {"extra": {"via_relay": True, "app_kind": "github_app"}}
    _unbind("github", account, revoked=True)
    env.hosted.add("github-mcp")
    _unbind("github", account, revoked=False)
    assert env.unbinds == []
    env.hosted.clear()
    _unbind("github", account, revoked=False)
    assert env.unbinds == [{"provider_id": "github", "scope_id": "", "user_key": "", "sign_with": "fs"}]


def test_a_deleted_persons_own_hosted_account_does_not_keep_github(env):
    """At a person's deletion their account rows still exist (they cascade
    later): their own hosted account must not keep the provider."""
    env.hold()
    env.hosted_of_leaver.add("github-mcp")
    account = {"extra": {"via_relay": True, "app_kind": "github_app"}}
    _unbind("github", account, revoked=False)
    assert env.unbinds == []
    _unbind("github", account, revoked=False, leaving_user_sub="leaver")
    assert env.unbinds == [{"provider_id": "github", "scope_id": "", "user_key": "", "sign_with": "fs"}]


def test_a_busy_relay_or_no_secret_keeps_the_unbind_pending(env):
    account = _slack_account(relay_binding={"scope_id": "T1", "user_key": "U1"})

    async def no_secret_then_mint():
        await relay_events.unbind_account("slack", account, revoked=True)
        await _drain()
        assert env.unbinds == [] and len(env.pending()) == 1
        env.row("slack-mcp", "slack", ["reaction_added"])
        await relay_events.sync_provider("slack")     # mints, then starts the sender
        await _drain()
    asyncio.run(no_secret_then_mint())
    assert len(env.unbinds) == 1 and env.pending() == []

    env.unbind_answers.append(relay_client.RelayError(relay_client.RATE_LIMITED))
    _unbind("slack", account, revoked=True)
    assert len(env.unbinds) == 3 and env.pending() == []


def test_an_older_relay_without_unbind_is_logged_and_dropped(env):
    env.hold()
    env.unbind_answers.append(relay_client.RelayError("provider_unknown"))
    _unbind("slack", _slack_account(relay_binding={"scope_id": "T1", "user_key": "U1"}), revoked=True)
    assert len(env.unbinds) == 1 and env.pending() == []


@pytest.mark.parametrize("code", ["events_unbind_invalid", "events_provider_unsupported", "license_invalid"])
def test_a_refusal_for_good_settles_the_unbind(env, code):
    env.hold()
    env.unbind_answers.append(relay_client.RelayError(code))
    _unbind("slack", _slack_account(relay_binding={"scope_id": "T1", "user_key": "U1"}), revoked=True)
    assert len(env.unbinds) == 1 and env.pending() == []


def test_the_cap_drops_the_oldest_and_says_so(env, caplog, monkeypatch):
    monkeypatch.setattr(relay_events, "_PENDING_UNBINDS_MAX", 3)
    with caplog.at_level("WARNING", logger="otodock.relay-events"):
        relay_events._save_pending([{"provider_id": "slack", "user_key": f"U{i}"} for i in range(5)])
    assert [i["user_key"] for i in env.pending()] == ["U2", "U3", "U4"]
    assert "2 pending unbinds over the cap of 3 dropped" in caplog.text


def _pending(env, n):
    env.settings["relay_events_pending_unbinds"] = json.dumps(
        [{"provider_id": "slack", "scope_id": "T1", "user_key": f"U{i}"} for i in range(n)])


def test_an_unreachable_relay_costs_one_call_per_pass(env, monkeypatch):
    """Three pending, the relay down: one call, the rest kept, the next pass
    a minute later, and no registration waits on it."""
    import httpx
    env.hold()
    _pending(env, 3)
    monkeypatch.setattr(relay_events, "RETRY_DELAY_S", 3600)
    env.unbind_answers.append(httpx.ConnectError("down"))

    async def scenario():
        relay_events.send_unbinds_soon()
        for _ in range(200):
            if env.unbinds:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        sender = relay_events._now().sender
        assert sender is not None and not sender.done()      # sleeping toward the next pass
        assert not relay_events.registration_lock().locked()
        sender.cancel()
    asyncio.run(scenario())
    assert len(env.unbinds) == 1 and len(env.pending()) == 3


def test_a_refused_signature_stops_the_pass_with_no_retry(env):
    """The install lost its secret: twelve pending, one call, no loop."""
    env.hold()
    _pending(env, 12)
    env.unbind_answers.append(relay_client.RelayError("bad_signature"))

    async def scenario():
        relay_events.send_unbinds_soon()
        await _drain()
        sender = relay_events._now().sender
        assert sender is None or sender.done()
    asyncio.run(scenario())
    assert len(env.unbinds) == 1 and len(env.pending()) == 12


def test_a_relay_down_costs_the_unbind_caller_nothing(env, monkeypatch):
    """The disconnect and a person's deletion await ``unbind_account``: it
    queues and returns, whatever the relay does."""
    env.hold()

    async def hang(**kw):
        await asyncio.Event().wait()
    monkeypatch.setattr(relay_client, "events_unbind", hang)

    async def scenario():
        await asyncio.wait_for(relay_events.unbind_account(
            "slack", _slack_account(relay_binding={"scope_id": "T1", "user_key": "U1"}), revoked=True), 1.0)
        assert len(env.pending()) == 1
        relay_events._now().sender.cancel()
    asyncio.run(scenario())


def test_the_boot_sends_the_unbinds_left_from_before_a_restart(env):
    env.hold()
    _pending(env, 2)

    async def scenario():
        await relay_events.sync_all()
        await _drain()
    asyncio.run(scenario())
    assert [u["user_key"] for u in env.unbinds] == ["U0", "U1"] and env.pending() == []


def test_the_gone_mark():
    import hashlib
    import hmac
    expected = "v0=" + hmac.new(b"fs", b"v0:gone:slack:1700000000", hashlib.sha256).hexdigest()
    assert relay_events.gone_mark("fs", "slack", "1700000000") == expected
