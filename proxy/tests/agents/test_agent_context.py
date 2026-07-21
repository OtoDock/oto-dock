"""Manifest-driven ``agent_context`` parser + resolver tests.

Covers:
  - ``_parse_agent_context`` strict-validation contract
  - ``_build_token_map`` user-scope vs agent-scope branches
  - ``_resolve_manifest_blocks`` scope filter + ``requires`` semantics
  - Python provider + manifest block coexistence in ``get_dynamic_contexts``

Uses the real PostgreSQL test DB (same fixtures pattern as
``test_multi_account.py``) — no mocking — because the integration
points (credential_store, agent_store) are themselves DB-backed.
"""

from __future__ import annotations

import uuid
import pytest

from services.mcp import dynamic_context, mcp_registry
from services.mcp.mcp_registry import AgentContextBlock, McpManifest, ServerConfig, CredentialConfig
from storage import credential_store
from storage.pg import get_conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_manifest(name: str, agent_context: list[AgentContextBlock]) -> McpManifest:
    """Build a minimal McpManifest carrying agent_context blocks for tests."""
    return McpManifest(
        name=name,
        label=name,
        description="",
        version="0.0.0",
        category="community",
        server=ServerConfig(runtime="python", transport="stdio", command="", args=[]),
        credentials=CredentialConfig(type="per_user"),
        config=[],
        env={},
        agent_env={},
        exclude_from=[],
        skills=[],
        agent_context=agent_context,
    )


@pytest.fixture
def reset_manifests():
    """Snapshot + restore the module-level manifest cache so tests don't leak."""
    saved = dict(mcp_registry._manifests)
    yield
    mcp_registry._manifests = saved


@pytest.fixture
def user_sub(request):
    """Insert a real users row + clean up on teardown."""
    sub = f"test-ac-user-{uuid.uuid4().hex[:12]}"
    username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, "
            "auth_provider, created_at, last_login) "
            "VALUES (%s, %s, 'Test User', %s, 'member', 'local', "
            "NOW()::text, NOW()::text)",
            (sub, f"{username}@example.test", username),
        )
        conn.commit()

    def cleanup():
        with get_conn() as conn:
            conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
            conn.commit()
    request.addfinalizer(cleanup)
    return sub


@pytest.fixture
def agent_name(request):
    """Insert a real agents row + clean up on teardown.

    `agent_context` token resolver reads `agent_store.get_agent(name)`
    for `${agent.display_name}` / description / color, and that
    function is cached so we have to invalidate after teardown.
    """
    from storage import agent_store
    slug = f"test-ac-agent-{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agents (slug, display_name, description, color, "
            "execution_path, created_at, updated_at) "
            "VALUES (%s, %s, 'Test agent for agent_context', '#abc123', "
            "'claude-code-cli', NOW()::text, NOW()::text)",
            (slug, f"Test Agent {slug}"),
        )
        conn.commit()
    agent_store._invalidate_cache()

    def cleanup():
        with get_conn() as conn:
            conn.execute("DELETE FROM agents WHERE slug = %s", (slug,))
            conn.commit()
        agent_store._invalidate_cache()
    request.addfinalizer(cleanup)
    return slug


@pytest.fixture
def mcp_name(request):
    """Unique MCP name per test; cleans up any service-account rows on teardown."""
    name = f"test-ac-mcp-{uuid.uuid4().hex[:8]}"

    def cleanup():
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM service_agent_bindings WHERE mcp_name = %s", (name,),
            )
            conn.execute(
                "DELETE FROM user_credentials WHERE mcp_name = %s", (name,),
            )
            conn.execute(
                "DELETE FROM user_credential_accounts WHERE mcp_name = %s", (name,),
            )
            conn.commit()
    request.addfinalizer(cleanup)
    return name


# ---------------------------------------------------------------------------
# Parser contract (_parse_agent_context)
# ---------------------------------------------------------------------------


class TestParser:
    def test_none_returns_empty_list(self):
        assert mcp_registry._parse_agent_context(None, "x") == []

    def test_missing_returns_empty_list(self):
        # via get(): default is None
        assert mcp_registry._parse_agent_context({}.get("agent_context"), "x") == []

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="must be a list"):
            mcp_registry._parse_agent_context({}, "x")

    def test_rejects_block_missing_template(self):
        with pytest.raises(ValueError, match="template must be a non-empty string"):
            mcp_registry._parse_agent_context([{"requires": ["x"]}], "x")

    def test_rejects_empty_template(self):
        with pytest.raises(ValueError, match="template must be a non-empty string"):
            mcp_registry._parse_agent_context([{"template": "   "}], "x")

    def test_accepts_minimal_block(self):
        blocks = mcp_registry._parse_agent_context([{"template": "hi"}], "x")
        assert len(blocks) == 1
        assert blocks[0].template == "hi"
        assert blocks[0].requires == []
        assert blocks[0].scope == []

    def test_parses_minimal_builder_block(self):
        # Builder is now structured — tool format + args + bounds
        # are strict-validated. Default timeout 5s, default account_label "".
        blocks = mcp_registry._parse_agent_context(
            [{
                "template": "x ${result.name}",
                "builder": {
                    "tool": "mcp__some-server__lookup",
                    "args": {"id": "abc"},
                },
            }],
            "x",
        )
        assert len(blocks) == 1
        assert blocks[0].builder is not None
        assert blocks[0].builder.tool == "mcp__some-server__lookup"
        assert blocks[0].builder.args == {"id": "abc"}
        assert blocks[0].builder.timeout_seconds == 5
        assert blocks[0].builder.account_label == ""

    def test_rejects_unknown_key(self):
        with pytest.raises(ValueError, match="unknown keys"):
            mcp_registry._parse_agent_context(
                [{"template": "x", "typo_field": 1}], "x",
            )

    def test_rejects_invalid_scope_value(self):
        with pytest.raises(ValueError, match="scope contains invalid values"):
            mcp_registry._parse_agent_context(
                [{"template": "x", "scope": ["weird"]}], "x",
            )

    def test_accepts_user_scope(self):
        blocks = mcp_registry._parse_agent_context(
            [{"template": "x", "scope": ["user"]}], "x",
        )
        assert blocks[0].scope == ["user"]

    def test_rejects_non_string_requires(self):
        with pytest.raises(ValueError, match="requires must be a list of non-empty strings"):
            mcp_registry._parse_agent_context(
                [{"template": "x", "requires": [123]}], "x",
            )


# ---------------------------------------------------------------------------
# Token map (_build_token_map)
# ---------------------------------------------------------------------------


class TestTokenMap:
    def test_agent_tokens_always_populated(self, agent_name, mcp_name):
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert tokens["agent.name"] == agent_name
        assert tokens["agent.display_name"].startswith("Test Agent")
        assert tokens["agent.color"] == "#abc123"
        assert tokens["agent.description"] == "Test agent for agent_context"

    def test_user_scope_populates_account_email_from_bound_user_account(
        self, user_sub, agent_name, mcp_name,
    ):
        credential_store.set_user_credentials(
            user_sub, mcp_name,
            {"GOOGLE_EMAIL": "work@example.com", "GOOGLE_SERVICES": "gmail,drive"},
            account_label="work@example.com",
        )
        credential_store.set_account_display_email(
            user_sub, mcp_name, "work@example.com", "work@example.com",
        )
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub=user_sub, user_role="manager", session_ctx={},
        )
        assert tokens["account.label"] == "work@example.com"
        assert tokens["account.email"] == "work@example.com"
        assert tokens["credential.GOOGLE_EMAIL"] == "work@example.com"
        assert tokens["credential.GOOGLE_SERVICES"] == "gmail,drive"
        assert tokens["user.role"] == "manager"
        assert tokens["user.email"].endswith("@example.test")

    def test_agent_scope_populates_account_email_from_bound_account(
        self, agent_name, mcp_name, user_sub,
    ):
        # A manager binds their own connected account as the agent's service
        # identity; agent-scope sessions then read that account.
        credential_store.set_user_credentials(
            user_sub, mcp_name,
            {"GOOGLE_EMAIL": "service@example.com", "GOOGLE_SERVICES": "gmail"},
            account_label="default",
        )
        credential_store.set_account_display_email(
            user_sub, mcp_name, "default", "service@example.com",
        )
        credential_store.set_service_agent_binding(
            mcp_name, agent_name, account_label="default", owner_sub=user_sub,
        )
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert tokens["account.label"] == "default"
        assert tokens["account.email"] == "service@example.com"
        assert tokens["credential.GOOGLE_EMAIL"] == "service@example.com"
        # No user.* tokens for agent-scope
        assert "user.email" not in tokens

    def test_agent_scope_no_binding_leaves_account_tokens_absent(
        self, agent_name, mcp_name,
    ):
        # No binding for this agent → no account.* tokens.
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert "account.email" not in tokens
        assert "account.label" not in tokens

    def test_session_ctx_passthrough(self, agent_name, mcp_name):
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub="", user_role="",
            session_ctx={"task_owner": "abc", "chat_id": "xyz"},
        )
        assert tokens["session.task_owner"] == "abc"
        assert tokens["session.chat_id"] == "xyz"
        assert tokens["session.task_username"] == ""


# ---------------------------------------------------------------------------
# Block evaluator (_resolve_manifest_blocks) — async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBlockResolution:
    async def test_block_renders_with_substitution(
        self, reset_manifests, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(template="Hi ${agent.display_name} from ${agent.name}"),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert len(rendered) == 1
        assert rendered[0].startswith("Hi Test Agent")
        assert agent_name in rendered[0]

    async def test_block_skipped_when_required_token_empty(
        self, reset_manifests, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(
                template="Use ${account.email}",
                requires=["account.email"],
            ),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert rendered == []

    async def test_block_renders_for_service_scope_when_account_bound(
        self, reset_manifests, agent_name, mcp_name, user_sub,
    ):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"GOOGLE_EMAIL": "support@org.com"},
            account_label="default",
        )
        credential_store.set_account_display_email(
            user_sub, mcp_name, "default", "support@org.com",
        )
        credential_store.set_service_agent_binding(
            mcp_name, agent_name, account_label="default", owner_sub=user_sub,
        )
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(
                template="Use ${account.email}",
                requires=["account.email"],
            ),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert rendered == ["Use support@org.com"]

    async def test_block_skipped_when_scope_user_and_session_agent(
        self, reset_manifests, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(template="user only", scope=["user"]),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert rendered == []

    async def test_block_skipped_when_scope_agent_and_session_user(
        self, reset_manifests, user_sub, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(template="agent only", scope=["agent"]),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub=user_sub, user_role="manager",
            session_ctx={},
        )
        assert rendered == []

    async def test_scope_omitted_renders_in_both_scopes(
        self, reset_manifests, user_sub, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(template="universal: ${agent.name}"),
        ])
        u = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub=user_sub, user_role="manager",
            session_ctx={},
        )
        a = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert u == a == [f"universal: {agent_name}"]

    async def test_multiple_blocks_all_emit_in_declared_order(
        self, reset_manifests, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(template="first"),
            AgentContextBlock(template="second ${agent.name}"),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert rendered == ["first", f"second {agent_name}"]

    async def test_missing_token_substitutes_empty_when_not_required(
        self, reset_manifests, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(template="x=${unknown.token}|y=${agent.name}"),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
        )
        assert rendered == [f"x=|y={agent_name}"]


# ---------------------------------------------------------------------------
# Coexistence with Python providers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPythonAndManifestCoexist:
    async def test_python_provider_runs_before_manifest_blocks_for_same_mcp(
        self, reset_manifests, agent_name, mcp_name,
    ):
        def fake(agent_name, **kwargs):
            return "FROM-PYTHON"
        dynamic_context.register(mcp_name, fake)
        try:
            mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
                AgentContextBlock(template="FROM-MANIFEST"),
            ])
            results = await dynamic_context.get_dynamic_contexts(
                agent_name, [mcp_name],
                user_sub="", user_role="", session_ctx={},
            )
            assert results == [
                (mcp_name, "FROM-PYTHON"),
                (mcp_name, "FROM-MANIFEST"),
            ]
        finally:
            dynamic_context._providers.pop(mcp_name, None)

    async def test_get_dynamic_contexts_with_no_kwargs_doesnt_crash(
        self, reset_manifests, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(template="agent=${agent.name}"),
        ])
        results = await dynamic_context.get_dynamic_contexts(agent_name, [mcp_name])
        assert results == [(mcp_name, f"agent={agent_name}")]


# ---------------------------------------------------------------------------
# ${trigger.*} namespace
# ---------------------------------------------------------------------------


class TestTriggerTokensFlat:
    def test_trigger_tokens_normalized_from_payload(self, agent_name, mcp_name):
        payload = {
            "source": "phone",
            "route": "support-inbound",
            "phone": "+15551234567",
            "did": "+18005550100",
        }
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
            trigger_payload=payload,
        )
        assert tokens["trigger.source"] == "phone"
        assert tokens["trigger.route"] == "support-inbound"
        assert tokens["trigger.phone"] == "+15551234567"
        assert tokens["trigger.did"] == "+18005550100"
        # email not in payload at top level OR body → empty
        assert tokens["trigger.email"] == ""

    def test_trigger_phone_fallback_from_body(self, agent_name, mcp_name):
        # No top-level phone — body should provide.
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
            trigger_payload={"body": {"phone": "+1888"}},
        )
        assert tokens["trigger.phone"] == "+1888"

    def test_trigger_email_fallback_from_body(self, agent_name, mcp_name):
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
            trigger_payload={"body": {"email": "user@x.com"}},
        )
        assert tokens["trigger.email"] == "user@x.com"

    def test_trigger_namespace_empty_when_no_payload(self, agent_name, mcp_name):
        # Backward compat: chat sessions never carry a payload.
        tokens = dynamic_context._build_token_map(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
            trigger_payload=None,
        )
        # Trigger keys absent (NOT present-as-empty, to avoid satisfying
        # an accidental ``requires: ["trigger.phone"]`` on chat sessions).
        assert "trigger.phone" not in tokens
        assert "trigger.source" not in tokens


class TestTriggerBodyDotPath:
    def test_trigger_body_dot_path_substitutes_via_template(self):
        payload = {"body": {"user": {"email": "alice@x.com", "id": 42}}}
        rendered = dynamic_context._substitute_tokens(
            "${trigger.body.user.email} (#${trigger.body.user.id})",
            tokens={},
            trigger_payload=payload,
        )
        assert rendered == "alice@x.com (#42)"

    def test_trigger_body_dot_path_returns_empty_on_miss(self):
        payload = {"body": {"a": 1}}
        rendered = dynamic_context._substitute_tokens(
            "${trigger.body.nonexistent.key}",
            tokens={},
            trigger_payload=payload,
        )
        assert rendered == ""

    def test_trigger_body_nested_serializes_to_json(self):
        payload = {"body": {"addresses": ["123 Main", "456 Oak"]}}
        rendered = dynamic_context._substitute_tokens(
            "list: ${trigger.body.addresses}",
            tokens={},
            trigger_payload=payload,
        )
        assert rendered.startswith('list: ["123 Main"')

    def test_trigger_body_walk_safe_with_no_payload(self):
        # Defensive: trigger_payload=None must not raise.
        rendered = dynamic_context._substitute_tokens(
            "${trigger.body.anything}",
            tokens={},
            trigger_payload=None,
        )
        assert rendered == ""


@pytest.mark.asyncio
class TestTriggerRequiresGate:
    async def test_trigger_required_block_skipped_without_payload(
        self, reset_manifests, agent_name, mcp_name,
    ):
        # A trigger-gated block must NOT render for plain chat sessions.
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(
                template="Phone: ${trigger.phone}",
                requires=["trigger.phone"],
            ),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
            trigger_payload=None,
        )
        assert rendered == []

    async def test_trigger_required_block_renders_with_payload(
        self, reset_manifests, agent_name, mcp_name,
    ):
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(
                template="Phone: ${trigger.phone}",
                requires=["trigger.phone"],
            ),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
            trigger_payload={"phone": "+1234"},
        )
        assert rendered == ["Phone: +1234"]

    async def test_trigger_body_dot_path_satisfies_requires(
        self, reset_manifests, agent_name, mcp_name,
    ):
        # `requires` should accept dot-path tokens too — same walking
        # semantics as substitution so authors get a consistent contract.
        mcp_registry._manifests[mcp_name] = _make_manifest(mcp_name, [
            AgentContextBlock(
                template="Issue: ${trigger.body.issue.title}",
                requires=["trigger.body.issue.title"],
            ),
        ])
        rendered = await dynamic_context._resolve_manifest_blocks(
            mcp_name, agent_name, user_sub="", user_role="", session_ctx={},
            trigger_payload={"body": {"issue": {"title": "ship it"}}},
        )
        assert rendered == ["Issue: ship it"]


# ---------------------------------------------------------------------------
# Delegation roster (delegate() layers/models discoverability)
# ---------------------------------------------------------------------------


def _roster_env(monkeypatch, *, models, default_model="m-default",
                paths=("claude-code-cli",)):
    import json as _json

    import config as app_config
    from storage import agent_store, subscription_store

    agent_row = {
        "display_name": "Worker", "description": "does work",
        "execution_path": paths[0],
        "execution_paths": (_json.dumps(list(paths[1:]))
                            if len(paths) > 1 else ""),
        "default_model": default_model,
    }
    monkeypatch.setattr(agent_store, "get_agent", lambda slug: dict(agent_row))
    monkeypatch.setattr(subscription_store, "list_models",
                        lambda layer=None: [dict(m) for m in models])
    monkeypatch.setattr(app_config, "resolve_agent_model",
                        lambda slug: default_model)


def test_build_delegation_roster_enabled_only_default_first(monkeypatch):
    _roster_env(monkeypatch, models=[
        {"model_id": "m-a", "enabled": True},
        {"model_id": "m-disabled", "enabled": False},
        {"model_id": "m-default", "enabled": True},
    ])
    roster = dynamic_context.build_delegation_roster(["worker"])
    (layer,) = roster["worker"]
    assert layer["path"] == "claude-code-cli" and layer["is_default"]
    assert layer["models"] == ["m-default", "m-a"]
    assert layer["more"] == 0 and not layer["local_only"]


def test_build_delegation_roster_caps_and_tags(monkeypatch):
    _roster_env(
        monkeypatch,
        models=[{"model_id": f"m-{i}", "enabled": True} for i in range(10)]
        + [{"model_id": "m-default", "enabled": True}],
        paths=("claude-code-cli", "direct-llm"),
    )
    layers = dynamic_context.build_delegation_roster(["worker"])["worker"]
    cli, direct = layers
    assert len(cli["models"]) == 8 and cli["models"][0] == "m-default"
    assert cli["more"] == 3  # 11 enabled − 8 shown
    assert direct["path"] == "direct-llm" and direct["local_only"]
    assert not direct["is_default"]


def test_delegation_context_renders_roster(monkeypatch, temp_db):
    _roster_env(monkeypatch, models=[
        {"model_id": "m-default", "enabled": True},
        {"model_id": "m-b", "enabled": True},
    ])
    roster = dynamic_context.build_delegation_roster(["worker"])
    text = dynamic_context._delegation_mcp_context(
        "self-agent", ["worker"], delegation_roster=roster)
    assert ("layers: claude-code-cli (default | models: "
            "m-default [default], m-b)") in text
    assert "Ignored with `continue_id`" in text
    # Without the pre-resolved roster the provider stays name-only (no I/O).
    text2 = dynamic_context._delegation_mcp_context("self-agent", ["worker"])
    assert "layers:" not in text2
