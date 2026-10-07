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
from starlette.requests import Request

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
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
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


def test_an_untrusted_forwarder_stays_one_distinct_client():
    """f8: forwarding headers from a private peer that is not a hop name the
    case (the detector, the local-only refusal) and change nothing about
    the per-address buckets: the peer is one client whatever it sends."""
    plain = _resolve(_EDGE)
    assert plain.case == "" and not plain.shared
    for header in (("X-Real-IP", "1.2.3.4"), ("X-Forwarded-For", "203.0.113.7"),
                   ("X-Forwarded-Proto", "https"), ("Forwarded", "for=203.0.113.7")):
        r = _resolve(_EDGE, [header])
        assert r.case == "untrusted_forwarder" and r.client == _EDGE and not r.shared
        assert r.bucket_key == _EDGE


def test_the_containers_gateway_is_shared_with_or_without_headers(monkeypatch):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    monkeypatch.setattr(lan_check, "_docker_gateway", lambda: _EDGE)
    assert _resolve(_EDGE).shared
    r = _resolve(_EDGE, [("X-Forwarded-For", "203.0.113.7")])
    assert r.case == "untrusted_forwarder" and r.shared
    # Another private peer in the container is one client.
    assert not _resolve("10.200.0.7", [("X-Forwarded-For", "203.0.113.7")]).shared


def test_the_bucket_key_of_a_resolution_is_the_auth_bucket_key(monkeypatch):
    """The receive routes key their buckets on ``ClientAddress.bucket_key``
    from the one resolution they also read ``shared`` from."""
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 45123)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    for scope in (_scope("127.0.0.1", server=("127.0.0.1", 45123)), _scope("127.0.0.1"),
                  _scope(_EDGE, [("X-Forwarded-For", "203.0.113.7")]),
                  _scope("192.168.1.9", [("X-Real-IP", "1.2.3.4")])):
        assert lan_check.resolve(scope).bucket_key == lan_check.auth_bucket_key(Request(scope))
    assert lan_check.resolve(_scope("127.0.0.1", server=("127.0.0.1", 45123))).bucket_key \
        == lan_check.INTERNAL_LISTENER_KEY


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


def test_the_boot_check_is_a_row_while_it_holds(monkeypatch):
    """A container with an https public URL and no TRUSTED_PROXY: the row
    names the gateway, comes after the detector's rows, and goes once the
    value is set (read live)."""
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://otodock.example.com")
    monkeypatch.setattr(lan_check, "_docker_gateway", lambda: _EDGE)
    lan_check.stamp_scope(_scope("192.168.1.9", [("X-Forwarded-For", "203.0.113.8")]))
    detected, boot = lan_check.forwarding_warnings()
    assert detected["case"] == "untrusted_forwarder"
    assert boot["case"] == "no_trusted_proxy" and boot["peer"] == _EDGE and boot["gateway"]
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    assert [r["case"] for r in lan_check.forwarding_warnings()] == ["untrusted_forwarder"]
    # A loopback entry is dropped in a container: no hop is trusted.
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["127.0.0.1"])
    assert lan_check.forwarding_warnings()[-1]["case"] == "no_trusted_proxy"


@pytest.mark.parametrize("docker,url", [
    (True, "http://192.168.1.10:8400"), (False, "https://otodock.example.com"), (True, "")])
def test_no_boot_row_without_an_https_container(monkeypatch, docker, url):
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", docker)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", url)
    assert lan_check.forwarding_warnings() == []


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


def _schemed(peer, proto, *, kind="http", **kw):
    scope = _scope(peer, [("X-Forwarded-Proto", proto)] if proto is not None else [], **kw)
    scope["type"] = kind
    scope["scheme"] = "ws" if kind == "websocket" else "http"
    return scope


def test_a_hop_sets_the_scheme_from_its_first_forwarded_proto(monkeypatch):
    """f10: behind a TLS edge on loopback (bare metal) or a TRUSTED_PROXY
    hop, the request's scheme is the one the edge names."""
    for proto, kind, want in (("https", "http", "https"), ("https", "websocket", "wss"),
                              ("http", "http", "http"), ("HTTPS ", "http", "https"),
                              ("https, http", "http", "https"), ("wss", "websocket", "wss"),
                              ("wss", "http", "https"), ("gopher", "http", "http")):
        scope = _schemed("127.0.0.1", proto, kind=kind)
        lan_check.stamp_scope(scope)
        assert scope["scheme"] == want, (proto, kind)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    scope = _schemed(_EDGE, "https")
    scope["headers"].append((b"x-forwarded-for", b"203.0.113.7"))
    lan_check.stamp_scope(scope)
    assert scope["scheme"] == "https" and scope["client"][0] == "203.0.113.7"
    # The request-level read gives the same answer without the shim.
    assert lan_check.trusted_forwarded_proto(_schemed(_EDGE, "https")) == "https"


def test_no_other_peer_sets_the_scheme(monkeypatch):
    for peer in (_EDGE, "198.51.100.20", "testclient"):
        scope = _schemed(peer, "https")
        lan_check.stamp_scope(scope)
        assert scope["scheme"] == "http", peer
        assert lan_check.trusted_forwarded_proto(_schemed(peer, "https")) == ""
    # The internal listener reads no forwarding header.
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 45123)
    scope = _schemed("127.0.0.1", "https", kind="websocket", server=("127.0.0.1", 45123))
    lan_check.stamp_scope(scope)
    assert scope["scheme"] == "ws"
    # Loopback in a container is not a hop.
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    scope = _schemed("127.0.0.1", "https")
    lan_check.stamp_scope(scope)
    assert scope["scheme"] == "http"


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


# ── the internal listener is not the local network ─────────────────────
# Sandboxes, app servers, scripts and the satellite tunnel reach the proxy
# on the internal listener from 127.0.0.1 (AUTH.md "Bucket key",
# SECURITY-DECISIONS.md decision 15).

_INTERNAL = 45123


def _internal_client():
    return TestClient(app, base_url=f"http://127.0.0.1:{_INTERNAL}", client=("127.0.0.1", 40000))


def _loopback_client():
    return TestClient(app, base_url="http://127.0.0.1:8400", client=("127.0.0.1", 40000))


def _key(scope):
    return lan_check.auth_bucket_key(Request(scope))


def _login_cap():
    return config.RATE_LIMIT_RULES["login"]["max"]


def test_the_internal_listener_is_marked_and_keyed_on_its_literal(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INTERNAL)
    r = _resolve("127.0.0.1", server=("127.0.0.1", _INTERNAL))
    assert r.internal and r.shared and r.client == "127.0.0.1"
    assert not _resolve("127.0.0.1").internal
    assert _key(_scope("127.0.0.1", server=("127.0.0.1", _INTERNAL))) == "internal-listener"
    assert _key(_scope("127.0.0.1")) == "127.0.0.1"
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [_EDGE])
    assert _key(_scope(_EDGE, [("X-Forwarded-For", "203.0.113.7")])) == "203.0.113.7"
    # In a container the internal listener is the same.
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", True)
    assert _resolve("127.0.0.1", server=("127.0.0.1", _INTERNAL)).internal


def test_without_an_internal_listener_nothing_is_internal(monkeypatch):
    # Port 0 means no internal listener (tests, scripts): a scope whose own
    # server port is 0 must not match it, nor a scope with no server.
    assert config.INTERNAL_LISTENER_PORT == 0
    assert not _resolve("127.0.0.1", server=("127.0.0.1", 0)).internal
    assert not lan_check.resolve({"type": "http", "client": ("127.0.0.1", 1), "server": None,
                                  "headers": []}).internal
    assert _key(_scope("127.0.0.1", server=("127.0.0.1", 0))) == "127.0.0.1"


def test_a_local_only_login_on_the_internal_listener_is_refused(monkeypatch, caplog):
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INTERNAL)
    email = _local_only_user()
    with caplog.at_level(logging.WARNING, logger="claude-proxy"):
        r = _internal_client().post("/auth/login/local", json={"email": email, "password": _PW})
    assert r.status_code == 403, r.text
    assert any("internal listener" in x.getMessage() for x in caplog.records)
    # The same peer on the main listener is a person on the host's loopback.
    assert _loopback_client().post(
        "/auth/login/local", json={"email": email, "password": _PW}).status_code == 200


def test_any_other_account_signs_in_there_and_its_bucket_is_its_own(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INTERNAL)
    db.create_local_user("inner-lc@t.com", "I", "I", "member", hash_password(_PW))
    inner = _internal_client()
    inner.post("/auth/login/local", json={"email": "nobody-lc@t.com", "password": "x" * 12})
    assert rate_limiter._attempts[("login", "internal-listener")]["count"] == 1
    assert ("login", "127.0.0.1") not in rate_limiter._attempts
    r = inner.post("/auth/login/local", json={"email": "inner-lc@t.com", "password": _PW})
    assert r.status_code == 200, r.text
    # The full login gave back its own attempt in the bucket it was counted
    # in; the earlier failure stays counted there, and no other bucket moved.
    assert rate_limiter._attempts[("login", "internal-listener")]["count"] == 1
    assert ("login", "127.0.0.1") not in rate_limiter._attempts


def test_the_2fa_step_on_the_internal_listener_refuses_local_only(monkeypatch):
    from auth.totp import create_2fa_session_token
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INTERNAL)
    email = _local_only_user()
    sub = db.get_user_by_email(email)["sub"]
    token = create_2fa_session_token(sub)
    r = _internal_client().post("/auth/login/2fa", json={"totp_session_token": token, "code": "123456"})
    assert r.status_code == 403, r.text
    assert ("2fa", "internal-listener") in rate_limiter._attempts
    # On the host's loopback it passes the gate (and finds no TOTP set up).
    r = _loopback_client().post("/auth/login/2fa", json={"totp_session_token": token, "code": "123456"})
    assert r.status_code == 400, r.text


def test_a_flood_on_the_internal_listener_never_locks_the_hosts_loopback(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INTERNAL)
    db.create_local_user("host-lc@t.com", "H", "H", "member", hash_password(_PW))
    inner = _internal_client()
    codes = [inner.post("/auth/login/local", json={"email": "nobody-lc@t.com", "password": "x" * 12}).status_code
             for _ in range(_login_cap() + 1)]
    assert codes[-1] == 429 and set(codes[:-1]) == {401}, codes
    outer = _loopback_client()
    outer.post("/auth/login/local", json={"email": "nobody-lc@t.com", "password": "x" * 12})
    assert rate_limiter._attempts[("login", "127.0.0.1")]["count"] == 1
    assert outer.post(
        "/auth/login/local", json={"email": "host-lc@t.com", "password": _PW}).status_code == 200
    assert rate_limiter._attempts[("login", "internal-listener")]["blocked_until"] > 0


def test_a_flood_on_the_hosts_loopback_never_locks_the_internal_listener(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INTERNAL)
    db.create_local_user("inner2-lc@t.com", "I", "I", "member", hash_password(_PW))
    outer = _loopback_client()
    codes = [outer.post("/auth/login/local", json={"email": "nobody-lc@t.com", "password": "x" * 12}).status_code
             for _ in range(_login_cap() + 1)]
    assert codes[-1] == 429, codes
    assert _internal_client().post(
        "/auth/login/local", json={"email": "inner2-lc@t.com", "password": _PW}).status_code == 200


def test_every_address_bucket_on_the_internal_listener_uses_the_literal(monkeypatch):
    from fastapi import HTTPException

    from api.events import triggers
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", _INTERNAL)
    inner = _internal_client()
    inner.post("/auth/forgot-password", json={"email": "someone-lc@t.com"})
    inner.post("/auth/reset-password", json={"token": "not-a-token", "new_password": "x" * 16})
    inner.post("/auth/accept-invite", json={"token": "not-a-token", "new_password": "x" * 16})
    with pytest.raises(HTTPException):
        triggers._webhook_auth_failed(Request(_scope("127.0.0.1", server=("127.0.0.1", _INTERNAL))))
    keys = set(rate_limiter._attempts)
    for bucket in ("forgot", "reset", "invite"):
        assert (bucket, "internal-listener") in keys, (bucket, keys)
    assert ("webhook_auth", "ip:internal-listener") in keys
    assert not [k for k in keys if k[1] in ("127.0.0.1", "ip:127.0.0.1")], keys


async def _echo_bucket_key(scope, receive, send):
    """A bare ASGI app answering with the auth bucket key of the request."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    body = lan_check.auth_bucket_key(Request(scope)).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})


def test_the_two_real_listeners_key_their_buckets_apart(monkeypatch):
    import threading
    import time
    import urllib.request

    from app import _build_server
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    monkeypatch.setattr(config, "PORT", 0)
    server, sockets = _build_server(_echo_bucket_key)
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started

        def key_seen(port):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                return resp.read().decode()

        assert key_seen(sockets[0].getsockname()[1]) == "127.0.0.1"
        assert key_seen(sockets[1].getsockname()[1]) == "internal-listener"
    finally:
        server.should_exit = True
        thread.join(10)
        started = server.started
        for s in sockets:
            s.close()
    assert started and not thread.is_alive()
