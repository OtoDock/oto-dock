"""Classifier-hosted — phone turn-classifier Groq credential resolution.

The phone turn classifier reuses the Direct-LLM platform Groq subscription: a BYO
key wins (used directly against Groq), otherwise a hosted relay sub mints a
**system** token via the SAME ``subscription_pool.relay_llm_credentials`` the
execution layer uses (no duplicated mint logic). ``groq_classifier_configured``
backs the read-only admin "active" indicator and must NOT mint.

``subscription_store`` + the relay are mocked (no DB / network).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import storage.billing.subscription_store  # noqa: F401 — ensure `storage.billing.subscription_store` attr exists for monkeypatch
from services.phone import phone_config


def _groq_sub(auth_type: str = "relay", status: str = "active", **o) -> dict:
    s = {
        "id": f"groq-{auth_type}", "layer": "direct-llm", "provider": "groq",
        "auth_type": auth_type, "status": status,
    }
    s.update(o)
    return s


@pytest.fixture
def store(monkeypatch):
    m = MagicMock()
    m.get_credential_data.return_value = {}
    m.list_platform_pool.return_value = []
    monkeypatch.setattr("storage.billing.subscription_store", m)
    return m


def test_no_groq_sub_returns_empty(store):
    store.list_platform_pool.return_value = []
    assert phone_config.direct_llm_groq_credentials() == ("", "")
    assert phone_config.groq_classifier_configured() is False


def test_byo_key_wins_over_relay(store, monkeypatch):
    # Both a BYO key sub and a relay sub present → the key wins, relay untouched.
    store.list_platform_pool.return_value = [
        _groq_sub(auth_type="api_key"), _groq_sub(auth_type="relay"),
    ]
    store.get_credential_data.return_value = {"api_key": "byo-secret"}
    relay = MagicMock(return_value=("TKN", "URL"))
    monkeypatch.setattr("services.engines.subscription_pool.relay_llm_credentials", relay)

    key, base = phone_config.direct_llm_groq_credentials()
    assert (key, base) == ("byo-secret", "")   # base_url empty → classifier uses Groq directly
    relay.assert_not_called()
    assert phone_config.groq_classifier_configured() is True


def test_relay_sub_mints_via_shared_helper(store, monkeypatch):
    store.list_platform_pool.return_value = [_groq_sub(auth_type="relay")]
    relay = MagicMock(return_value=("MINTED", "https://api.otodock.io/v1/relay/groq/v1"))
    monkeypatch.setattr("services.engines.subscription_pool.relay_llm_credentials", relay)

    key, base = phone_config.direct_llm_groq_credentials()
    assert key == "MINTED"
    assert base == "https://api.otodock.io/v1/relay/groq/v1"
    relay.assert_called_once_with("direct-llm", "groq", "")   # system token (user_sub=""), the API engine's relay path


def test_relay_unavailable_falls_back_to_empty(store, monkeypatch):
    # Relay down / no credit → mint returns None → no creds (dispatcher → Smart Turn).
    store.list_platform_pool.return_value = [_groq_sub(auth_type="relay")]
    monkeypatch.setattr(
        "services.engines.subscription_pool.relay_llm_credentials", lambda layer, provider, user_sub: None,
    )
    assert phone_config.direct_llm_groq_credentials() == ("", "")
    # configured() reflects configuration, not live availability → still True.
    assert phone_config.groq_classifier_configured() is True


def test_disabled_sub_ignored(store):
    # list_platform_pool filters non-active subs server-side, so a disabled sub is
    # simply absent from the pool the helpers read.
    store.list_platform_pool.return_value = []
    assert phone_config.direct_llm_groq_credentials() == ("", "")
    assert phone_config.groq_classifier_configured() is False


def test_configured_does_not_mint(store, monkeypatch):
    # The admin "active" GET indicator must never trigger a relay round-trip.
    store.list_platform_pool.return_value = [_groq_sub(auth_type="relay")]
    relay = MagicMock(return_value=("X", "Y"))
    monkeypatch.setattr("services.engines.subscription_pool.relay_llm_credentials", relay)
    assert phone_config.groq_classifier_configured() is True
    relay.assert_not_called()
