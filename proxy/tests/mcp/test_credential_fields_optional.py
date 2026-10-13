"""``credentials.fields`` honours ``"required": false``: an optional field
left unset keeps the MCP in the session, a required one an account lacks
leaves it out with a reason naming the field, and the save route accepts an
account without its optional fields (or an edit adding only one)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from services.oauth import credential_resolver
from storage.identity import credential_store

SCHEMA = {
    "type": "per_user", "label": "Email",
    "fields": [
        {"key": "EMAIL_USER", "label": "User"},
        {"key": "EMAIL_PASSWORD", "label": "Password", "required": True},
        {"key": "EMAIL_ADDRESS", "label": "Sender address", "required": False},
        {"key": "SIGNATURE", "label": "Signature", "required": "false"},  # a string: required
    ],
}


def test_which_fields_are_required():
    assert credential_resolver.required_field_keys(SCHEMA) == [
        "EMAIL_USER", "EMAIL_PASSWORD", "SIGNATURE"]
    assert credential_resolver.missing_required_labels(
        SCHEMA, {"EMAIL_USER": "u", "EMAIL_PASSWORD": "p"}) == ["Signature"]


@pytest.fixture
def account(monkeypatch):
    held: dict[str, str] = {}
    monkeypatch.setattr(credential_resolver, "pick_account",
                        lambda mcp, agent, user_sub=None: credential_resolver.AccountRef(
                            label="default", owner_sub="u1"))
    monkeypatch.setattr(credential_store, "get_user_credentials", lambda sub, mcp, label: dict(held))
    from storage.mcp import mcp_store
    monkeypatch.setattr(mcp_store, "get_mcp_config_values", lambda n: {})
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda n: None)
    return held


def test_an_optional_field_left_unset_keeps_the_mcp(account):
    account.update({"EMAIL_USER": "u", "EMAIL_PASSWORD": "p", "SIGNATURE": ""})
    env = credential_resolver._resolve_per_user("email", SCHEMA, "u1", "user", agent_name="a")
    assert env == {"EMAIL_USER": "u", "EMAIL_PASSWORD": "p", "SIGNATURE": ""}


def test_a_required_field_the_account_lacks_is_named(account, monkeypatch):
    """A field that became required on an upgrade: the people whose account
    lacks it lose the MCP, and the session says which field."""
    from services.mcp import mcp_registry
    account.update({"EMAIL_USER": "u", "EMAIL_PASSWORD": "p"})
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements",
                        lambda agent: [type("M", (), {"name": "email"})()])
    monkeypatch.setattr(mcp_registry, "get_credential_schema", lambda name: SCHEMA)
    result = credential_resolver.resolve_credentials("agent", "u1", task_scope="user")
    assert "email" in result.excluded_mcps
    assert result.exclusion_reasons["email"].startswith("Email needs Signature")


def test_a_field_turned_optional_removes_nobody(account, monkeypatch):
    from services.mcp import mcp_registry
    account.update({"EMAIL_USER": "u", "EMAIL_PASSWORD": "p", "SIGNATURE": "s"})
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements",
                        lambda agent: [type("M", (), {"name": "email"})()])
    monkeypatch.setattr(mcp_registry, "get_credential_schema", lambda name: SCHEMA)
    result = credential_resolver.resolve_credentials("agent", "u1", task_scope="user")
    assert "email" in result.available_mcps


def test_an_infra_mcp_whose_fields_are_all_optional_needs_no_save(monkeypatch):
    monkeypatch.setattr(credential_store, "get_infra_credentials", lambda n: {})
    optional = {"fields": [{"key": "A", "required": False}]}
    assert credential_resolver._resolve_infra("x", optional) == {}
    assert credential_resolver._resolve_infra("x", {"fields": [{"key": "A"}]}) is None
    assert credential_resolver._resolve_infra("x", {"fields": []}) == {}


# ---------------------------------------------------------------------------
# The save route
# ---------------------------------------------------------------------------

@pytest.fixture
def save(monkeypatch):
    from api.mcp import credentials as api
    from auth.providers import UserContext
    stored: dict[str, str] = {}
    written = []
    monkeypatch.setattr(api, "_get_cred_schema", lambda n: SCHEMA)
    monkeypatch.setattr(credential_store, "get_user_credentials", lambda sub, mcp, label: dict(stored))
    monkeypatch.setattr(credential_store, "set_user_credentials",
                        lambda sub, mcp, creds, label: written.append(creds))
    monkeypatch.setattr(credential_store, "set_account_display_email", lambda *a: None)
    user = UserContext(sub="u1", email="u@x", name="u", role="member")

    async def _put(creds: dict):
        return await api.set_my_integration(
            "email", api.SetCredentialRequest(credentials=creds), user)
    return _put, stored, written


@pytest.mark.asyncio
async def test_an_account_without_its_optional_fields_saves(save):
    put, stored, written = save
    await put({"EMAIL_USER": "u", "EMAIL_PASSWORD": "p", "SIGNATURE": "s", "JUNK": "x"})
    assert written == [{"EMAIL_USER": "u", "EMAIL_PASSWORD": "p", "SIGNATURE": "s"}]


@pytest.mark.asyncio
async def test_an_edit_adding_an_optional_field_alone_saves(save):
    put, stored, written = save
    stored.update({"EMAIL_USER": "u", "EMAIL_PASSWORD": "p", "SIGNATURE": "s"})
    await put({"EMAIL_ADDRESS": "me@example.com"})
    assert written == [{"EMAIL_ADDRESS": "me@example.com"}]


@pytest.mark.asyncio
async def test_a_required_field_nobody_gave_is_refused(save):
    put, stored, written = save
    with pytest.raises(HTTPException) as ei:
        await put({"EMAIL_USER": "u", "EMAIL_ADDRESS": "x"})
    assert ei.value.status_code == 400
    assert "EMAIL_PASSWORD" in ei.value.detail and "EMAIL_ADDRESS" not in ei.value.detail
    assert written == []
