"""Refusals a person or an agent reads are plain sentences: the exception's
own text (a library's words, a server path, a vendor's body) goes to the log,
never into the answer."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auth.providers import UserContext

ADMIN = UserContext(sub="user-admin", email="admin@test.com", name="Admin",
                    role="admin", agents=[], agent_roles={})


def _refused(coro) -> tuple[int, str]:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value.status_code, exc.value.detail


def test_model_discovery_names_the_provider_not_the_sdk_error(monkeypatch, caplog):
    from api.admin import execution_layers as el
    from core.layers import providers

    class Adapter:
        async def list_available_models(self, **kw):
            raise RuntimeError("Error code: 401 - {'error': {'message': 'Incorrect API key sk-abc'}}")

    monkeypatch.setattr(el, "_valid_layers", lambda: {"direct-llm"})
    monkeypatch.setattr(el.subscription_store, "get_subscription",
                        lambda sid: {"layer": "direct-llm", "provider": "openai"})
    monkeypatch.setattr(el.subscription_store, "get_credential_data", lambda sid: {"api_key": "k"})
    monkeypatch.setattr(providers, "get_adapter", lambda name: Adapter())
    with caplog.at_level("WARNING"):
        status, detail = _refused(el.admin_discover_models(
            "direct-llm", el.DiscoverModelsRequest(subscription_id="1"), user=ADMIN))
    assert (status, detail) == (
        502, "Could not list the models of openai. Check its key or address, then try again.")
    assert "Incorrect API key" in caplog.text


@pytest.mark.parametrize("module", ["api.tasks.tasks", "api.notifications.notifications"])
def test_an_unknown_time_zone_is_named_plainly(module):
    import importlib
    validate = importlib.import_module(module)._validate_user_tz
    with pytest.raises(HTTPException) as exc:
        validate("Mars/Olympus")
    assert (exc.value.status_code, exc.value.detail) == (
        400, "That time zone is not known. Use a name like Europe/Athens.")
    assert validate("Europe/Athens") == "Europe/Athens"


def test_the_chatgpt_sign_in_that_cannot_be_read(tmp_path, caplog):
    from api.auth import openai_oauth
    with pytest.raises(HTTPException) as exc:
        openai_oauth._read_login_blob(tmp_path)
    assert exc.value.detail == "The ChatGPT sign-in did not finish. Start the sign-in again."
    (tmp_path / "auth.json").write_text("{not json")
    with caplog.at_level("WARNING"), pytest.raises(HTTPException) as exc:
        openai_oauth._read_login_blob(tmp_path)
    assert exc.value.detail == "Could not read the ChatGPT sign-in. Start the sign-in again."
    assert "Expecting property name" in caplog.text
    (tmp_path / "auth.json").write_text("[]")
    with pytest.raises(HTTPException) as exc:
        openai_oauth._read_login_blob(tmp_path)
    assert exc.value.detail == "Could not read the ChatGPT sign-in. Start the sign-in again."


def test_the_phone_service_that_does_not_answer(monkeypatch, caplog):
    import httpx

    from api.phone import phone_relay

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **kw):
            raise httpx.ConnectError("All connection attempts failed")

    async def no_refusal(url=None):
        return None

    monkeypatch.setattr(phone_relay.daemon_url, "refusal", no_refusal)
    monkeypatch.setattr(phone_relay.daemon_url, "client", lambda url, timeout: Client())
    with caplog.at_level("WARNING"):
        status, detail = _refused(phone_relay._relay("GET", "/calls/x"))
    assert (status, detail) == (502, "The phone service is not answering. Check that it is running.")
    assert "All connection attempts failed" in caplog.text


def test_a_personal_token_the_service_refused(monkeypatch, caplog):
    from api.auth import oauth

    class Provider:
        async def exchange_personal_access_token(self, **kw):
            raise RuntimeError("GitHub userinfo failed: HTTP 401 Bad credentials")

    manifest = SimpleNamespace(credentials=SimpleNamespace(oauth={"flows": ["personal_access_token"]}))
    monkeypatch.setattr(oauth, "_validate_provider_for_mcp", lambda mcp, provider: None)
    monkeypatch.setattr(oauth.mcp_registry, "get_manifest", lambda name: manifest)
    monkeypatch.setattr(oauth, "get_provider", lambda name: Provider())
    with caplog.at_level("WARNING"):
        status, detail = _refused(oauth.pat_save(
            "github", oauth.PatSaveRequest(mcp_name="github-mcp", token="ghp_x"), user=ADMIN))
    assert (status, detail) == (401, "That token was refused. Check it and try again.")
    assert "Bad credentials" in caplog.text
