"""A rig for the session builders with nothing stubbed but the collaborators
that already run in worker threads (the MCP config build, the prompt build,
the persistent CLI dir, the subscription) and the three text renderers whose
words belong to other modules (the permission context, the dashboard
context, the OTO_* environment). Everything a builder reads itself (the
agent and user rows, the delegation targets, the visibility and memory
toggles, the agent's MCP set, the model) runs real against the test
database, so the characterization goldens and the loop inventory see the
real reads. Each stubbed collaborator records its arguments, in call order.
"""
import asyncio
import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace

AGENT = "char-agent"
PEER = "char-peer"
SHARED = "char-shared"
SESSION_TOKEN = "<session-token>"
MODEL = "claude-char-1"


def _set_username(sub: str, username: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        conn.commit()


def _manifest(name: str, *, path_env: dict | None = None):
    from services.mcp.mcp_manifest_types import (
        CredentialConfig,
        McpManifest,
        ServerConfig,
    )
    return McpManifest(
        name=name, label=name, description="", version="1.0.0", category="custom",
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(type="none"),
        config=[], env={}, agent_env={}, exclude_from=[], skills=[],
        path_env=path_env or {},
    )


def install(monkeypatch, tmp_path) -> SimpleNamespace:
    """The agents, the people, two enabled MCPs on the agent (one with a
    ``path_env``, one the phone MCP, so the call-control branch runs), one
    model, and the recording stubs. Returns the recorder."""
    from core.config import (
        config_builder as cb,
        phone_config_builder as pcb,
        task_config_builder as tcb,
    )
    from services.mcp import dynamic_context, mcp_registry
    from services.mcp.mcp_manifest_types import PathEnvDecl
    from services.meetings import meeting_context as mc
    from storage.agents import agent_store
    from storage.billing import subscription_store
    from storage.identity import db_users
    from storage.mcp import mcp_store

    agent_store.create_agent(AGENT, "Char Agent")
    agent_store.create_agent(PEER, "Char Peer")
    agent_store.create_agent(SHARED, "Char Shared", collaborative=False, default_scope="agent")
    agent_store.set_delegation_targets(AGENT, [PEER])
    _set_username("user-admin", "ada")
    _set_username("user-viewer", "vic")
    _set_username("user-manager", "max")
    db_users.add_user_agent("user-viewer", AGENT, "editor", "user-admin")
    db_users.add_user_agent("user-viewer", SHARED, "viewer", "user-admin")
    db_users.add_user_agent("user-manager", AGENT, "manager", "user-admin")
    subscription_store.add_model("claude-code-cli", MODEL, "Char", provider="anthropic", tier=2)

    files = _manifest("char-files", path_env={"CHAR_FILES_DIR": PathEnvDecl(role="workspace", subpath="files")})
    phone = _manifest("phone-mcp")
    monkeypatch.setitem(mcp_registry._manifests, files.name, files)
    monkeypatch.setitem(mcp_registry._manifests, phone.name, phone)
    for agent in (AGENT, SHARED):
        mcp_store.set_manager_enabled_mcps(agent, [files.name, phone.name])
    mcp_store.set_mcp_enabled(files.name, True)
    mcp_store.set_mcp_enabled(phone.name, True)

    rec = SimpleNamespace(calls=[])

    def _note(name, args, kwargs):
        rec.calls.append({"call": name, "args": list(args), "kwargs": dict(kwargs)})

    def _mcp_build(*args, **kwargs):
        _note("build_session_mcp_config", args, kwargs)
        return (None, {"MCP_CRED": "1"}, {}, {}, set())

    def _prompt(*args, **kwargs):
        _note("build_agent_prompt", args, kwargs)
        return "PROMPT"

    def _persistent_dir(*args, **kwargs):
        _note("ensure_persistent_agent_dir", args, kwargs)
        return Path("/char/.claude")

    def _subscription(*args, **kwargs):
        _note("resolve_subscription_env", args, kwargs)
        return ("sub-char", {"SUB_ENV": "1"})

    def _permission_context(*args, **kwargs):
        _note("build_permission_context", args, kwargs)
        return "\n\nPERMISSIONS"

    def _client_context(self, *args, **kwargs):
        _note("build_client_context", args, kwargs)
        return "CLIENT"

    def _oto_env(**kwargs):
        _note("build_oto_env", (), kwargs)
        return {"OTO_STUB": "1"}

    real_dyn = dynamic_context.get_dynamic_contexts

    async def _dynamic(*args, **kwargs):
        _note("get_dynamic_contexts", args, kwargs)
        return await real_dyn(*args, **kwargs)

    import config
    from adapters.dashboard import DashboardAdapter
    from core.sandbox import oto_env, sandbox as _sb
    for mod in (cb, tcb, pcb, mc):
        monkeypatch.setattr(mod, "build_permission_context", _permission_context)
    monkeypatch.setattr(DashboardAdapter, "build_client_context", _client_context)
    monkeypatch.setattr(oto_env, "build_oto_env", _oto_env)
    monkeypatch.setattr(oto_env, "OTO_MULTI_VALUE_ENVS", {"OTO_STUB_ROOTS": ":"})
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _mcp_build)
    monkeypatch.setattr(config, "build_agent_prompt", _prompt)
    monkeypatch.setattr(_sb, "ensure_persistent_agent_dir", _persistent_dir)
    monkeypatch.setattr(pcb, "ensure_persistent_agent_dir", _persistent_dir)
    for mod in (cb, tcb, pcb, mc):
        monkeypatch.setattr(mod.subscription_pool, "resolve_subscription_env", _subscription)
    monkeypatch.setattr(dynamic_context, "get_dynamic_contexts", _dynamic)
    rec.tmp_path = tmp_path
    return rec


def run(coro):
    return asyncio.run(coro)


def outcome(rec, build) -> dict:
    """Run one build; the golden value: the config (or the refusal) and every
    recorded collaborator call."""
    rec.calls.clear()
    try:
        cfg = run(build())
    except Exception as exc:  # noqa: BLE001  # a refusal is part of the behaviour the golden pins
        return {"raised": type(exc).__name__, "calls": normalize(rec.calls)}
    return {"config": normalize(cfg), "calls": normalize(rec.calls)}


def _is_default(f, current) -> bool:
    if f.default is not dataclasses.MISSING:
        return current == f.default
    if f.default_factory is not dataclasses.MISSING:
        return current == f.default_factory()
    return False


_JWT = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def normalize(value):
    """Plain JSON values with the run's volatile parts named: the test agents
    root, the session token, the token's mint time, the rows' stamps. A
    dataclass shows only its fields that differ from their defaults, so a
    field another lane adds with a default changes no golden."""
    import config
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {f.name: normalize(getattr(value, f.name)) for f in dataclasses.fields(value)
               if not _is_default(f, getattr(value, f.name))}
        if "token_minted_at" in out:
            out["token_minted_at"] = "<now>"
        return out
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((normalize(v) for v in value), key=repr)
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if _JWT.match(value) and len(value) > 60:
            return SESSION_TOKEN
        if _STAMP.match(value):
            return "<stamp>"
        return value.replace(str(config.AGENTS_DIR), "<agents>")
    return value
