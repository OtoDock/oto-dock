"""The client address of a request (``auth.lan_check``).

One resolver for HTTP and WebSocket: the socket peer, the effective hops
(``TRUSTED_PROXY`` plus loopback on bare metal, never loopback in a
container), every X-Forwarded-For line walked right to left, the internal
listener where no forwarding header is read, and the detector that names an
edge misconfiguration without letting a request change another's answer.
``RUNNING_IN_DOCKER`` is pinned in every test.
"""

import logging

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth import lan_check, rate_limiter
from auth.password import hash_password
from storage import database as db

_PW = "correct-horse-battery-staple-77"
_EDGE = "10.200.0.1"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 0)
    lan_check.reset_state()
    rate_limiter._attempts.clear()
    yield
    lan_check.reset_state()
    rate_limiter._attempts.clear()


def _scope(peer, headers=(), server=("10.0.0.5", 8400)):
    return {
        "type": "http",
        "client": (peer, 40000) if peer is not None else None,
        "server": server,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }


def _resolve(peer, headers=(), **kw):
    return lan_check.resolve(_scope(peer, headers, **kw))


def _local_only_user(email="lan-only@t.com", role="member"):
    sub = db.create_local_user(email, "L", "L", role, hash_password(_PW))
    db.update_user_auth_fields(sub, local_only=True)
    return email


# ── the resolver (0b-to-C2 "Tests (C2)") ─────────────────────────────────


def test_two_xff_lines_resolve_to_the_edges_entry(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    r = _resolve(_EDGE, [("X-Forwarded-For", "198.51.100.9"),
                         ("X-Forwarded-For", "203.0.113.7")])
    assert r.client == "203.0.113.7" and r.case == "" and not r.shared


def test_an_ip_port_entry_resolves_to_the_ip(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    assert _resolve(_EDGE, [("X-Forwarded-For", "203.0.113.7:51234")]).client == "203.0.113.7"
    assert _resolve(_EDGE, [("X-Forwarded-For", "[2001:db8::7]:443")]).client == "2001:db8::7"
    assert _resolve(_EDGE, [("X-Forwarded-For", "2001:db8::8")]).client == "2001:db8::8"
    assert _resolve(_EDGE, [("X-Forwarded-For", "::ffff:203.0.113.9")]).client == "203.0.113.9"


def test_bare_metal_loopback_hop_walks_to_the_real_client(monkeypatch):
    # cloudflared -> nginx on the same host -> proxy, TRUSTED_PROXY naming
    # the far hop: loopback stays a hop on bare metal.
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["172.18.0.2"])
    r = _resolve("127.0.0.1", [("X-Forwarded-For", "203.0.113.7, 172.18.0.2")])
    assert r.client == "203.0.113.7"


def test_docker_loopback_with_xff_is_ignored_and_raises_no_flag(monkeypatch, caplog):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    with caplog.at_level(logging.WARNING, logger="claude-proxy"):
        r = lan_check.stamp_scope(_scope("127.0.0.1", [("X-Forwarded-For", "203.0.113.7")]))
    assert r.client == "127.0.0.1" and r.case == ""
    assert lan_check.forwarding_warnings() == []
    assert not [x for x in caplog.records if x.levelno >= logging.WARNING]


def test_the_internal_listener_never_reads_forwarding_headers(monkeypatch, caplog):
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 45123)
    with caplog.at_level(logging.WARNING, logger="claude-proxy"):
        r = lan_check.stamp_scope(_scope("127.0.0.1", [("X-Forwarded-For", "203.0.113.7")],
                                         server=("127.0.0.1", 45123)))
    assert r.client == "127.0.0.1" and r.case == "" and r.shared
    assert lan_check.forwarding_warnings() == []
    # The same request on the main listener resolves the header (loopback is
    # a hop on bare metal).
    assert _resolve("127.0.0.1", [("X-Forwarded-For", "203.0.113.7")]).client == "203.0.113.7"


def test_loopback_edge_without_xff_refuses_local_only(monkeypatch):
    email = _local_only_user()
    c = TestClient(app, client=("127.0.0.1", 40000))
    r = c.post("/auth/login/local", headers={"X-Forwarded-Proto": "https"},
               json={"email": email, "password": _PW})
    assert r.status_code == 403, r.text
    assert c.post("/auth/login/local", json={"email": email, "password": _PW}).status_code == 200


def test_the_docker_gateway_as_the_client_refuses_local_only(monkeypatch):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    monkeypatch.setattr(lan_check, "_docker_gateway", lambda: _EDGE)
    email = _local_only_user()
    c = TestClient(app, client=(_EDGE, 40000))
    assert c.post("/auth/login/local", json={"email": email, "password": _PW}).status_code == 403
    assert _resolve(_EDGE).shared


def test_a_public_peer_with_xff_raises_no_flag(caplog):
    with caplog.at_level(logging.WARNING, logger="claude-proxy"):
        r = lan_check.stamp_scope(_scope("198.51.100.20", [("X-Forwarded-For", "10.1.2.3")]))
    assert r.client == "198.51.100.20" and r.case == "" and not r.shared
    assert lan_check.forwarding_warnings() == []


# ── edge cases ────────────────────────────────────────────────────────────


def test_forwarded_and_x_real_ip_never_resolve(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    r = _resolve(_EDGE, [("Forwarded", "for=203.0.113.7"), ("X-Real-IP", "203.0.113.8")])
    assert r.client == _EDGE and r.case == "edge_without_xff"


def test_an_unparsable_entry_ends_the_walk_at_the_peer(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    assert _resolve(_EDGE, [("X-Forwarded-For", "203.0.113.7, unknown")]).client == _EDGE


def test_an_all_hop_chain_returns_its_leftmost_entry(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE, "10.200.0.2"])
    assert _resolve(_EDGE, [("X-Forwarded-For", "10.200.0.2, 127.0.0.1")]).client == "10.200.0.2"


def test_a_missing_or_non_ip_peer_is_neither_trusted_nor_local():
    r = _resolve(None, [("X-Forwarded-For", "10.1.2.3")])
    assert r.client == "" and not lan_check.is_private_ip(r.client)
    assert _resolve("testclient", [("X-Forwarded-For", "10.1.2.3")]).client == "testclient"


def test_a_loopback_trusted_proxy_entry_is_dropped_in_docker(monkeypatch, caplog):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["127.0.0.0/8", _EDGE])
    with caplog.at_level(logging.ERROR, logger="claude-proxy"):
        r = _resolve("127.0.0.1", [("X-Forwarded-For", "203.0.113.7")])
    assert r.client == "127.0.0.1"
    assert any("covers loopback" in x.getMessage() for x in caplog.records)
    # The edge entry still works.
    assert _resolve(_EDGE, [("X-Forwarded-For", "203.0.113.7")]).client == "203.0.113.7"


def test_ip_in_trusted_keeps_meaning_the_configured_list(monkeypatch):
    # render_principal.direct_peer reads it: loopback is NOT in the list.
    assert not lan_check._ip_in_trusted("127.0.0.1")
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    assert lan_check._ip_in_trusted(_EDGE)


# ── the detector ──────────────────────────────────────────────────────────


def test_an_untrusted_private_forwarder_logs_one_error_per_hour(caplog):
    with caplog.at_level(logging.ERROR, logger="claude-proxy"):
        for _ in range(3):
            lan_check.stamp_scope(_scope(_EDGE, [("X-Forwarded-For", "203.0.113.7")]))
    errors = [x.getMessage() for x in caplog.records if x.levelno == logging.ERROR]
    assert len(errors) == 1
    text = errors[0]
    assert _EDGE in text and f"TRUSTED_PROXY={_EDGE}" in text and "never a subnet" in text
    assert "/24" not in text and "127.0.0.1" not in text
    (row,) = lan_check.forwarding_warnings()
    assert row["peer"] == _EDGE and row["case"] == "untrusted_forwarder" and row["count"] == 3


def test_the_gateway_wording_carries_the_loopback_publish_condition(monkeypatch, caplog):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    monkeypatch.setattr(lan_check, "_docker_gateway", lambda: _EDGE)
    with caplog.at_level(logging.ERROR, logger="claude-proxy"):
        lan_check.stamp_scope(_scope(_EDGE, [("X-Forwarded-For", "203.0.113.7")]))
    (text,) = [x.getMessage() for x in caplog.records if x.levelno == logging.ERROR]
    assert "PROXY_BIND_IP=127.0.0.1" in text


def test_a_warning_row_marks_the_containers_gateway(monkeypatch):
    """The Security tab words the gateway case with its condition (the port
    published on 127.0.0.1 only), so the row says which case it is."""
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    monkeypatch.setattr(lan_check, "_docker_gateway", lambda: _EDGE)
    lan_check.stamp_scope(_scope(_EDGE, [("X-Forwarded-For", "203.0.113.7")]))
    lan_check.stamp_scope(_scope("192.168.1.9", [("X-Forwarded-For", "203.0.113.8")]))
    rows = {r["peer"]: r for r in lan_check.forwarding_warnings()}
    assert rows[_EDGE]["gateway"] is True
    assert rows["192.168.1.9"]["gateway"] is False


def test_no_warning_row_is_a_gateway_on_bare_metal(monkeypatch):
    monkeypatch.setattr(lan_check, "_docker_gateway", lambda: _EDGE)
    lan_check.stamp_scope(_scope(_EDGE, [("X-Forwarded-For", "203.0.113.7")]))
    (row,) = lan_check.forwarding_warnings()
    assert row["gateway"] is False


def test_an_edge_without_xff_logs_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    with caplog.at_level(logging.WARNING, logger="claude-proxy"):
        for _ in range(2):
            lan_check.stamp_scope(_scope(_EDGE, [("X-Forwarded-Proto", "https")]))
    warnings = [x.getMessage() for x in caplog.records if x.levelno == logging.WARNING]
    assert len(warnings) == 1 and "$proxy_add_x_forwarded_for" in warnings[0]


def test_stamp_scope_keeps_the_peer_and_rewrites_the_client(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    scope = _scope(_EDGE, [("X-Forwarded-For", "203.0.113.7")])
    lan_check.stamp_scope(scope)
    assert scope["otodock.peer"] == _EDGE and scope["client"] == ("203.0.113.7", 40000)
    # A second resolution from the stamped scope gives the same answer.
    assert lan_check.resolve(scope).client == "203.0.113.7"


def test_one_request_cannot_close_local_only_for_another(monkeypatch):
    email = _local_only_user()
    lan_check.stamp_scope(_scope("192.168.1.50", [("X-Forwarded-For", "203.0.113.7")]))
    c = TestClient(app, client=("192.168.1.60", 40000))
    assert c.post("/auth/login/local", json={"email": email, "password": _PW}).status_code == 200


# ── forged forwarding and a lockout across one edge, simulated ─────────


def test_untrusted_forwarding_refuses_local_only_and_logs():
    email = _local_only_user(role="admin")
    c = TestClient(app, client=(_EDGE, 40000))
    r = c.post("/auth/login/local", headers={"X-Forwarded-For": "203.0.113.99"},
               json={"email": email, "password": _PW})
    assert r.status_code == 403, r.text


def test_behind_a_trusted_edge_one_client_cannot_lock_out_another(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    db.create_local_user("victim-lc@t.com", "V", "V", "member", hash_password(_PW))
    c = TestClient(app, client=(_EDGE, 40000))
    for _ in range(11):
        c.post("/auth/login/local", headers={"X-Forwarded-For": "203.0.113.66"},
               json={"email": "nobody-lc@t.com", "password": "x" * 12})
    r = c.post("/auth/login/local", headers={"X-Forwarded-For": "198.51.100.7"},
               json={"email": "victim-lc@t.com", "password": _PW})
    assert r.status_code == 200, r.text
