"""The engine descriptor's contract (engine-contract lane, phase 2).

Registration-time invariants every registered layer must satisfy, so a
fourth engine that declares itself wrong fails CI instead of the field:
the declared default model is one the layer serves; the three flags that
shadow ABC default methods agree with the methods; the pin keys are the
frozen satellite vocabulary; ``to_dict()`` carries the four groups.
"""

import sys

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.events import tool_roles
from core.execution_layer import (
    AUTH_TYPES, PERMISSION_MODES, PROVIDER_KINDS, WINDOW_ROLES, ExecutionLayer, LayerCapabilities,
)
from core.session.session_manager import account_label_for, get_all_layers


# The satellite reads the pinned CLI versions under exactly these keys
# (satellite/host/cli_versions.py _PIN_KEYS, fail-open on anything else).
_FROZEN_PIN_KEYS = {"claude_code", "codex"}


def test_declared_default_model_is_one_the_layer_serves():
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        served = {m["value"] for m in caps.models if m.get("value")}
        assert caps.model_policy.default_model in served, (
            f"{path} declares default_model={caps.model_policy.default_model!r} "
            f"but its models are {sorted(served)}"
        )


def test_method_shadowing_flags_agree_with_the_methods():
    # supports_steer / supports_compact / supports_interrupt_for_queued exist
    # as DATA because the remote layer must know without calling the local
    # method; this binds each flag to the override so they cannot drift.
    # Scoped to the registry: the remote layer (not registered — a placement,
    # not an engine) overrides all three as forwarders.
    for path, layer in get_all_layers().items():
        b = layer.capabilities.behaviour
        cls = type(layer)
        assert b.supports_steer == (cls.steer is not ExecutionLayer.steer), path
        assert b.supports_compact == (cls.compact is not ExecutionLayer.compact), path
        assert b.supports_interrupt_for_queued == (
            cls.interrupt_for_queued is not ExecutionLayer.interrupt_for_queued
        ), path


def test_pin_keys_are_the_frozen_satellite_vocabulary():
    for path, layer in get_all_layers().items():
        rt = layer.capabilities.runtime
        if rt.binary:
            assert rt.pin_key in _FROZEN_PIN_KEYS, (path, rt.pin_key)
            assert layer.pinned_cli_version(), path
        else:
            assert rt.pin_key == "" and layer.pinned_cli_version() == "", path


def test_to_dict_carries_the_groups_and_keeps_the_flat_contract():
    for layer in get_all_layers().values():
        d = layer.capabilities.to_dict()
        # The nineteen flat fields are the public contract — still there.
        for key in ("name", "display_name", "supports_plan_mode", "models",
                    "effort_levels", "mcp_config_format", "providers"):
            assert key in d
        for group in ("identity", "runtime", "behaviour", "model_policy", "auth", "usage"):
            assert isinstance(d[group], dict), group
        assert d["identity"]["short_name"] == layer.engine_name
        assert d["auth"]["auth_types"] == list(layer.capabilities.auth.auth_types)
        assert [w["key"] for w in d["usage"]["windows"]] == [
            s.key for s in layer.capabilities.usage.windows]


def test_descriptor_built_with_flat_fields_alone_still_constructs():
    # The remote layer's synthetic descriptor (until phase 4) passes only the
    # flat fields; every group must default.
    caps = LayerCapabilities(name="remote", display_name="Remote Execution")
    assert caps.runtime.has_os_process is False
    assert caps.model_policy.default_model == ""
    assert caps.to_dict()["behaviour"]["supports_steer"] is False


def test_engine_name_is_the_declared_short_name():
    names = {l.engine_name for l in get_all_layers().values()}
    assert names == {"claude", "codex", "direct"}


# --- auth (phase 3a) ---------------------------------------------------------

def test_auth_types_are_the_storage_vocabulary():
    # ``execution_layer_subscriptions.auth_type`` takes exactly these values;
    # an engine declares the subset its rows may carry, and declares SOME.
    for path, layer in get_all_layers().items():
        declared = layer.capabilities.auth.auth_types
        assert declared, path
        assert set(declared) <= set(AUTH_TYPES), (path, declared)
        assert len(set(declared)) == len(declared), (path, declared)


def test_an_engine_with_a_login_names_its_vendor_and_account():
    # The OAuth routes bind to an engine by its vendor, and every message
    # about an OAuth account names the product ("Claude", "ChatGPT").
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        if "oauth" in caps.auth.auth_types:
            assert caps.identity.vendor_id, path
            assert caps.identity.account_label, path


# The two login SHAPES the dashboard renders (engine-contract phase 5): a
# popup whose page shows a code the user pastes back, or a verification URL
# plus a one-time code the page polls on. The value names the UI, not the
# grant; the vendor routes stay bound by vendor_id.
_OAUTH_FLOWS = {"code_paste", "device_code"}


def test_oauth_flow_is_declared_exactly_when_the_engine_has_a_login():
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        if "oauth" in caps.auth.auth_types:
            assert caps.auth.oauth_flow in _OAUTH_FLOWS, (path, caps.auth.oauth_flow)
        else:
            assert caps.auth.oauth_flow == "", (path, caps.auth.oauth_flow)
        assert caps.to_dict()["auth"]["oauth_flow"] == caps.auth.oauth_flow


# --- the credential file (phase 3c-i) ---------------------------------------

# The two rows a released satellite clamps on
# (satellite/sessions/session_manager.py credentials_update: kind ∈ {claude,
# codex}, dir suffix and filename derived from kind). A fourth engine with a
# credential file fails HERE by design until the satellite learns its row.
# The start-payload key that carries the file is the engine's own wire
# vocabulary (its remote adapter writes it, its satellite session classes
# read it) — pinned by the remote-adapter contract test, not here.
_FROZEN_CREDENTIAL_FILES = {
    ("claude", ".claude", ".credentials.json"),
    ("codex", ".codex", "auth.json"),
}


def test_credential_file_is_one_of_the_frozen_rows_or_none():
    for path, layer in get_all_layers().items():
        spec = layer.capabilities.auth.credential_file
        if spec is None:
            continue
        row = (spec.wire_kind, spec.dirname, spec.filename)
        assert row in _FROZEN_CREDENTIAL_FILES, (path, row)


def test_config_dir_agrees_with_the_credential_dir_where_both_exist():
    # Two facts that coincide for both CLI engines — the sandbox config dir
    # is a runtime fact, the credential dir is the satellite clamp's — and
    # neither reads the other; a fourth engine that splits them fails here.
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        spec = caps.auth.credential_file
        if spec is not None and caps.runtime.config_dir_name:
            assert caps.runtime.config_dir_name == spec.dirname, path


def test_remote_runtime_facts_follow_supports_remote_execution():
    # An engine that runs on a satellite has a proxy-side event queue to
    # size; one that never does declares none. A self-waking engine has a
    # process to wake.
    for path, layer in get_all_layers().items():
        rt = layer.capabilities.runtime
        assert (rt.event_queue_depth > 0) == rt.supports_remote_execution, path
        if rt.self_wakes:
            assert rt.has_os_process, path
        if rt.interactive_submit_backstop:
            assert rt.supports_interactive_pty, path


def test_the_remote_adapter_matches_supports_remote_execution():
    # An engine that runs on a satellite has a RemoteEngineAdapter (its
    # satellite-protocol half); one that never does has none. The adapter's
    # private env keys are engine-PRIVATE names (no two engines share one),
    # and its soft-interrupt frame is a satellite frame name.
    from core.execution_layer import RemoteEngineAdapter
    seen_private: dict[str, str] = {}
    for path, layer in get_all_layers().items():
        adapter = layer.remote_adapter()
        assert (adapter is not None) == layer.capabilities.runtime.supports_remote_execution, path
        if adapter is None:
            continue
        assert isinstance(adapter, RemoteEngineAdapter), path
        assert layer.remote_adapter() is adapter, path          # one per layer
        assert adapter.soft_interrupt_frame(), path
        for key in adapter.private_env_keys:
            assert key not in seen_private, (path, key, seen_private[key])
            seen_private[key] = path


def test_the_interactive_transcript_tailer_matches_the_declaration():
    # An engine with a TUI persists that TUI's transcript; one without has
    # no tailer to hand the interactive session.
    for path, layer in get_all_layers().items():
        has_tui = layer.capabilities.runtime.supports_interactive_pty
        tailer = layer.transcript_tailer()
        assert (tailer is not None) == has_tui, path
        if tailer is not None:
            assert callable(getattr(tailer, "resolve_and_tail", None)), path
            assert callable(getattr(tailer, "forget", None)), path


def test_the_credential_adapter_matches_the_declaration():
    # Every registered engine maps a subscription to its env (the default
    # RAISES, so an engine that forgets cannot spawn with no credentials);
    # an engine that declares a credential file produces and consumes it,
    # one that declares none does neither.
    for path, layer in get_all_layers().items():
        cls = type(layer)
        assert cls.subscription_env is not ExecutionLayer.subscription_env, path
        has_file = layer.capabilities.auth.credential_file is not None
        assert has_file == (
            cls.credential_file_payload is not ExecutionLayer.credential_file_payload), path
        assert has_file == (
            cls.credential_file_from_env is not ExecutionLayer.credential_file_from_env), path


# --- usage (phase 3b) --------------------------------------------------------

def test_declared_windows_have_distinct_keys_and_one_quota_window():
    # The routing, the day cap and the alerts key off THE quota window; a
    # vendor with two would need the readers to choose, and none has one.
    for path, layer in get_all_layers().items():
        specs = layer.capabilities.usage.windows
        keys = [s.key for s in specs]
        assert len(set(keys)) == len(keys), (path, keys)
        for s in specs:
            assert s.role in WINDOW_ROLES, (path, s)
            assert s.length_s > 0 and s.label, (path, s)
        if specs:
            assert [s.role for s in specs].count("quota") == 1, (path, specs)


def test_an_engine_without_a_login_declares_no_windows():
    # A vendor reports usage windows on a LOGIN; keys are metered, not capped.
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        if "oauth" not in caps.auth.auth_types:
            assert caps.usage.windows == (), path


def test_the_login_and_usage_adapters_match_the_declarations():
    # An engine that takes a login refreshes it; an engine that declares
    # windows asks its vendor for them, parses them, and records its
    # in-band events — and one that declares neither overrides none of it.
    for path, layer in get_all_layers().items():
        cls = type(layer)
        caps = layer.capabilities
        has_login = "oauth" in caps.auth.auth_types
        assert has_login == (cls.refresh_oauth is not ExecutionLayer.refresh_oauth), path
        has_windows = bool(caps.usage.windows)
        for name in ("usage_request", "parse_usage", "record_usage_event"):
            assert has_windows == (getattr(cls, name) is not getattr(ExecutionLayer, name)), (path, name)


def test_account_label_is_tolerant_of_an_unknown_engine():
    assert account_label_for("claude-code-cli") == "Claude"
    assert account_label_for("codex-cli") == "ChatGPT"
    assert account_label_for("direct-llm", "AI engine") == "AI engine"   # no account product
    assert account_label_for("acme-cli", "AI engine") == "AI engine"     # never raises


# ---------------------------------------------------------------------------
# Effort per provider (engine-contract phase 5e): every providers[] entry says
# which platform levels its API takes and which of them a model row's flag
# gates, so the dashboard's effort ladder and the admin xhigh checkbox read a
# declaration instead of a provider name.
# ---------------------------------------------------------------------------

_PROVIDER_KEYS = {"id", "label", "requires_key", "effort_scale", "effort_per_model",
                  "kind", "relay_path", "api_path"}


def _sub_sequence(part: list[str], whole: list[str]) -> bool:
    it = iter(whole)
    return all(any(w == p for w in it) for p in part)


def test_every_provider_entry_declares_its_effort_ladder():
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        assert caps.providers, f"{path} declares no providers (at least its vendor)"
        for entry in caps.providers:
            assert set(entry) == _PROVIDER_KEYS, (path, entry)
            scale, per_model = entry["effort_scale"], entry["effort_per_model"]
            assert scale, (path, entry["id"])
            assert _sub_sequence(scale, caps.effort_levels), (path, entry["id"], scale)
            assert set(per_model) <= set(scale), (path, entry["id"], per_model)


def test_every_served_model_names_a_declared_provider():
    # The pool filters rows by the model's provider on a multi-provider
    # engine, and the admin card groups rows by it: a served row must resolve
    # to a provider the engine declares (this is what makes Claude declare
    # Anthropic).
    import config as app_config
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        declared = {p["id"] for p in caps.providers}
        for m in caps.models:
            if not m.get("value"):
                continue
            assert m.get("provider") in declared, (path, m["value"], m.get("provider"))
            assert app_config.get_model_provider(m["value"], layer=path) in declared, (path, m["value"])


def _entries(path: str) -> dict[str, dict]:
    return {p["id"]: p for p in get_all_layers()[path].capabilities.providers}


def test_the_effort_scales_agree_with_the_adapters():
    # Each declared scale is what the engine's effort map sends unchanged on
    # a model that takes it all — computed from the map where one exists.
    from core.layers.codex.helpers import map_effort_to_codex
    from core.layers.providers.openai_adapter import _EFFORT_TO_OPENAI

    codex = get_all_layers()["codex-cli"].capabilities
    assert "minimal" not in codex.effort_levels   # declared once, never mapped or offered
    codex_entries = _entries("codex-cli")
    assert codex_entries["openai"]["effort_scale"] == [
        lvl for lvl in codex.effort_levels if map_effort_to_codex(lvl, "gpt-6-astra") == lvl]
    for pid in ("ollama", "openai_compatible"):
        if pid in codex_entries:
            assert codex_entries[pid]["effort_scale"] == [
                lvl for lvl in codex.effort_levels if map_effort_to_codex(lvl, "qwen3:latest") == lvl]

    direct = get_all_layers()["direct-llm"].capabilities
    direct_entries = _entries("direct-llm")
    openai_scale = [lvl for lvl in direct.effort_levels if _EFFORT_TO_OPENAI.get(lvl) == lvl]
    for pid in ("openai", "ollama", "openai_compatible"):
        if pid in direct_entries:
            assert direct_entries[pid]["effort_scale"] == openai_scale, pid
    # Groq's API documents low / medium / high (gpt-oss); the adapter still
    # sends the inherited OpenAI map above that — the declaration states what
    # the provider takes, and it is a strict prefix of the map's identity set.
    groq_scale = direct_entries["groq"]["effort_scale"]
    assert groq_scale == openai_scale[:len(groq_scale)] and len(groq_scale) < len(openai_scale)
    # Anthropic's rewrite is inline (anthropic_adapter.stream_response,
    # cli/session._build_persistent_cmd): every level but ultra goes through
    # as itself, xhigh only when the row supports it.
    for path in ("direct-llm", "claude-code-cli"):
        anthropic = _entries(path)["anthropic"]
        assert anthropic["effort_scale"] == ["low", "medium", "high", "xhigh", "max"], path
        assert anthropic["effort_per_model"] == ["xhigh"], path


def test_effort_per_model_names_a_flag_the_rows_carry():
    # xhigh is gated by supports_xhigh (read by cli/session.py, cli/remote.py
    # and anthropic_adapter.py); ultra by supports_ultra, which
    # get_layer_models emits only on an engine that offers it. Any other
    # per-model level would have no row flag to read.
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        for entry in caps.providers:
            for lvl in entry["effort_per_model"]:
                assert lvl in ("xhigh", "ultra"), (path, entry["id"], lvl)
                if lvl == "ultra":
                    assert any(m.get("supports_ultra") for m in caps.models), path


# ---------------------------------------------------------------------------
# The config dir, the interactive resume, the binary and the pin (phase 6):
# the last three places shared code branched on an engine id became layer
# methods; each is bound to the declaration below.
# ---------------------------------------------------------------------------

def test_the_config_dir_builder_matches_the_declaration(monkeypatch):
    # Every registered engine prepares the config dir a local session
    # mounts (the default raises); an engine with a config_dir_name returns
    # a dir of that name, one without still returns SOME dir (Direct LLM
    # keeps its plans and MCP config home in the .claude tree).
    monkeypatch.setattr("core.sandbox.skills_materializer.materialize_skills_for_sandbox",
                        lambda *a, **k: None)
    for path, layer in get_all_layers().items():
        cls = type(layer)
        assert cls.prepare_config_dir is not ExecutionLayer.prepare_config_dir, path
        d = layer.prepare_config_dir("scratch-agent", username="", scope="agent")
        assert d.is_dir(), (path, d)
        want = layer.capabilities.runtime.config_dir_name
        if want:
            assert d.name == want, (path, d.name, want)
        else:
            assert d.name.startswith("."), (path, d.name)


def test_the_binary_path_and_the_pin_follow_the_binary():
    # cli_binary_path() names the executable this host runs exactly when the
    # descriptor declares a binary; pinned_cli_version() likewise (the
    # frozen-pin-key test above already binds the version to the pin key).
    for path, layer in get_all_layers().items():
        has_binary = bool(layer.capabilities.runtime.binary)
        assert bool(layer.cli_binary_path()) == has_binary, path
        assert bool(layer.pinned_cli_version()) == has_binary, path


def test_the_pin_key_names_the_versions_md_row():
    # The env name the bootstrap headers export and the row VERSIONS.md
    # carries are the pin key upper-cased plus _VERSION — CLAUDE_CODE_VERSION
    # / CODEX_VERSION — so a fourth engine's pin reaches the installers with
    # no edit to the headers.
    import config as app_config
    for path, layer in get_all_layers().items():
        pin_key = layer.capabilities.runtime.pin_key
        if not pin_key:
            continue
        row = app_config._read_pinned_version(f"{pin_key.upper()}_VERSION")
        assert row and row == layer.pinned_cli_version(), (path, pin_key, row)


def test_the_installer_defaults_mirror_the_pins():
    # The Docker image installs the CLIs from the installer's own defaults
    # (proxy/Dockerfile runs scripts/install-baseline-tools.sh before
    # VERSIONS.md is copied in), so every image's CLI version is the
    # default baked into the script, not the VERSIONS.md row. Both scripts'
    # defaults must equal the row of each engine's pin key, or an image
    # ships one CLI version while the proxy and the satellites pin another.
    import re
    from tests._paths import REPO_ROOT
    sh = (REPO_ROOT / "scripts" / "install-baseline-tools.sh").read_text(encoding="utf-8")
    ps1 = (REPO_ROOT / "scripts" / "install-baseline-tools.ps1").read_text(encoding="utf-8")
    ps1_names = {"claude_code": "ClaudeCodeVersion", "codex": "CodexVersion"}
    for path, layer in get_all_layers().items():
        pin_key = layer.capabilities.runtime.pin_key
        if not pin_key:
            continue
        key = f"{pin_key.upper()}_VERSION"
        want = layer.pinned_cli_version()
        assert want, (path, key)
        sh_hits = re.findall(rf'^{key}="\$\{{{key}:-([^}}]+)\}}"$', sh, re.MULTILINE)
        assert sh_hits == [want], (path, key, sh_hits, want)
        var = ps1_names[pin_key]
        ps1_hits = re.findall(
            rf"^\s*\${var}\s*=\s*if \(\$env:{key}\) \{{ \$env:{key} \}} else \{{ '([^']+)' \}}$",
            ps1, re.MULTILINE,
        )
        assert ps1_hits == [want], (path, key, ps1_hits, want)


def test_the_installer_toolchain_defaults_mirror_the_pins():
    # The Docker image runs scripts/install-baseline-tools.sh before
    # VERSIONS.md is copied in, so uv, pnpm, Bun and sympy in every image are
    # the script's own defaults, as on any host run without the env
    # overrides. Only the Node default's major is used (the NodeSource line);
    # the whole value is held equal so "keep in sync" stays true.
    import re
    import config as app_config
    from tests._paths import REPO_ROOT
    sh = (REPO_ROOT / "scripts" / "install-baseline-tools.sh").read_text(encoding="utf-8")
    ps1 = (REPO_ROOT / "scripts" / "install-baseline-tools.ps1").read_text(encoding="utf-8")
    sh_vars = {"UV_VERSION": "uv_ver", "PNPM_VERSION": "pnpm_ver", "BUN_VERSION": "bun_ver",
               "SYMPY_VERSION": "sympy_ver", "NODE_VERSION": "node_ver"}
    for key, var in sh_vars.items():
        want = app_config._read_pinned_version(key)
        assert want, key
        hits = re.findall(rf'^\s*local {var}="\$\{{{key}:-([^}}]+)\}}"', sh, re.MULTILINE)
        assert hits == [want], (key, hits, want)
    for key, var in {"UV_VERSION": "UvVersion", "PNPM_VERSION": "PnpmVersion"}.items():
        want = app_config._read_pinned_version(key)
        hits = re.findall(
            rf"^\s*\${var}\s*=\s*if \(\$env:{key}\) \{{ \$env:{key} \}} else \{{ '([^']+)' \}}$",
            ps1, re.MULTILINE)
        assert hits == [want], (key, hits, want)
    sympy = app_config._read_pinned_version("SYMPY_VERSION")
    assert re.findall(r"sympy==([0-9][0-9.]*)", ps1) == [sympy]


def test_the_image_defaults_mirror_the_pins():
    # A bare `docker build` or `docker compose up` (no scripts/versions.sh in
    # front) takes the Dockerfile ARG and compose literal defaults, and T1
    # builds the file-tools sidecar that way, so each default equals its
    # VERSIONS.md row; so do CI's Postgres service images and dev-setup's
    # fallbacks. A workflow missing from this checkout is not read.
    import re
    import config as app_config
    from tests._paths import REPO_ROOT
    python_image = app_config._read_pinned_version("PYTHON_IMAGE")
    node_image = app_config._read_pinned_version("NODE_IMAGE")
    postgres_image = app_config._read_pinned_version("POSTGRES_IMAGE")
    assert python_image and node_image and postgres_image
    for rel in ("proxy/Dockerfile", "phone/Dockerfile", "mcps/custom/file-tools-mcp/Dockerfile"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert re.findall(r"^ARG PYTHON_IMAGE=(\S+)$", text, re.MULTILINE) == [python_image], rel
    proxy = (REPO_ROOT / "proxy" / "Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r"^ARG NODE_IMAGE=(\S+)$", proxy, re.MULTILINE) == [node_image]
    setup = (REPO_ROOT / "scripts" / "dev-setup.sh").read_text(encoding="utf-8")
    literals = {"docker-compose.yml": 2, "docker-compose.t1.yml": 1, "scripts/dev-setup.sh": 1}
    for rel, count in literals.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert re.findall(r"\$\{POSTGRES_IMAGE:-([^}]+)\}", text) == [postgres_image] * count, rel
    ci = [tag for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
          for tag in re.findall(r"^\s*image:\s*(postgres:\S+)$", path.read_text(encoding="utf-8"), re.MULTILINE)]
    assert ci and set(ci) == {postgres_image}, ci
    for key in ("PYTHON_VERSION", "NODE_VERSION"):
        hits = re.findall(rf'{key}="\$\{{{key}:-([^}}]+)\}}"', setup)
        assert hits == [app_config._read_pinned_version(key)], (key, hits)


def test_the_agents_column_default_is_the_platform_default():
    # DDL cannot read a constant: the agents.execution_path default is typed
    # in storage/agents/schema.py and pinned here to DEFAULT_EXECUTION_PATH.
    from core.execution_layer import DEFAULT_EXECUTION_PATH
    ddl = (_PROXY_DIR / "storage" / "agents" / "schema.py").read_text(encoding="utf-8")
    assert f"execution_path TEXT NOT NULL DEFAULT '{DEFAULT_EXECUTION_PATH}'" in ddl


def _cfg(**over):
    from core.execution_layer import AgentConfig
    base = dict(agent_name="a", interactive=True, execution_target="local",
                sandbox_host_claude_dir="/agents/a/users/u/.codex", resume=False,
                resume_handle="")
    base.update(over)
    return AgentConfig(**base)


def test_resumes_interactive_answers_the_flag_unless_the_engine_resumes_by_a_handle(monkeypatch):
    layers = get_all_layers()
    # Every engine: a non-interactive config keeps the warmup's flag.
    for path, layer in layers.items():
        for flag in (True, False):
            assert layer.resumes_interactive(_cfg(interactive=False, resume=flag)) is flag, path
    # An engine that resumes its session id keeps the flag when interactive too.
    claude = layers["claude-code-cli"]
    assert claude.resumes_interactive(_cfg(resume=True)) is True
    assert claude.resumes_interactive(_cfg(resume=False)) is False
    # Codex resumes by the thread id: on a satellite the handle decides;
    # locally the handle AND its rollout on disk.
    codex = layers["codex-cli"]
    assert codex.resumes_interactive(_cfg(execution_target="mach-1", resume_handle="tid")) is True
    assert codex.resumes_interactive(_cfg(execution_target="mach-1", resume_handle="", resume=True)) is False
    from core.session import codex_rollout_tailer
    seen = []
    monkeypatch.setattr(codex_rollout_tailer, "rollout_exists",
                        lambda home, tid: seen.append((home, tid)) or True)
    assert codex.resumes_interactive(_cfg(resume_handle="tid")) is True
    assert seen == [("/agents/a/users/u/.codex", "tid")]
    monkeypatch.setattr(codex_rollout_tailer, "rollout_exists", lambda home, tid: False)
    assert codex.resumes_interactive(_cfg(resume_handle="tid", resume=True)) is False
    assert codex.resumes_interactive(_cfg(resume_handle="", resume=True)) is False


# --- core-seams phase 2: the tool vocabulary, the provider kinds, the modes --

def test_every_declared_native_tool_name_maps_into_its_role():
    """``behaviour.tools`` is the engine's native vocabulary by role; each name
    must reach a canonical name (``core/events/tool_roles``) of that role
    through the layer's ``canonical_tool_name`` — the binding that lets
    generic code ask a role and never compare a name."""
    for path, layer in get_all_layers().items():
        tools = layer.capabilities.behaviour.tools
        assert tools, f"{path} declares no tools"
        for role, natives in tools.items():
            assert role in tool_roles.ROLES, (path, role)
            assert natives, (path, role)
            for native in natives:
                canonical = layer.canonical_tool_name("", native)
                assert tool_roles.role_of(canonical) == role, (path, role, native, canonical)


def test_question_tool_holds_turn_only_with_a_question_tool():
    for path, layer in get_all_layers().items():
        b = layer.capabilities.behaviour
        if b.question_tool_holds_turn:
            assert b.tools.get(tool_roles.QUESTION), path


def test_provider_entries_declare_their_kind_and_paths():
    """``kind`` is one of the platform's; a relay path only on a vendor of an
    engine that takes ``relay`` rows; one id declared by two engines agrees
    on its kind, its api path and (where both name one) its relay path."""
    seen: dict[str, dict] = {}
    for path, layer in get_all_layers().items():
        caps = layer.capabilities
        for entry in caps.providers:
            assert entry["kind"] in PROVIDER_KINDS, (path, entry["id"], entry["kind"])
            if entry["relay_path"]:
                assert entry["kind"] == "vendor", (path, entry["id"])
                assert "relay" in caps.auth.auth_types, (path, entry["id"])
                assert entry["relay_path"].startswith("/"), (path, entry["id"])
            if entry["kind"] == "local":
                assert entry["requires_key"] is False, (path, entry["id"])
            prior = seen.setdefault(entry["id"], entry)
            assert prior["kind"] == entry["kind"], (entry["id"], path)
            assert prior["api_path"] == entry["api_path"], (entry["id"], path)
            if prior["relay_path"] and entry["relay_path"]:
                assert prior["relay_path"] == entry["relay_path"], (entry["id"], path)


def test_permission_modes_are_the_platforms():
    for path, layer in get_all_layers().items():
        modes = layer.capabilities.permission_modes
        assert modes, path
        assert set(modes) <= set(PERMISSION_MODES), (path, modes)


def test_installed_name_follows_the_binary():
    for path, layer in get_all_layers().items():
        rt = layer.capabilities.runtime
        assert bool(rt.installed_name) == bool(rt.binary), (path, rt.installed_name, rt.binary)


def test_to_dict_carries_the_phase_2_facts():
    for path, layer in get_all_layers().items():
        d = layer.capabilities.to_dict()
        assert "installed_name" in d["runtime"], path
        assert isinstance(d["behaviour"]["tools"], dict), path
        assert all(isinstance(v, list) for v in d["behaviour"]["tools"].values()), path
        assert "question_tool_holds_turn" in d["behaviour"], path
        for entry in d["providers"]:
            assert {"kind", "relay_path", "api_path"} <= set(entry), (path, entry)
