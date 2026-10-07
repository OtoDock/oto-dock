"""Inbound hooks (APPS.md "Inbound hooks").

Load-bearing: the ``inbound`` block is validated, signed and carried, and
its handlers count as declared handlers; the route is verified-then-enqueue
and nothing else: the id shape and the app row first (a malformed or
unknown id is a 404 that counts against no bucket), then the per-address
bucket (skipped for an address every client shares) and the per-app bucket,
one 404 for an unknown hook, an unapproved manifest and an unset secret,
the body cap, the hook's failed-signature bucket, the
four schemes over the raw bytes with a payload signed here, 401 on a bad
signature, the vendor's event id as the idempotency key, a replay as a
duplicate, a full queue as 503; an inbound wake fires with basis
``inbound`` and a claim of that kind, which is no write and no press
authority; the app identity alone may notify its own people.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from api.apps import app_proxy
from api.apps import apps as apps_api
from api.apps import manifest as _mf
from app import app
from auth import rate_limiter
from auth.providers import UserContext, get_current_user
from services.apps import app_deploy, app_handlers, app_inbound, app_lint, app_supervisor, app_tokens
from storage import database as task_store
from storage import db_app_deliveries as deliveries
from storage import db_app_secrets

client = TestClient(app)

AGENT = "inbound-agent"
CANON = task_store.canonical_actions_json

SECRETS = [{"name": n, "required": True} for n in
           ("STRIPE_WEBHOOK_SECRET", "GH_SECRET", "HMAC_SECRET", "BEARER_SECRET")]
INBOUND = {
    "stripe": {"verify": "stripe", "secret": "STRIPE_WEBHOOK_SECRET", "handler": "payment"},
    "gh": {"verify": "github", "secret": "GH_SECRET", "handler": "push"},
    "generic": {"verify": "hmac_sha256", "secret": "HMAC_SECRET", "handler": "generic",
                "header": "X-Signature", "prefix": "sha256=", "id_header": "X-Event-Id"},
    "token": {"verify": "bearer", "secret": "BEARER_SECRET", "handler": "poke", "id_header": "X-Delivery"},
}
VALUES = {"STRIPE_WEBHOOK_SECRET": "whsec-alpha-beta-gamma", "GH_SECRET": "gh-delta-epsilon",
          "HMAC_SECRET": "hmac-zeta-eta-theta", "BEARER_SECRET": "bearer-iota-kappa-lambda"}


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), agent_roles=roles)


ALICE = _user()


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _as(ALICE)
    apps_api._fire_rate.clear()
    app_proxy._buckets.clear()
    rate_limiter._attempts.clear()
    monkeypatch.setattr(app_handlers, "schedule_drain", lambda app_id: None)
    monkeypatch.setattr(app_proxy, "PLATFORM_RATE", 1000.0)
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_proxy._buckets.clear()
    rate_limiter._attempts.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace" / "notes").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    from storage.pg import get_conn
    task_store.upsert_user("alice-sub", "alice-sub@test.com", "Alice", "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    return agents_root / AGENT


def _row(slug: str = "shop", *, inbound: dict | None = None, secrets: list[dict] | None = None,
         handlers: dict | None = None, actions: list[dict] | None = None, files: dict | None = None,
         username: str = "alice", owner: str | None = "alice-sub", approve: bool = True,
         values: bool = True) -> dict:
    root = f"users/{username}/workspace" if username else "workspace"
    blocks = {"inbound": CANON(inbound) if inbound else "", "secrets": CANON(secrets) if secrets else "",
              "handlers": CANON(handlers) if handlers else "", "files": CANON(files) if files else ""}
    actions_json = None
    if actions is not None:
        actions_json, err = _mf.validate_actions(actions, AGENT, shared=not username)
        assert actions_json is not None, err
    row = task_store.upsert_app(AGENT, username, owner, slug, title=slug.title(),
                                rel_path=f"{root}/apps/{slug}", kind="folder",
                                actions_json=actions_json, blocks=blocks)
    if approve:
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
    if values:
        for s in (secrets or []):
            if s["name"] in VALUES:
                db_app_secrets.set_value(row["id"], s["name"], VALUES[s["name"]], "alice-sub")
    return task_store.get_app(row["id"])


def _stripe_header(secret: str, body: bytes, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def _hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(row: dict, name: str, body: bytes, headers: dict[str, str]):
    return client.post(f"/v1/apps/{row['id']}/inbound/{name}", content=body,
                       headers={"Content-Type": "application/json", **headers})


# ── the block ───────────────────────────────────────────────────────────────


def test_the_inbound_block_is_validated_signed_and_carried(agent_tree):
    base = {"title": "Shop", "secrets": SECRETS, "handlers": {"on_trigger": ["gh-trigger"]}}
    v = lambda doc: app_deploy.validate_app_json({**base, **doc}, AGENT, False)  # noqa: E731
    m = v({"inbound": INBOUND})
    got = json.loads(m.inbound_json)
    assert got["stripe"] == {"verify": "stripe", "secret": "STRIPE_WEBHOOK_SECRET", "handler": "payment"}
    assert got["generic"]["header"] == "X-Signature" and got["generic"]["prefix"] == "sha256="
    assert got["token"]["id_header"] == "X-Delivery"
    assert m.blocks()["inbound"] == m.inbound_json
    assert v({"inbound": {}}).inbound_json == ""
    # The lint knows the hooks' handlers as routes the server must have.
    assert {"payment", "push", "generic", "poke", "gh-trigger"} <= app_lint.declared_from_manifest(m).handlers
    for bad in (
        {"inbound": [INBOUND["stripe"]]},
        {"inbound": {"Bad Name": INBOUND["stripe"]}},
        {"inbound": {"x": {**INBOUND["stripe"], "extra": 1}}},
        {"inbound": {"x": {**INBOUND["stripe"], "verify": "md5"}}},
        {"inbound": {"x": {**INBOUND["stripe"], "secret": "NOT_DECLARED"}}},
        {"inbound": {"x": {**INBOUND["stripe"], "handler": "Bad"}}},
        {"inbound": {"x": {**INBOUND["stripe"], "handler": "gh-trigger"}}},      # a trigger's handler
        {"inbound": {"x": {"verify": "hmac_sha256", "secret": "HMAC_SECRET", "handler": "h"}}},   # no header
        {"inbound": {"x": {**INBOUND["stripe"], "header": "X-Sig"}}},           # stripe knows its header
        {"inbound": {"x": {**INBOUND["stripe"], "id_header": "X-Id"}}},         # stripe carries its own id
        {"inbound": {"x": {**INBOUND["generic"], "id_header": "bad header"}}},
        {"inbound": {"x": {**INBOUND["generic"], "prefix": "p" * 33}}},
        {"inbound": {f"h{i}": {**INBOUND["stripe"], "handler": f"h{i}"} for i in range(9)}},
    ):
        with pytest.raises(app_deploy.DeployError):
            v(bad)
    # Carried and signed: the handlers count as declared, the signature moves.
    row = _row(inbound=INBOUND, secrets=SECRETS, handlers={"on_trigger": ["gh-trigger"]}, values=False)
    assert _mf.handler_names(row) == {"payment", "push", "generic", "poke", "gh-trigger"}
    assert _mf.parse_inbound(row)["gh"]["handler"] == "push"
    before = task_store.manifest_sig(row)
    row = task_store.upsert_app(AGENT, "alice", "alice-sub", "shop", blocks={"inbound": ""})
    assert task_store.manifest_sig(row) != before
    shaped = apps_api.shape_app_rows([task_store.get_app(row["id"])], ALICE)[0]
    assert shaped["inbound"] == {}


# ── the route ───────────────────────────────────────────────────────────────


def test_the_route_verifies_then_enqueues_and_nothing_else(agent_tree, monkeypatch):
    row = _row(inbound=INBOUND, secrets=SECRETS)
    body = json.dumps({"id": "evt_alpha", "type": "checkout.session.completed"}).encode()
    # One 404 for an unknown app, an unknown hook, an unapproved manifest and
    # an unset secret.
    assert client.post(f"/v1/apps/{uuid.uuid4()}/inbound/stripe", content=body).status_code == 404
    assert _post(row, "nope", body, {}).status_code == 404
    parked = _row("parked", inbound=INBOUND, secrets=SECRETS, approve=False)
    assert _post(parked, "stripe", body, {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body)}).status_code == 404
    db_app_secrets.delete_value(row["id"], "STRIPE_WEBHOOK_SECRET")
    assert _post(row, "stripe", body, {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body)}).status_code == 404
    db_app_secrets.set_value(row["id"], "STRIPE_WEBHOOK_SECRET", VALUES["STRIPE_WEBHOOK_SECRET"], "alice-sub")
    # Stripe: a signed event lands as a delivery for the handler with the
    # body as text, the header subset without the signature, and the
    # vendor's id scoped by the hook; a replay is a duplicate.
    r = _post(row, "stripe", body, {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body),
                                    "User-Agent": "Stripe/1"})
    assert r.status_code == 200, r.text
    d = deliveries.get(r.json()["delivery_id"])
    assert d["handler"] == "payment" and d["event"] == "inbound:stripe" and d["status"] == "pending"
    assert d["event_id"] == "stripe:evt_alpha"
    assert d["payload"]["body"] == body.decode() and d["payload"]["hook"] == "stripe"
    assert d["payload"]["headers"]["content-type"] == "application/json"
    assert d["payload"]["headers"]["user-agent"] == "Stripe/1"
    assert "stripe-signature" not in d["payload"]["headers"] and d["payload"]["event_id"] == "evt_alpha"
    again = _post(row, "stripe", body, {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body)})
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["delivery_id"] == d["id"]
    # A bad signature, a stale timestamp, a missing header, a wrong secret.
    r = _post(row, "stripe", body, {"Stripe-Signature": _stripe_header("wrong-secret-mu", body)})
    assert r.status_code == 401 and "signature_mismatch" in r.text
    r = _post(row, "stripe", body, {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body, t=int(time.time()) - 600)})
    assert r.status_code == 401 and "timestamp_too_old" in r.text
    assert _post(row, "stripe", body, {}).status_code == 401
    assert _post(row, "stripe", body, {"Stripe-Signature": "garbage"}).status_code == 401
    # Several v1 during a rotation: any may match.
    hdr = _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body)
    rotated = hdr.replace(",v1=", ",v1=" + "0" * 64 + ",v1=")
    assert _post(row, "stripe", json.dumps({"id": "evt_beta"}).encode(),
                 {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], json.dumps({"id": "evt_beta"}).encode()).replace(",v1=", ",v1=" + "0" * 64 + ",v1=")}).status_code == 200
    assert rotated  # the shape above
    # The body cap, before any verifying; the wrapped cap too.
    big = b"{" + b" " * (48 * 1024) + b"}"
    assert _post(row, "stripe", big, {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], big)}).status_code == 413
    # (the delivery cap is 256 KB; an inbound event over it is refused, not
    # trimmed — the vendor keeps its copy and retries)
    monkeypatch.setattr(app_inbound, "BODY_MAX_BYTES", 400 * 1024)
    quoted = b'"' * (300 * 1024)
    assert _post(row, "stripe", quoted, {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], quoted)}).status_code == 413
    monkeypatch.setattr(app_inbound, "BODY_MAX_BYTES", 48 * 1024)
    # GitHub: the signature header and the delivery id.
    gh_body = json.dumps({"action": "opened"}).encode()
    r = _post(row, "gh", gh_body, {"X-Hub-Signature-256": "sha256=" + _hex(VALUES["GH_SECRET"], gh_body),
                                   "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "deliv-gamma"})
    assert r.status_code == 200, r.text
    d = deliveries.get(r.json()["delivery_id"])
    assert d["handler"] == "push" and d["event_id"] == "gh:deliv-gamma"
    assert d["payload"]["headers"]["x-github-event"] == "pull_request"
    assert "x-hub-signature-256" not in d["payload"]["headers"]
    assert _post(row, "gh", gh_body, {"X-Hub-Signature-256": "sha256=" + _hex("nope-nu", gh_body)}).status_code == 401
    # A generic HMAC with a prefix and an id header; a bearer in constant
    # time with an id header; no id → no de-duplication (two rows).
    r = _post(row, "generic", gh_body, {"X-Signature": "sha256=" + _hex(VALUES["HMAC_SECRET"], gh_body),
                                        "X-Event-Id": "ev-xi"})
    assert r.status_code == 200 and deliveries.get(r.json()["delivery_id"])["event_id"] == "generic:ev-xi"
    assert _post(row, "generic", gh_body, {"X-Signature": "sha256=" + _hex("wrong-rho", gh_body)}).status_code == 401
    assert _post(row, "generic", gh_body, {}).status_code == 401
    r1 = _post(row, "token", gh_body, {"Authorization": f"Bearer {VALUES['BEARER_SECRET']}"})
    r2 = _post(row, "token", gh_body, {"Authorization": f"Bearer {VALUES['BEARER_SECRET']}"})
    assert r1.status_code == 200 and r2.status_code == 200 and r1.json()["delivery_id"] != r2.json()["delivery_id"]
    assert _post(row, "token", gh_body, {"Authorization": "Bearer wrong-omicron"}).status_code == 401
    assert _post(row, "token", gh_body, {}).status_code == 401
    r = _post(row, "token", gh_body, {"Authorization": f"Bearer {VALUES['BEARER_SECRET']}", "X-Delivery": "d-pi"})
    assert deliveries.get(r.json()["delivery_id"])["event_id"] == "token:d-pi"
    # A full queue is a 503 the vendor retries after — and the retry, same
    # event id, is taken, not answered as a duplicate of the refusal.
    monkeypatch.setattr(deliveries, "PENDING_CAP", 0)
    retried = {"Authorization": f"Bearer {VALUES['BEARER_SECRET']}", "X-Delivery": "d-sigma"}
    r = _post(row, "token", gh_body, retried)
    assert r.status_code == 503 and r.headers.get("retry-after") == "60" and "queue full" in r.text
    monkeypatch.setattr(deliveries, "PENDING_CAP", 1000)
    r = _post(row, "token", gh_body, retried)
    assert r.status_code == 200 and not r.json().get("duplicate"), r.text
    assert deliveries.get(r.json()["delivery_id"])["status"] == "pending"
    # The buckets: per client address once the app is known (a probe of hook
    # names on a real app counts; an unknown app never does), per app after.
    rate_limiter._attempts.clear()
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "app_inbound_ip",
                        {"max": 2, "window": 60, "base_block": 60, "max_block": 60})
    for _ in range(5):
        assert client.post(f"/v1/apps/{uuid.uuid4()}/inbound/x", content=b"{}").status_code == 404
    assert _post(row, "x", b"{}", {}).status_code == 404
    assert _post(row, "y", b"{}", {}).status_code == 404
    r = _post(row, "z", b"{}", {})
    assert r.status_code == 429 and r.headers.get("retry-after")
    rate_limiter._attempts.clear()
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "app_inbound_ip",
                        {"max": 1000, "window": 60, "base_block": 60, "max_block": 60})
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "app_inbound_app",
                        {"max": 1, "window": 60, "base_block": 60, "max_block": 60})
    assert _post(row, "token", gh_body, {"Authorization": f"Bearer {VALUES['BEARER_SECRET']}"}).status_code == 200
    assert _post(row, "token", gh_body, {"Authorization": f"Bearer {VALUES['BEARER_SECRET']}"}).status_code == 429


def test_garbage_ids_never_arm_the_per_address_bucket(agent_tree, monkeypatch):
    """A malformed or unknown app id is
    refused before any bucket counts it, and an address every client shares
    (no TRUSTED_PROXY behind an edge) never arms the per-address bucket, so a
    burst of garbage never refuses a vendor's signed event."""
    from fastapi.testclient import TestClient as _TC
    from auth import lan_check
    row = _row(inbound=INBOUND, secrets=SECRETS)
    body = json.dumps({"id": "evt_after_burst", "type": "checkout.session.completed"}).encode()
    signed = {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body)}
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "app_inbound_ip",
                        {"max": 3, "window": 60, "base_block": 60, "max_block": 600})
    # One distinct client: its garbage costs a lookup and counts nowhere.
    for i in range(10):
        target = "zzz" if i % 2 else str(uuid.uuid4())
        assert client.post(f"/v1/apps/{target}/inbound/x", content=b"x").status_code == 404
    assert _post(row, "stripe", body, signed).status_code == 200
    # The edge's address, shared by every client (a trusted proxy that sends
    # no X-Forwarded-For).
    rate_limiter._attempts.clear()
    lan_check.reset_state()
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["10.200.0.1"])
    edge = _TC(app, client=("10.200.0.1", 40000))
    for i in range(10):
        r = edge.post(f"/v1/apps/{uuid.uuid4()}/inbound/x", content=b"x",
                      headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 404
        assert edge.post(f"/v1/apps/{row['id']}/inbound/nope-{i}", content=b"x",
                         headers={"X-Forwarded-Proto": "https"}).status_code == 404
    body = json.dumps({"id": "evt_vendor", "type": "checkout.session.completed"}).encode()
    r = edge.post(f"/v1/apps/{row['id']}/inbound/stripe", content=body,
                  headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https",
                           "Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body)})
    assert r.status_code == 200, r.text
    lan_check.reset_state()


def test_an_untrusted_forwarder_is_one_client_for_the_per_address_bucket(agent_tree, monkeypatch):
    """f8: a private peer that is not a trusted proxy counts under its own
    address whatever forwarding header it sends."""
    from fastapi.testclient import TestClient as _TC
    from auth import lan_check
    row = _row(inbound=INBOUND, secrets=SECRETS)
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "app_inbound_ip",
                        {"max": 3, "window": 60, "base_block": 60, "max_block": 600})
    lan_check.reset_state()
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    edge = _TC(app, client=("10.200.0.1", 40000))
    codes = [edge.post(f"/v1/apps/{row['id']}/inbound/nope-{i}", content=b"x",
                       headers={"X-Real-IP": f"203.0.113.{i}"}).status_code for i in range(4)]
    assert codes == [404, 404, 404, 429]
    assert edge.post(f"/v1/apps/{row['id']}/inbound/nope", content=b"x").status_code == 429
    keys = {k[1] for k in rate_limiter._attempts if k[0] == "app_inbound_ip"}
    assert keys == {"ip:10.200.0.1"}
    lan_check.reset_state()


def test_wrong_signatures_meet_the_hooks_failure_bucket(agent_tree, monkeypatch):
    """c7: a signature that fails against the secret counts against the hook,
    and against the client too for a distinct client, checked right before
    the next verification: a bearer hook on an address every client shares
    faces more than the per-app event bucket. A delivery that verifies, a
    refusal that tested no secret, another hook, another app and another
    client's own key never count."""
    from fastapi.testclient import TestClient as _TC
    from auth import lan_check
    row = _row(inbound=INBOUND, secrets=SECRETS)
    other = _row("other", inbound=INBOUND, secrets=SECRETS)
    monkeypatch.setitem(config.RATE_LIMIT_RULES, "app_inbound_fail",
                        {"max": 3, "window": 60, "base_block": 60, "max_block": 600})
    lan_check.reset_state()
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["10.200.0.1"])
    # A trusted proxy that sends no X-Forwarded-For: every client shares it.
    edge = _TC(app, client=("10.200.0.1", 40000))
    edge_headers = {"X-Forwarded-Proto": "https"}

    def post(via, app_row, name, headers, extra=None):
        return via.post(f"/v1/apps/{app_row['id']}/inbound/{name}", content=b"{}",
                        headers={"Content-Type": "application/json", **(extra or {}), **headers})

    good = {"Authorization": f"Bearer {VALUES['BEARER_SECRET']}"}
    signed = {"X-Signature": "sha256=" + _hex(VALUES["HMAC_SECRET"], b"{}")}
    for _ in range(5):
        assert post(edge, row, "token", good, edge_headers).status_code == 200
    for _ in range(5):  # missing_header: no secret tested
        assert post(edge, row, "token", {}, edge_headers).status_code == 401
    for i in range(3):
        r = post(edge, row, "token", {"Authorization": f"Bearer wrong-{i}"}, edge_headers)
        assert r.status_code == 401 and "signature_mismatch" in r.text
    r = post(edge, row, "token", {"Authorization": "Bearer wrong-3"}, edge_headers)
    assert r.status_code == 429 and r.headers.get("retry-after")
    # Refused before the verification: the right secret too, on that hook.
    assert post(edge, row, "token", good, edge_headers).status_code == 429
    # Another hook of the app, and another app, are not locked.
    assert post(edge, row, "generic", signed, edge_headers).status_code == 200
    assert post(edge, other, "token", good, edge_headers).status_code == 200
    assert ("app_inbound_fail", f"{row['id']}:token") in rate_limiter._attempts
    # A distinct client counts against its own key: it locks only itself.
    a = _TC(app, client=("198.51.100.7", 40000))
    b = _TC(app, client=("198.51.100.8", 40000))
    for i in range(3):
        assert post(a, other, "token", {"Authorization": f"Bearer wrong-{i}"}).status_code == 401
    assert post(a, other, "token", good).status_code == 429
    assert post(b, other, "token", good).status_code == 200
    assert post(edge, other, "token", good, edge_headers).status_code == 200
    assert ("app_inbound_fail", f"{other['id']}:token:ip:198.51.100.7") in rate_limiter._attempts
    lan_check.reset_state()


def test_a_dormant_personal_app_takes_no_inbound_event(agent_tree):
    """Once a personal app's owner lost the agent, its hooks
    answer the route's one 404 (the vendor retries; a re-attach within its
    window still delivers)."""
    from storage.pg import get_conn
    row = _row(inbound=INBOUND, secrets=SECRETS)
    body = json.dumps({"id": "evt_dormant", "type": "checkout.session.completed"}).encode()
    signed = {"Stripe-Signature": _stripe_header(VALUES["STRIPE_WEBHOOK_SECRET"], body)}
    with get_conn() as conn:
        conn.execute("DELETE FROM user_agents WHERE sub=%s AND agent=%s", ("alice-sub", AGENT))
        conn.commit()
    assert _post(row, "stripe", body, signed).status_code == 404
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    assert _post(row, "stripe", body, signed).status_code == 200


# ── the wake ────────────────────────────────────────────────────────────────


def test_the_signatures_are_checked_over_the_raw_bytes_and_odd_headers_are_refused():
    """The HMAC schemes sign the bytes as they arrived: a body that carries
    the text ``{timestamp}`` or is not UTF-8 verifies under its own
    signature, and one altered to fit another body's signature does not. A
    header the sender shaped oddly is a refusal, never a server error."""
    secret = "gh-secret-tau"
    body = b'{"text": "{timestamp} is only a word"}'
    gh = {"verify": "github"}
    assert app_inbound.verify(gh, body, {"X-Hub-Signature-256": "sha256=" + _hex(secret, body)}, secret).ok
    stripped = body.replace(b"{timestamp}", b"")
    forged = app_inbound.verify(gh, body, {"X-Hub-Signature-256": "sha256=" + _hex(secret, stripped)}, secret)
    assert not forged.ok and forged.reason == "signature_mismatch"
    latin = b"caf\xe9 \xff"
    generic = {"verify": "hmac_sha256", "header": "X-Signature", "prefix": "sha256="}
    assert app_inbound.verify(generic, latin, {"X-Signature": "sha256=" + _hex(secret, latin)}, secret).ok
    for hdr in ({"X-Hub-Signature-256": "sha256=\u00e9\u00e9"}, {"X-Hub-Signature-256": "sha256="}):
        v = app_inbound.verify(gh, body, hdr, secret)
        assert not v.ok and v.reason in ("signature_mismatch", "missing_header"), v
    stripe = {"verify": "stripe"}
    for header in ("t=" + "9" * 5000 + ",v1=ab", "t=\u00b2,v1=ab", f"t={int(time.time())},v1=\u00e9"):
        v = app_inbound.verify(stripe, body, {"Stripe-Signature": header}, secret)
        assert not v.ok and v.reason in ("malformed_header", "signature_mismatch"), v


def test_an_inbound_wake_is_server_only_and_the_app_tells_its_people_itself(agent_tree, monkeypatch):
    row = _row(inbound={"stripe": INBOUND["stripe"]}, secrets=SECRETS[:1],
               actions=[{"id": "w", "label": "Write", "type": "platform", "method": "files.write"},
                        {"id": "n", "label": "Notify", "type": "platform", "method": "notifications.create"},
                        {"id": "s", "label": "Say", "type": "send_prompt", "prompt": "hi"}],
               files={"read": [], "write": ["users/{owner}/workspace/notes"]})
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row, release_dir=agent_tree,
                                   data_dir=agent_tree, host_port=1, state="up", entry="server/index.ts")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    d = deliveries.enqueue(row["id"], "payment", "inbound:stripe", {"hook": "stripe", "body": "{}"})
    d = deliveries.claim_next(row["id"])
    assert app_handlers.basis_for(d) == "inbound"
    claim = app_handlers.claim_for(d, row["id"])
    assert app_tokens.verify(claim, row["id"], app_tokens.PURPOSE_CALLER)["kind"] == "inbound"
    hdr = {"Authorization": f"Bearer {inst.token}"}
    forwarded = {**hdr, "X-OtoDock-Viewer": claim}
    # The fire says the basis; the claim opens no write and no button.
    seen: dict = {}

    class _Up:
        status_code = 200

        async def aiter_bytes(self):
            yield b"ok"

        async def aclose(self):
            return None

    async def fake_forward(inst_, method, path, headers, body, *, timeout=None):
        seen.update({"path": path, "headers": dict(headers)})
        return _Up()

    async def fake_ensure_up(r, name="live"):
        return inst

    with patch("api.apps.app_proxy.forward", fake_forward), \
            patch.object(app_supervisor, "ensure_up", fake_ensure_up):
        asyncio.run(app_handlers._fire(d))
    assert seen["path"] == "/_handler/payment" and seen["headers"]["X-OtoDock-Basis"] == "inbound"
    assert deliveries.get(d["id"])["status"] == "done"
    d = deliveries.enqueue(row["id"], "payment", "inbound:stripe", {"hook": "stripe", "body": "{}"})
    d = deliveries.claim_next(row["id"])
    claim = app_handlers.claim_for(d, row["id"])
    forwarded = {**hdr, "X-OtoDock-Viewer": claim}
    args = {"args": {"path": "users/alice/workspace/notes/a.md", "content": "hi"}}
    r = client.post(f"/v1/apps/{row['id']}/platform/files.write", json=args, headers=forwarded)
    assert r.status_code == 403 and "inbound" in r.text
    r = client.post(f"/v1/apps/{row['id']}/platform/notifications.create",
                    json={"args": {"title": "x"}}, headers=forwarded)
    assert r.status_code == 403 and "inbound" in r.text
    _as(None)   # the app's own server pressing, no person behind the request
    r = client.post(f"/v1/apps/{row['id']}/actions/s", json={"args": {}}, headers=forwarded)
    assert r.status_code == 403 and "inbound" in r.text
    _as(ALICE)
    # A read on that basis still answers (the app identity).
    r = client.post(f"/v1/apps/{row['id']}/platform/viewer.me", json={}, headers=forwarded)
    assert r.status_code == 404   # not declared — the gate reached the catalog
    # The app identity alone tells its owner (a personal app), thirty an
    # hour, never a named user; a forwarded external claim never.
    sent: list = []

    async def fake_fire(title, body, **kw):
        sent.append((title, kw))
        return [{"id": "x"}]

    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fake_fire)
    r = client.post(f"/v1/apps/{row['id']}/platform/notifications.create",
                    json={"args": {"title": "A booking landed", "body": "Tue 10:00"}}, headers=hdr)
    assert r.status_code == 200 and r.json()["result"] == {"delivered": 1, "to": "owner"}, r.text
    assert sent[0][1]["scope"] == "user" and sent[0][1]["target"] == "alice-sub"
    assert sent[0][1]["source"] == "app" and sent[0][1]["source_id"] == row["id"]
    # A write on the app identity alone stays refused.
    assert client.post(f"/v1/apps/{row['id']}/platform/files.write", json=args, headers=hdr).status_code == 403
    # A shared app tells its members.
    shared = _row("team", username="", owner=None, secrets=None,
                  actions=[{"id": "n", "label": "Notify", "type": "platform", "method": "notifications.create"}])
    sinst = app_supervisor.Instance(row_id=shared["id"], name="live", row=shared, release_dir=agent_tree,
                                    data_dir=agent_tree, host_port=1, state="up", entry="server/index.ts")
    sinst.token = app_tokens.mint(shared["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{shared['id']}"}, 3600)
    app_supervisor._instances[(shared["id"], "live")] = sinst
    r = client.post(f"/v1/apps/{shared['id']}/platform/notifications.create",
                    json={"args": {"title": "Paid"}}, headers={"Authorization": f"Bearer {sinst.token}"})
    assert r.status_code == 200 and r.json()["result"]["to"] == "members", r.text
    assert sent[-1][1]["scope"] == "agent" and sent[-1][1]["target"] == AGENT
