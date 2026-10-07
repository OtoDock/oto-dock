"""The vendor-call URL check (F62, ``services/webhooks/vendor_http.py``):
substituted values are encoded, the URL must be https on a public address,
and an egress proxy's name resolution is left to the proxy."""

from __future__ import annotations

import asyncio

import pytest

from services.webhooks import vendor_http


def _refused(url: str) -> str:
    with pytest.raises(vendor_http.VendorURLRefused) as e:
        asyncio.run(vendor_http.check_url(url))
    return str(e.value)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


class TestTemplate:
    def test_a_repository_target_keeps_its_slash(self):
        url = vendor_http.url_from_template(
            "https://api.github.com/repos/${vendor_target}/hooks/${vendor_subscription_id}",
            {"vendor_target": "octo/hello-world", "vendor_subscription_id": 42})
        assert url == "https://api.github.com/repos/octo/hello-world/hooks/42"

    @pytest.mark.parametrize("target", ["../x", "a/../b", "a//b", "./a", "a/"])
    def test_a_dot_or_empty_segment_is_refused(self, target):
        with pytest.raises(vendor_http.VendorURLRefused):
            vendor_http.url_from_template(
                "https://api.github.com/repos/${vendor_target}/hooks", {"vendor_target": target})

    def test_other_values_cannot_leave_their_place(self):
        url = vendor_http.url_from_template(
            "https://graph.microsoft.com/v1.0/subscriptions/${vendor_subscription_id}",
            {"vendor_subscription_id": "x?y#z@evil.example/../w"})
        assert url == ("https://graph.microsoft.com/v1.0/subscriptions/"
                       "x%3Fy%23z%40evil.example%2F..%2Fw")

    def test_a_missing_value_renders_empty(self):
        assert vendor_http.url_from_template("https://h/${nope}", {}) == "https://h/"


class TestCheck:
    @pytest.mark.parametrize("url", [
        "https://127.0.0.1/x", "https://10.0.0.5/x", "https://169.254.169.254/latest",
        "https://192.168.1.2:8443/x",
    ])
    def test_private_loopback_and_metadata_hosts_are_refused(self, url):
        assert "private or internal" in _refused(url)

    def test_an_ipv6_loopback_literal_is_refused(self):
        _refused("https://[::1]/x")

    def test_plain_http_is_refused(self):
        assert "https" in _refused("http://8.8.8.8/x")

    def test_a_name_that_resolves_privately_is_refused(self, monkeypatch):
        monkeypatch.setattr("services.infra.outbound_url.socket.getaddrinfo",
                            lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
        assert "private or internal" in _refused("https://vendor.example/x")

    def test_a_public_address_passes(self, monkeypatch):
        monkeypatch.setattr("services.infra.outbound_url.socket.getaddrinfo",
                            lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
        asyncio.run(vendor_http.check_url("https://vendor.example/x"))

    def test_behind_an_egress_proxy_a_name_is_not_resolved(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")

        def _no_dns(*a, **k):
            raise AssertionError("resolved behind the proxy")
        monkeypatch.setattr("services.infra.outbound_url.socket.getaddrinfo", _no_dns)
        asyncio.run(vendor_http.check_url("https://api.github.com/x"))
        assert "private or internal" in _refused("https://10.0.0.1/x")


def test_a_vendor_call_to_a_refused_url_never_leaves(monkeypatch):
    import httpx
    from services.webhooks import subscription_manager as sm
    sent = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(sm.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))))
    with pytest.raises(sm.VendorAPIError, match="refused"):
        asyncio.run(sm._call_vendor(
            call_block={"method": "POST", "url_template": "https://10.0.0.9/${vendor_target}"},
            row={"vendor_target": "a/b", "id": "s1", "provider_id": "github"},
            access_token="t", extra_subs={}))
    assert sent == []
