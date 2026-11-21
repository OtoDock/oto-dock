"""The mail server an admin names is reached at a checked address: on this
machine, the LAN or the internet on a self-hosted install, a public one only
on a hosted install (``OTODOCK_CLOUD``), for the test route and every send."""

import socket

import pytest

import config
from services.infra import pinned_http
from services.notifications import smtp


@pytest.fixture
def lan_mail_server(monkeypatch):
    def answers(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.25", port))]
    monkeypatch.setattr(pinned_http, "_getaddrinfo", answers)
    dialled: list = []

    def connect(sock, address):
        dialled.append(address)
        raise ConnectionRefusedError("nothing listens in the test")
    monkeypatch.setattr(socket.socket, "connect", connect)
    return dialled


def test_a_private_mail_server_is_refused_on_a_hosted_install(monkeypatch, lan_mail_server):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", True)
    ok, message = smtp.test_smtp_connection("mail.lan", 587, "", "", use_tls=False)
    assert ok is False and "may not reach" in message
    assert lan_mail_server == [], "nothing was dialled"


def test_a_lan_mail_server_is_dialled_on_a_self_hosted_install(monkeypatch, lan_mail_server):
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    ok, _message = smtp.test_smtp_connection("mail.lan", 587, "", "", use_tls=False)
    assert ok is False
    assert lan_mail_server == [("192.168.1.25", 587)]


def test_the_cloud_metadata_address_is_refused_everywhere(monkeypatch):
    def metadata(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]
    monkeypatch.setattr(pinned_http, "_getaddrinfo", metadata)
    monkeypatch.setattr(config, "OTODOCK_CLOUD", False)
    ok, message = smtp.test_smtp_connection("mail.example", 25, "", "", use_tls=False)
    assert ok is False and "may not reach" in message
