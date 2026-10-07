"""Builder-block tests — parser, post-load validator, executor.

Three layers:
  1. ``_parse_builder_block`` — structural validation (tool format,
     args type, timeout bounds, account_label type, unknown keys).
  2. ``_validate_builder_block_transports`` — post-load cross-MCP check
     that the referenced MCP exists and is HTTP-class.
  3. ``builder_executor.execute_builder`` — end-to-end through a real
     in-process FastMCP server (``mock_mcp`` fixture), exercising token
     substitution, timeout, error handling, and parallel evaluation.
"""

from __future__ import annotations

import asyncio
import gc
import json
import time
import uuid

import pytest

from services.mcp import builder_executor, dynamic_context, mcp_registry
from services.mcp.mcp_registry import (
    AgentContextBlock,
    AgentContextBuilder,
    CredentialConfig,
    McpManifest,
    ServerConfig,
)

# Re-export the mock_mcp fixture so this file picks it up.
from tests.fixtures.mock_mcp_server import mock_mcp  # noqa: F401


# ---------------------------------------------------------------------------
# Layer 1 — parser
# ---------------------------------------------------------------------------


class TestBuilderParser:
    def test_minimal_builder_block_parses(self):
        blocks = mcp_registry._parse_agent_context([{
            "template": "x ${result.foo}",
            "builder": {
                "tool": "mcp__svr__lookup",
                "args": {},
            },
        }], "x")
        assert blocks[0].builder is not None
        assert blocks[0].builder.tool == "mcp__svr__lookup"
        assert blocks[0].builder.args == {}
        assert blocks[0].builder.timeout_seconds == 5
        assert blocks[0].builder.account_label == ""

    def test_rejects_invalid_tool_format(self):
        for bad in ("lookup", "mcp__svr", "MCP__svr__tool", "mcp__svr__"):
            with pytest.raises(ValueError, match="builder.tool"):
                mcp_registry._parse_agent_context([{
                    "template": "x",
                    "builder": {"tool": bad, "args": {}},
                }], "x")

    def test_rejects_non_dict_args(self):
        with pytest.raises(ValueError, match="builder.args"):
            mcp_registry._parse_agent_context([{
                "template": "x",
                "builder": {"tool": "mcp__a__b", "args": [1, 2, 3]},
            }], "x")

    def test_rejects_timeout_under_one(self):
        with pytest.raises(ValueError, match="timeout_seconds"):
            mcp_registry._parse_agent_context([{
                "template": "x",
                "builder": {"tool": "mcp__a__b", "args": {}, "timeout_seconds": 0},
            }], "x")

    def test_rejects_timeout_over_thirty(self):
        with pytest.raises(ValueError, match="timeout_seconds"):
            mcp_registry._parse_agent_context([{
                "template": "x",
                "builder": {"tool": "mcp__a__b", "args": {}, "timeout_seconds": 60},
            }], "x")

    def test_rejects_bool_timeout(self):
        with pytest.raises(ValueError, match="must be an integer"):
            mcp_registry._parse_agent_context([{
                "template": "x",
                "builder": {"tool": "mcp__a__b", "args": {}, "timeout_seconds": True},
            }], "x")

    def test_rejects_unknown_builder_key(self):
        with pytest.raises(ValueError, match="unknown keys"):
            mcp_registry._parse_agent_context([{
                "template": "x",
                "builder": {"tool": "mcp__a__b", "args": {}, "typo": 1},
            }], "x")

    def test_rejects_non_dict_builder(self):
        with pytest.raises(ValueError, match="builder must be an object"):
            mcp_registry._parse_agent_context([{
                "template": "x",
                "builder": "mcp__a__b",
            }], "x")


# ---------------------------------------------------------------------------
# Layer 2 — post-load transport validation
# ---------------------------------------------------------------------------


def _make_manifest(name, *, transport="stdio", agent_context=()):
    return McpManifest(
        name=name,
        label=name,
        description="",
        version="0.0.0",
        category="community",
        server=ServerConfig(
            runtime="python", transport=transport, command="", args=[],
            url_template=f"http://127.0.0.1:{8000 + len(name)}",
        ),
        credentials=CredentialConfig(type="none"),
        config=[],
        env={},
        agent_env={},
        exclude_from=[],
        skills=[],
        agent_context=list(agent_context),
    )


@pytest.fixture
def reset_manifests():
    saved = dict(mcp_registry._manifests)
    yield
    mcp_registry._manifests = saved


class TestBuilderTransportValidation:
    def test_drops_block_when_target_mcp_missing(self, reset_manifests):
        block = AgentContextBlock(
            template="x",
            builder=AgentContextBuilder(tool="mcp__nonexistent__foo", args={}),
        )
        owner = _make_manifest("owner", agent_context=[block])
        mcp_registry._manifests = {"owner": owner}
        mcp_registry._validate_builder_block_transports()
        # Block dropped — manifest stays.
        assert mcp_registry._manifests["owner"].agent_context == []

    def test_drops_block_when_target_mcp_is_stdio(self, reset_manifests):
        # Stdio target — rejected at post-load.
        block = AgentContextBlock(
            template="x",
            builder=AgentContextBuilder(tool="mcp__stdio-mcp__foo", args={}),
        )
        owner = _make_manifest("owner", agent_context=[block])
        target = _make_manifest("stdio-mcp", transport="stdio")
        mcp_registry._manifests = {"owner": owner, "stdio-mcp": target}
        mcp_registry._validate_builder_block_transports()
        assert mcp_registry._manifests["owner"].agent_context == []

    def test_keeps_block_when_target_is_http(self, reset_manifests):
        block = AgentContextBlock(
            template="x",
            builder=AgentContextBuilder(tool="mcp__http-mcp__foo", args={}),
        )
        owner = _make_manifest("owner", agent_context=[block])
        target = _make_manifest("http-mcp", transport="http")
        mcp_registry._manifests = {"owner": owner, "http-mcp": target}
        mcp_registry._validate_builder_block_transports()
        assert len(mcp_registry._manifests["owner"].agent_context) == 1

    def test_template_only_blocks_pass_through(self, reset_manifests):
        # No builder → no transport check applies.
        block = AgentContextBlock(template="hi", builder=None)
        owner = _make_manifest("owner", agent_context=[block])
        mcp_registry._manifests = {"owner": owner}
        mcp_registry._validate_builder_block_transports()
        assert len(mcp_registry._manifests["owner"].agent_context) == 1


# ---------------------------------------------------------------------------
# Layer 3 — executor (real FastMCP)
# ---------------------------------------------------------------------------


def _block(template, args=None, *, tool, timeout=5, requires=None):
    return AgentContextBlock(
        template=template,
        requires=requires or [],
        builder=AgentContextBuilder(
            tool=tool, args=args or {}, timeout_seconds=timeout,
        ),
    )


def _register_mock_as_mcp(handle, slug):
    """Register the running mock_mcp under ``slug`` in the registry so
    ``builder_executor`` looks it up exactly like a real MCP. The mock
    runs as ``streamable-http`` transport — matches the production
    streamable_http_manager path inside builder_executor.
    """
    manifest = McpManifest(
        name=slug,
        label=slug,
        description="",
        version="0.0.0",
        category="community",
        server=ServerConfig(
            runtime="python", transport="streamable-http", command="", args=[],
            url_template=handle.mcp_url,  # full URL — resolver leaves it alone
        ),
        credentials=CredentialConfig(type="none"),
        config=[],
        env={},
        agent_env={},
        exclude_from=[],
        skills=[],
        agent_context=[],
    )
    mcp_registry._manifests[slug] = manifest


@pytest.mark.asyncio
class TestBuilderExecutor:
    async def test_calls_tool_and_renders_template(self, mock_mcp, reset_manifests):
        slug = f"mock-{uuid.uuid4().hex[:6]}"
        _register_mock_as_mcp(mock_mcp, slug)

        async def lookup(name: str) -> str:
            return json.dumps({"name": name.upper(), "tier": "pro"})

        mock_mcp.register("lookup", lookup, schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        })

        block = _block(
            "Hi ${result.name} (${result.tier})",
            args={"name": "alice"},
            tool=f"mcp__{slug}__lookup",
        )
        rendered = await builder_executor.execute_builder(
            block=block, tokens={}, trigger_payload=None,
            mcp_name="caller-mcp", agent_name="some-agent",
        )
        assert rendered == "Hi ALICE (pro)"

    async def test_substitutes_trigger_tokens_in_args(
        self, mock_mcp, reset_manifests,
    ):
        slug = f"mock-{uuid.uuid4().hex[:6]}"
        _register_mock_as_mcp(mock_mcp, slug)

        received: dict = {}

        async def lookup(phone: str) -> str:
            received["phone"] = phone
            return json.dumps({"id": "x", "phone": phone})

        mock_mcp.register("lookup", lookup, schema={
            "type": "object",
            "properties": {"phone": {"type": "string"}},
            "required": ["phone"],
        })

        block = _block(
            "id=${result.id}",
            args={"phone": "${trigger.phone}"},
            tool=f"mcp__{slug}__lookup",
        )
        tokens = dynamic_context._build_trigger_tokens({"phone": "+15551234"})
        rendered = await builder_executor.execute_builder(
            block=block, tokens=tokens,
            trigger_payload={"phone": "+15551234"},
            mcp_name="owner", agent_name="agent",
        )
        # The tool ran on the sender's value: its result is fenced in the prompt.
        from services.infra import external_data
        assert rendered == external_data.with_note(f"id={external_data.fence('x')}")
        assert received["phone"] == "+15551234"

    async def test_returns_none_on_timeout(self, mock_mcp, reset_manifests):
        slug = f"mock-{uuid.uuid4().hex[:6]}"
        _register_mock_as_mcp(mock_mcp, slug)

        # Event-gated instead of a fixed sleep: the test releases the
        # handler right after the client times out, so no in-flight handler
        # survives to fixture teardown. A hard-cancelled uvicorn with an
        # in-flight handler leaves abandoned server-side async generators
        # whose deferred GC-finalization can poison a LATER test's
        # in-process server — the same finalize-on-a-foreign-loop failure
        # the executor quarantines against on the client side.
        release = asyncio.Event()

        async def slow() -> str:
            await asyncio.wait_for(release.wait(), timeout=8)
            return json.dumps({"ok": True})

        mock_mcp.register("slow", slow)

        block = _block(
            "${result.ok}",
            args={},
            tool=f"mcp__{slug}__slow",
            timeout=1,  # 1s ceiling, handler blocks until released
        )
        t0 = time.monotonic()
        rendered = await builder_executor.execute_builder(
            block=block, tokens={}, trigger_payload=None,
            mcp_name="owner", agent_name="agent",
        )
        elapsed = time.monotonic() - t0
        release.set()
        await asyncio.sleep(0.05)  # let the handler drain before teardown
        assert rendered is None
        assert elapsed < 2.5  # bailed early

    async def test_timeout_does_not_poison_subsequent_calls(
        self, mock_mcp, reset_manifests,
    ):
        """A timed-out call must not break later streamable-http calls.

        The executor quarantines each invocation on a throwaway event
        loop in its own thread, so whatever a client timeout abandons
        (suspended SDK async generators, a wedged task-group teardown)
        dies with that loop. The gc.collect() below must therefore find
        nothing to finalize on this shared loop, and the follow-up call
        must succeed.
        """
        slug = f"mock-{uuid.uuid4().hex[:6]}"
        _register_mock_as_mcp(mock_mcp, slug)

        # Event-gated for the same reason as test_returns_none_on_timeout:
        # no in-flight handler may survive to fixture teardown.
        release = asyncio.Event()

        async def slow() -> str:
            await asyncio.wait_for(release.wait(), timeout=8)
            return json.dumps({"ok": True})

        async def fast() -> str:
            return json.dumps({"name": "carol"})

        mock_mcp.register("slow", slow)
        mock_mcp.register("fast", fast)

        timed_out = await builder_executor.execute_builder(
            block=_block(
                "${result.ok}", args={}, tool=f"mcp__{slug}__slow", timeout=1,
            ),
            tokens={}, trigger_payload=None,
            mcp_name="owner", agent_name="agent",
        )
        release.set()
        await asyncio.sleep(0.05)  # let the handler drain before teardown
        assert timed_out is None

        # Force finalization of anything the timed-out call left behind —
        # on the shared loop this is what would trigger the poison.
        gc.collect()
        await asyncio.sleep(0.2)

        rendered = await builder_executor.execute_builder(
            block=_block(
                "Hi ${result.name}", args={}, tool=f"mcp__{slug}__fast",
            ),
            tokens={}, trigger_payload=None,
            mcp_name="owner", agent_name="agent",
        )
        assert rendered == "Hi carol"

    async def test_returns_none_when_tool_unknown_to_server(
        self, mock_mcp, reset_manifests,
    ):
        slug = f"mock-{uuid.uuid4().hex[:6]}"
        _register_mock_as_mcp(mock_mcp, slug)
        # Don't register any tools — server will return an error string.

        block = _block(
            "${result.text}",
            args={},
            tool=f"mcp__{slug}__nope",
        )
        rendered = await builder_executor.execute_builder(
            block=block, tokens={}, trigger_payload=None,
            mcp_name="owner", agent_name="agent",
        )
        # Either renders the error JSON or returns None — both are
        # acceptable "not a usable profile" outcomes. We assert no crash.
        assert rendered is None or "error" in rendered

    async def test_skips_when_target_mcp_is_stdio(self, reset_manifests):
        # Defence in depth: even if post-load validator missed it, executor
        # rejects stdio MCPs at call time.
        stdio = McpManifest(
            name="stdio-target",
            label="stdio-target",
            description="",
            version="0.0.0",
            category="community",
            server=ServerConfig(
                runtime="python", transport="stdio", command="", args=[],
            ),
            credentials=CredentialConfig(type="none"),
            config=[],
            env={}, agent_env={}, exclude_from=[], skills=[],
            agent_context=[],
        )
        mcp_registry._manifests["stdio-target"] = stdio
        block = _block(
            "x ${result.name}", args={},
            tool="mcp__stdio-target__foo",
        )
        rendered = await builder_executor.execute_builder(
            block=block, tokens={}, trigger_payload=None,
            mcp_name="owner", agent_name="agent",
        )
        assert rendered is None


@pytest.mark.asyncio
class TestBuilderParallelEvaluation:
    async def test_two_builders_run_in_parallel(
        self, mock_mcp, reset_manifests, monkeypatch,
    ):
        """Two 1.5s tool calls under one MCP must OVERLAP in the mock's
        handlers — proves ``asyncio.gather`` is firing them concurrently.
        Overlap is observed directly (in-flight counter in the in-process
        mock) instead of asserting wall-clock elapsed: a loaded test host
        can push a genuinely-parallel run past any fixed time threshold,
        but it can never make two serial calls overlap."""
        slug = f"mock-{uuid.uuid4().hex[:6]}"
        _register_mock_as_mcp(mock_mcp, slug)

        in_flight = {"now": 0, "peak": 0}

        async def slow_a() -> str:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            try:
                await asyncio.sleep(1.5)
            finally:
                in_flight["now"] -= 1
            return json.dumps({"a": 1})

        async def slow_b() -> str:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            try:
                await asyncio.sleep(1.5)
            finally:
                in_flight["now"] -= 1
            return json.dumps({"b": 2})

        mock_mcp.register("slow_a", slow_a)
        mock_mcp.register("slow_b", slow_b)

        # Pre-warm the mock MCP HTTP transport so the first builder doesn't
        # pay connection-setup latency inside the timed section.
        warmup_block = _block(
            "warmup=${result.text}",
            args={}, tool=f"mcp__{slug}__slow_a", timeout=30,
        )
        await builder_executor.execute_builder(
            block=warmup_block, tokens={}, trigger_payload=None,
            mcp_name="owner", agent_name="agent",
        )

        block_a = _block("A=${result.a}", args={}, tool=f"mcp__{slug}__slow_a", timeout=30)
        block_b = _block("B=${result.b}", args={}, tool=f"mcp__{slug}__slow_b", timeout=30)

        # Drive through _resolve_manifest_blocks so the gather path runs.
        owner_slug = f"owner-{uuid.uuid4().hex[:6]}"
        owner = McpManifest(
            name=owner_slug, label=owner_slug, description="", version="0.0.0",
            category="community",
            server=ServerConfig(runtime="python", transport="stdio", command="", args=[]),
            credentials=CredentialConfig(type="none"),
            config=[], env={}, agent_env={}, exclude_from=[], skills=[],
            agent_context=[block_a, block_b],
        )
        mcp_registry._manifests[owner_slug] = owner

        # Stub out the token-map builder so it doesn't try to load
        # nonexistent agent / service-account rows from the DB.
        monkeypatch.setattr(
            dynamic_context, "_build_token_map",
            lambda *a, **kw: {"agent.name": "test"},
        )

        # Reset after the warmup call so its slow_a in-flight count (max 1)
        # can't satisfy the overlap assertion.
        in_flight["peak"] = 0

        rendered = await dynamic_context._resolve_manifest_blocks(
            owner_slug, "test", user_sub="", user_role="", session_ctx={},
        )
        assert sorted(rendered) == ["A=1", "B=2"]
        # Serial execution can never have two handlers in flight at once.
        assert in_flight["peak"] == 2, (
            f"Builders ran serially (peak concurrent handlers={in_flight['peak']})"
        )


# ---------------------------------------------------------------------------
# Result flattening
# ---------------------------------------------------------------------------


class TestResultFlatten:
    def test_flat_object_explodes_to_dotted_tokens(self):
        tokens = builder_executor._flatten_result(json.dumps({
            "name": "Alice", "age": 30,
        }))
        assert tokens["result.name"] == "Alice"
        assert tokens["result.age"] == "30"
        # raw text always present as fallback
        assert "result.text" in tokens

    def test_nested_object_serializes_to_json(self):
        tokens = builder_executor._flatten_result(json.dumps({
            "address": {"city": "Berlin"},
        }))
        assert json.loads(tokens["result.address"]) == {"city": "Berlin"}

    def test_array_root_exposes_length_and_json(self):
        tokens = builder_executor._flatten_result(json.dumps([1, 2, 3]))
        assert tokens["result.length"] == "3"
        assert json.loads(tokens["result.json"]) == [1, 2, 3]

    def test_non_json_only_exposes_text(self):
        tokens = builder_executor._flatten_result("plain text result")
        assert tokens == {"result.text": "plain text result"}

    def test_empty_input_returns_empty_dict(self):
        assert builder_executor._flatten_result("") == {}
        assert builder_executor._flatten_result("   ") == {}


@pytest.mark.asyncio
async def test_a_header_style_key_builder_runs_with_the_resolved_key(
    mock_mcp, reset_manifests, monkeypatch,
):
    """A builder on an ``api_key_header`` MCP takes the key's value from the
    MCP's resolved secrets (the value the session bundle would carry), so
    the gateway entry is built and the call runs instead of being skipped."""
    from core.config import deployment
    from core.credentials import mcp_gateway
    from services.oauth import credential_resolver
    from storage.identity import bearer_allowlist

    slug = f"mock-{uuid.uuid4().hex[:6]}"
    _register_mock_as_mcp(mock_mcp, slug)
    m = mcp_registry._manifests[slug]
    m.credentials = CredentialConfig(type="per_user", api_key_header={
        "name": "X-Test-Key", "value_from": "TEST_KEY", "proposed_hosts": ["127.0.0.1"],
    })

    async def lookup() -> str:
        return json.dumps({"ok": "yes"})

    mock_mcp.register("lookup", lookup, schema={"type": "object", "properties": {}})

    seen: dict = {}

    def resolve(agent, user_sub, *, task_scope="user"):
        seen["call"] = (agent, user_sub, task_scope)
        return credential_resolver.ResolvedCredentials(
            env_by_mcp={slug: {"TEST_KEY": "sekret", "PLAIN": "x"}},
            secret_keys={"TEST_KEY"},
        )

    monkeypatch.setattr(credential_resolver, "resolve_credentials", resolve)
    monkeypatch.setattr(deployment, "is_proxy_local_mcp_host", lambda host, manifest: False)
    monkeypatch.setattr(bearer_allowlist, "is_host_allowed", lambda key, host: True)
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda upstream: None)
    mcp_gateway.forget_memos()

    rendered = await builder_executor.execute_builder(
        block=_block("Got ${result.ok}", tool=f"mcp__{slug}__lookup"),
        tokens={}, trigger_payload=None,
        mcp_name="caller-mcp", agent_name="some-agent", user_sub="u-1",
    )
    assert rendered == "Got yes"
    assert seen["call"] == ("some-agent", "u-1", "user")


@pytest.mark.asyncio
async def test_a_header_style_key_builder_reads_an_env_delivered_instance_field(
    mock_mcp, reset_manifests, monkeypatch,
):
    """``value_from`` naming an env-delivered ``instances.fields`` key: the
    builder takes the value from the agent's instance as the session build
    does, instead of skipping the block for a key the resolver never holds."""
    from types import SimpleNamespace
    from core.config import deployment
    from core.credentials import mcp_gateway
    from services.oauth import credential_resolver
    from storage.identity import bearer_allowlist
    from storage.mcp import mcp_store

    slug = f"mock-{uuid.uuid4().hex[:6]}"
    _register_mock_as_mcp(mock_mcp, slug)
    m = mcp_registry._manifests[slug]
    m.credentials = CredentialConfig(type="none", api_key_header={
        "name": "X-Test-Key", "value_from": "INST_KEY", "proposed_hosts": ["127.0.0.1"],
    })
    m.instances = SimpleNamespace(delivery="env", fields=[])

    async def lookup() -> str:
        return json.dumps({"ok": "inst"})

    mock_mcp.register("lookup", lookup, schema={"type": "object", "properties": {}})
    monkeypatch.setattr(
        credential_resolver, "resolve_credentials",
        lambda agent, user_sub, *, task_scope="user":
        credential_resolver.ResolvedCredentials(env_by_mcp={}, secret_keys=set()))
    monkeypatch.setattr(mcp_store, "get_instance_for_agent_env_delivery",
                        lambda mcp_name, agent_name: {"field_values": {"INST_KEY": "inst-key"}}
                        if (mcp_name, agent_name) == (slug, "some-agent") else None)
    monkeypatch.setattr(deployment, "is_proxy_local_mcp_host", lambda host, manifest: False)
    monkeypatch.setattr(bearer_allowlist, "is_host_allowed", lambda key, host: True)
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda upstream: None)
    mcp_gateway.forget_memos()

    rendered = await builder_executor.execute_builder(
        block=_block("Got ${result.ok}", tool=f"mcp__{slug}__lookup"),
        tokens={}, trigger_payload=None,
        mcp_name="caller-mcp", agent_name="some-agent", user_sub="u-1",
    )
    assert rendered == "Got inst"
