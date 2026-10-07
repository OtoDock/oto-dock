"""The daemon refuses to start on a plain-http PROXY_URL to an address
reachable from the internet: the proxy key and call audio would cross it
unencrypted. https, this machine, private networks, a tailnet's 100.64/10
and single-label service names are fine; a name that does not resolve is
left to the connection."""

import socket

import pytest

import config
import main


def _resolver(*addresses):
    def resolve(host, port):
        if not addresses:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]
    return resolve


@pytest.mark.parametrize("url", [
    "https://otodock.example.com", "https://203.0.113.9:8400",
    "http://127.0.0.1:8400", "http://[::1]:8400", "http://192.168.1.10:8400",
    "http://10.0.0.5:8400", "http://100.101.102.103:8400", "http://[fd00::5]:8400",
    "http://otodock-proxy:8400", "",
])
def test_allowed(url):
    assert config.plaintext_proxy_refusal(url, resolve=_resolver("8.8.8.8")) is None


def test_a_public_address_over_http_is_refused():
    reason = config.plaintext_proxy_refusal("http://8.8.8.8:8400")
    assert reason and "PROXY_URL" in reason and "https" in reason


def test_a_name_is_judged_by_every_address():
    url = "http://otodock.example.com:8400"
    assert config.plaintext_proxy_refusal(url, resolve=_resolver("192.168.1.10")) is None
    reason = config.plaintext_proxy_refusal(url, resolve=_resolver("8.8.8.8", "2001:4860::8888"))
    assert reason and "plain http" in reason
    # A LAN host with a global IPv6 address beside its private one.
    assert config.plaintext_proxy_refusal(url, resolve=_resolver("192.168.1.10", "2a02:587::10")) is None
    assert config.plaintext_proxy_refusal(url, resolve=_resolver()) is None


def test_main_exits_before_the_loop_on_a_refused_url(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(config, "PROXY_URL", "http://8.8.8.8:8400")
    monkeypatch.setattr(main.asyncio, "run", ran.append)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2 and ran == []
    assert "not started" in capsys.readouterr().err
