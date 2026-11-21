"""Characterization of the MCP registry over a fixture catalog: the manifest
store, the session filter, the egress, the agent's skills, the server entries
and the session config with its TOML writers, read through the public
functions against real rows. A drift is a behaviour change, and the commit that
records it says why. Only the boundaries are faked, where they live: the
network at ``socket``, the hosted relay at its client, the deployment mode at
``core.config.deployment``. The catalog is this file's own, never the shipped
``mcps/`` tree."""
import dataclasses
import itertools
import json
import socket
from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict

import config
from auth import oauth_providers
from core import placement as place
from core.config import deployment
from core.credentials import credential_exit
from services.billing import relay_client
from services.mcp import mcp_manifest_store, mcp_registry
from services.mcp.outside_picks import OutsidePicks, ToolPick
from services.media import audio_service
from services.oauth import oauth_account_store
from storage.agents import agent_store
from storage.identity import (
    bearer_allowlist,
    credential_store,
    db_users,
    service_account_set,
)
from storage.mcp import mcp_store
from storage.pg import get_conn
from storage.remote_store import BrowserTargetSettings
from tests.fixtures.characterization.golden_master import check_golden
from tests.fixtures.loop_builder_rig import normalize

AGENT = "char-reg"
_STDIO = {"runtime": "python", "transport": "stdio", "command": "venv/bin/python", "args": ["server.py"]}
_DOCKER = {"runtime": "docker", "transport": "http", "port": 8932,
           "url_template": "http://${docker_mcp_host}:${port}", "service_name": "char-lookalike"}
_VENDOR = {"runtime": "none", "transport": "streamable_http", "url_template": "https://vendor.example.test/mcp"}
_KEY_FIELD = {"key": "CHAR_KEY", "label": "Key", "input_type": "password"}
_OAUTH = {
    "provider_id": "char-oauth", "flows": ["authorization_code"], "bearer_required": True,
    "authorization_url": "https://auth.example.test/authorize", "token_url": "https://auth.example.test/token",
    "proposed_hosts": ["vendor.example.test"], "base_scopes": ["base", "mail.read"],
    "services": [{"key": "mail", "label": "Mail", "description": "d", "scopes": ["mail.read", "mail.send"]},
                 {"key": "cal", "label": "Calendar", "description": "d", "scopes": ["cal.read"]}],
}


def _m(label: str, category: str = "custom", **over) -> dict:
    return {"label": label, "description": f"{label} tools. An operational note stays out.", "version": "1.0.0",
            "category": category, "server": _STDIO, "credentials": {"type": "none"}, **over}


def _target(key: str, source: str = "config", **kw) -> dict:
    return {"source": source, "host_key": key, **kw}


def _skill(skill_id: str, **kw) -> dict:
    return {"id": skill_id, "file": f"skills/{skill_id}.md", "description": f"About {skill_id}.", **kw}


def _keyed(host: str) -> dict:
    return {"server": {**_VENDOR, "url_template": f"https://{host}/mcp"},
            "credentials": {"type": "infra", "label": "Keyed", "fields": [_KEY_FIELD], "api_key_header": {
                "name": "X-Char-Key", "value_from": "CHAR_KEY", "proposed_hosts": [host]}}}


# The tree a manifest sits in decides its scan order and whether it ships with the platform.
_CUSTOM = {
    "char-tools": _m(
        "Char Tools",
        server={**_STDIO, "args": ["server.py", "--mode", "fast", "${agent_name}", 'quote"and\\back', "line\nbreak"],
                "tool_timeout_sec": 120},
        env={"CHAR_STATIC": "1", "CHAR_DIR": "${mcp_dir}/data", "CHAR_ROOT": "${platform_root}/x"},
        agent_env={"CHAR_AGENT": "${agent_name}", "CHAR_CHAT": "${session.chat_id}", "CHAR_OWNER": "${session.task_owner}",
                   "CHAR_USER": "${session.task_username}", "CHAR_SCOPE": "${session.task_scope}",
                   "CHAR_ASK": "${session.call_ask_back}", "CHAR_PROXY": "${proxy_url}",
                   "CHAR_REF": "${config:char-homelab:CHAR_HL_URL}", "CHAR_MASTER": "${proxy_api_key}"},
        config=[{"key": "CHAR_MODE", "default": "fast"}, {"key": "CHAR_LIMIT", "default": 5}, {"key": "CHAR_ON", "default": True},
                {"key": "CHAR_OFF", "default": False}, {"key": "CHAR_EMPTY"}, {"key": "_CHAR_HIDDEN", "default": "x"}],
        tool_filter={"arg_name": "--enabled-tools"},
        path_env={"CHAR_TOKENS": {"role": "credentials_dir", "subpath": "char-tokens"},
                  "CHAR_ROOTS": {"values": [{"role": "user_root"}, {"role": "credentials_dir", "subpath": "char-multi"}]}},
        agent_context=[{"template": "kept"}, {"template": "sidecar", "builder": {"tool": "mcp__char-http__peek"}},
                       {"template": "stdio", "builder": {"tool": "mcp__char-tools__self"}},
                       {"template": "unknown", "builder": {"tool": "mcp__nobody__x"}}],
        skills=[_skill("char-always", loading="always"), _skill("char-guide"), _skill("char-zeta"),
                _skill("char-phone-off", default_exclude_from=["phone"], loading="always"),
                _skill("char-row-clears", default_exclude_from=["phone"]), _skill("char-row-adds"), _skill("char-row-off"),
                _skill("char-external-off", default_exclude_from=["external"]),
                _skill("char-shared-off", default_exclude_from=["shared_chat"]),
                _skill("char-owner", audience="owner"), _skill("char-editor", audience="editor", loading="always"),
                _skill("char-missing", loading="always")],
    ),
    **{f"char-no-{ctx.replace('_', '-')}": _m(f"No {ctx}", exclude_from=[ctx],
                                              skills=[_skill(f"char-no-{ctx.replace('_', '-')}-guide")])
       for ctx in ("phone", "task", "meeting", "terminal", "external", "shared_chat")},
    "char-no-many": _m("No Many", exclude_from=["external", "phone", "task", "meeting"]),
    "delegation-mcp": _m("Delegation", skills=[_skill("delegation-guide")]),
    "char-http": _m("Char Sidecar", server={**_DOCKER, "proxy_callbacks": True, "service_name": "char-http-svc"},
                    skills=[_skill("char-http-guide")]),
    "char-browser": _m("Char Browser", server_name="local", placement="satellite_only", device_capability="browser",
                       requires_display=True, skills=[_skill("char-browser-guide")],
                       server={"runtime": "node", "transport": "stdio", "command": "node", "args": ["index.js"]},
                       env={"PLAYWRIGHT_MCP_BLOCKED_ORIGINS": "http://localhost:*;https://blocked.test",
                            "OTO_BROWSER_MODE": "spoofed"}),
    "char-satellite": _m("Char Satellite", placement="satellite_only"),
    "char-admin-only": _m("Char Admin Only", remote_policy="admin_paired_only"),
    "char-editors": _m("Char Editors", audience="editor", network_targets=[_target("CHAR_ED_HOST", port_default=2222)]),
    "char-homelab": _m("Char Homelab", network_targets=[_target("CHAR_HL_URL", port_default=9090),
                                                        _target("CHAR_HL_HOST", "infra_credential", port_default=8443)],
                       credentials={"type": "infra", "label": "Homelab", "fields": [
                           {"key": "CHAR_HL_TOKEN", "label": "Token", "input_type": "password"},
                           {"key": "CHAR_HL_HOST", "label": "Host"}]}),
    "char-hostself": _m("Host Self", network_targets=[_target("CHAR_HS_URL", port_default=9200)]),
    "char-loopback": _m("Loopback", network_targets=[_target("CHAR_LB_URL")]),
    "char-toggle-off": _m("Toggle Off", network_targets=[_target("CHAR_TG_URL")]),
    "char-default-off": _m("Default Off", network_access_default=False, network_targets=[_target("CHAR_TG_URL")]),
    "char-toggle-on": _m("Toggle On", network_access_default=False, network_targets=[_target("CHAR_TG_URL")]),
    "char-infra-missing": _m("Infra Missing", credentials={"type": "infra", "label": "Missing",
                                                           "fields": [{"key": "CHAR_MISSING", "label": "K"}]}),
    "char-peruser": _m("Per User", config=[{"key": "USER_SMTP", "default": "smtp.example.test"}],
                       network_targets=[_target("USER_HOST", "per_user_credential", port_default=993)],
                       credentials={"type": "per_user", "label": "Per User", "service_account": True, "fields": [
                           {"key": "USER_TOKEN", "label": "Token", "input_type": "password"},
                           {"key": "USER_HOST", "label": "Host", "required": False}]}),
    "char-gitish": _m("Gitish", credentials={"type": "per_user", "label": "Git Account", "oauth": {
                          **{k: _OAUTH[k] for k in ("flows", "authorization_url", "token_url")}, "provider_id": "char-git",
                          "env_injection": ["GH_TOKEN"], "mcp_env_injection": ["CHAR_GIT_TOKEN"],
                          "git_credential_helper": {"host": "git.example.test", "helper": "!char"},
                          "services": [_OAUTH["services"][0]]}}),
    "char-env-inst": _m("Env Instance", assignment_mode="explicit",
                        instances={"delivery": "env", "fields": [{"key": "INST_HOST"}, {"key": "INST_PORT"},
                                                                 {"key": "INST_KEY", "secret": True}]},
                        network_targets=[_target("INST_HOST", "instance", port_key="INST_PORT", port_default=443)]),
    "char-hosted": _m("Hosted Relay", assignment_mode="explicit",
                      instances={"delivery": "env", "fields": [{"key": "VENDOR_KEY", "secret": True}]},
                      hosted={"oauth_app": {"available": True, "default_mode": "self_managed"},
                              "api_key_relay": {"available": True, "relay_path": "/relay/char",
                                                "min_balance_to_enable_usd": 5,
                                                "billing_setup_url": "https://billing.example.test"}}),
    "char-cfg-inst": _m("Config Instance", assignment_mode="explicit",
                        server={**_STDIO, "args": ["server.py", "--config-file", "placeholder", "--verbose"]},
                        instances={"delivery": "config_file", "config_file_arg": "--config-file", "transform": "ssh_hosts",
                                   "fields": [{"key": "host"}, {"key": "key_name"}]}),
    "char-no-instance": _m("No Instance", assignment_mode="explicit",
                           instances={"delivery": "env", "fields": [{"key": "K"}]}),
    "char-no-file-instance": _m("No File Instance", assignment_mode="explicit",
                                instances={"delivery": "config_file", "fields": [{"key": "K"}]}),
    "char-platform": _m("Platform Attached", assignment_mode="platform"),
    "char-cap-hidden": _m("Hidden", requires_capability="no_such_capability"),
    "char-cli-token": _m("Names Token", env={"CHAR_BAD": "${OTO_CLI_TOKEN}"}),
    "char-core": _m("Core One", "core", skills=[_skill("char-core-guide", default_exclude_from=["task"])]),
    "char-core-explicit": _m("Core Explicit", "core", assignment_mode="explicit"),
}
_COMMUNITY = {
    "char-tools": _m("Shadowed by the custom tree", "community"),
    "char-sse": _m("Char SSE", "community", server={"runtime": "none", "transport": "sse",
                                                    "url_template": "https://sse.example.test/sse"}),
    "char-keyed": _m("Char Keyed", "community", **_keyed("api.example.test")),
    "char-unapproved": _m("Char Unapproved", "community", **_keyed("attacker.example.test")),
    "char-bearer": _m("Char Bearer", "community", server=_VENDOR,
                      credentials={"type": "per_user", "label": "Bearer Account", "oauth": _OAUTH}),
    "char-bearer-sidecar": _m("Bearer Sidecar", "community", server={**_DOCKER, "port": 8934, "service_name": "char-bs-svc"},
                              credentials={"type": "per_user", "label": "Bearer Sidecar", "oauth": _OAUTH}),
    "char-lookalike": _m("Lookalike", "community", server=_DOCKER),
    "char-shared-docker": _m("Shared Docker", "community",
                             server={**_DOCKER, "port": 8933, "service_name": "char-shared-svc",
                                     "url_template": "http://192.168.77.80:8933"}),
    "char-disabled": _m("Stays Disabled", "community"),
}
_SKILLS = {
    "char-context": _m("Char Context", "skill", server={"runtime": "none", "transport": "none"},
                       skills=[_skill("char-context-guide", loading="always")]),
}
CATALOG = {"custom": _CUSTOM, "community": _COMMUNITY, "skills": _SKILLS}
_SKILL_FILES_MISSING = {"char-missing"}


def _write_catalog(root, trees: dict) -> None:
    for tree, manifests in trees.items():
        for name, body in manifests.items():
            folder = root / tree / name
            (folder / "skills").mkdir(parents=True)
            (folder / "manifest.json").write_text(json.dumps({"name": name, **body}))
            for skill in body.get("skills", []):
                if skill["id"] not in _SKILL_FILES_MISSING:
                    (folder / skill["file"]).write_text(
                        f"---\nname: {skill['id']}\ndescription: indexed\n---\n\n# {skill['id']}\n\nBody.\n")


def install_catalog(monkeypatch, root, trees: dict = CATALOG) -> dict:
    """The fixture catalog as manifest.json files under a temp MCPs folder,
    loaded by the real scan. The ONE place that names the module holding
    ``_manifests``: the scan rebinds it, and the monkeypatch hands the
    process's own back at the end of the test."""
    _write_catalog(root, trees)
    monkeypatch.setattr(config, "MCPS_DIR", root)
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {})
    # A bundled tool's stand-in stamp carries the platform's version: pinned
    # here, so a release's version bump moves no golden.
    monkeypatch.setattr(config, "PINNED_OTODOCK_VERSION", "0.0.0")
    return mcp_registry.scan_manifests()


_DNS = {
    "lan-box.test": ["192.168.77.70"],
    "multi.test": ["192.168.77.71", "8.8.8.8", "169.254.169.254", "127.0.0.1"],
    "char-http-svc": ["10.204.0.3"],
    "char-lookalike": ["10.204.0.4"],
    "char-shared-svc": ["10.200.0.30"],
    "char-bs-svc": ["10.200.0.31"],
}
_HOST_SELF = "192.168.77.1"


def _fake_network(monkeypatch) -> None:
    """The fixture's names resolve and only the fixture's own address binds.
    The test database's host stays real: the pool may dial while a test runs."""
    real_lookup, real_socket = socket.getaddrinfo, socket.socket
    passes = {"localhost", "127.0.0.1", "::1", conninfo_to_dict(config.DATABASE_URL).get("host")}

    def _lookup(host, port, *args, **kw):
        if host in _DNS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in _DNS[host]]
        if host in passes:
            return real_lookup(host, port, *args, **kw)
        raise socket.gaierror(socket.EAI_NONAME, "not a name of the fixture")

    class _Socket(real_socket):
        def bind(self, address):
            if not isinstance(address, tuple):
                super().bind(address)
            elif address[0] != _HOST_SELF:
                raise OSError(99, "not an address of this host")

    monkeypatch.setattr(socket, "getaddrinfo", _lookup)
    monkeypatch.setattr(socket, "socket", _Socket)


def _set_username(sub: str, username: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        conn.commit()


def _token_file(username: str, provider_id: str, label: str = "main", **raw) -> None:
    folder = oauth_account_store.get_token_dir(username, provider_id=provider_id)
    (folder / f"{label}.json").write_text(json.dumps({"access_token": f"token-of-{username}", **raw}))


@pytest.fixture
def world(temp_db, monkeypatch, tmp_path):
    """The catalog, an agent holding every MCP, its people (an admin, a viewer
    and an editor) and the rows behind each seam."""
    _fake_network(monkeypatch)
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(relay_client, "is_available", lambda: False)
    # The credential exit's placeholders are random: numbered here in the
    # order the build mints them, so a golden holds them.
    minted = itertools.count(1)
    monkeypatch.setattr(credential_exit, "new_placeholder",
                        lambda: f"{credential_exit.PLACEHOLDER_PREFIX}{next(minted):032x}")
    (tmp_path / "mcps" / "community" / "char-bad-json").mkdir(parents=True)
    (tmp_path / "mcps" / "community" / "char-bad-json" / "manifest.json").write_text("{nope")
    (tmp_path / "mcps" / "community" / "char-no-manifest").mkdir()
    (tmp_path / "mcps" / "community" / "stray-file.txt").write_text("not a folder")
    found = install_catalog(monkeypatch, tmp_path / "mcps")

    agent_store.create_agent(AGENT, "Char Registry")
    for sub, username in (("user-admin", "ada"), ("user-viewer", "vic"), ("user-manager", "max")):
        _set_username(sub, username)
    db_users.add_user_agent("user-viewer", AGENT, "viewer", "user-admin")
    db_users.add_user_agent("user-manager", AGENT, "editor", "user-admin")
    mcp_store.set_manager_enabled_mcps(AGENT, sorted(found))
    for name in sorted(_COMMUNITY.keys() - {"char-disabled"}) + sorted(_SKILLS):
        mcp_store.set_mcp_enabled(name, True)
    for name, values in {
        "char-tools": {"CHAR_LIMIT": "9", "CHAR_SAVED": "saved", "_CHAR_STATE": "hidden"},
        "char-homelab": {"CHAR_HL_URL": "http://192.168.77.10:9100/metrics"},
        "char-editors": {"CHAR_ED_HOST": "192.168.77.20"},
        "char-hostself": {"CHAR_HS_URL": f"http://{_HOST_SELF}:9200"},
        "char-loopback": {"CHAR_LB_URL": "http://localhost:9300"},
        "char-toggle-off": {"CHAR_TG_URL": "192.168.77.11", "_network_access": "false"},
        "char-default-off": {"CHAR_TG_URL": "192.168.77.14"},
        "char-toggle-on": {"CHAR_TG_URL": "192.168.77.12", "_network_access": "true"},
    }.items():
        mcp_store.set_mcp_config_values(name, values)
    mcp_store.set_tool_filter_regex("char-tools", "^safe_")
    mcp_store.set_agent_skill(AGENT, "char-row-clears", enabled=True, exclude_from=[])
    mcp_store.set_agent_skill(AGENT, "char-row-adds", enabled=True, exclude_from=["task"])
    mcp_store.set_agent_skill(AGENT, "char-row-off", enabled=False, exclude_from=[])
    agent_store.set_browser_allowed_origins(AGENT, ["http://localhost:3000", "https://allowed.test"])
    credential_store.set_infra_credentials("char-homelab", {"CHAR_HL_TOKEN": "hl-secret", "CHAR_HL_HOST": "192.168.77.13:8444"})
    for name in ("char-keyed", "char-unapproved"):
        credential_store.set_infra_credentials(name, {"CHAR_KEY": f"{name}-secret"})
    bearer_allowlist.add_allowed("char-keyed", "api.example.test")
    bearer_allowlist.add_allowed("char-oauth", "vendor.example.test")
    bearer_allowlist.add_allowed("char-oauth", "localhost")
    mcp_store.upsert_mcp_instance("char-env-inst", {
        "instance_name": "a-catch-all", "assigned_to_all": True,
        "field_values": {"INST_HOST": "192.168.77.41", "INST_PORT": "8441", "INST_KEY": "catch-all-secret"}})
    mcp_store.upsert_mcp_instance("char-env-inst", {
        "instance_name": "b-own", "agents": [AGENT],
        "field_values": {"INST_HOST": "192.168.77.40", "INST_PORT": "8440", "INST_KEY": "own-secret"}})
    mcp_store.upsert_mcp_instance("char-hosted", {
        "instance_name": "system", "assigned_to_all": True, "hosted_mode": "hosted", "field_values": {"VENDOR_KEY": "unused"}})
    mcp_store.upsert_mcp_instance("char-cfg-inst", {
        "instance_name": "box", "agents": [AGENT], "field_values": {"host": "192.168.77.30", "key_name": "id_char"}})
    credential_store.set_user_credentials("user-admin", "char-peruser", {"USER_TOKEN": "ada-secret", "USER_HOST": "192.168.77.50"},
                                          account_label="main")
    credential_store.set_user_credentials("user-manager", "char-peruser", {"USER_HOST": "192.168.77.51"}, account_label="main")
    for name in ("char-bearer", "char-bearer-sidecar", "char-gitish"):
        credential_store.set_user_credentials("user-admin", name, {"CHAR_EMAIL": "ada@example.test"}, account_label="main")
    for name in ("char-peruser", "char-bearer", "char-gitish"):
        assert service_account_set.add_service_account_to_agent(name, AGENT, account_label="main", owner_sub="user-admin",
                                                          set_by="user-admin") == ""
    _token_file("ada", "char-oauth")
    _token_file("ada", "char-git")
    yield SimpleNamespace(tmp_path=tmp_path, found=found)
    oauth_providers.clear_manifest_cache()


def _named(value, swaps: list[tuple[str, str]]):
    if isinstance(value, dict):
        return {k: _named(v, swaps) for k, v in value.items()}
    if isinstance(value, list):
        return [_named(v, swaps) for v in value]
    if isinstance(value, str):
        for old, new in swaps:
            value = value.replace(old, new)
    return value


def _scrub(world, value):
    """Plain JSON values with what varies by run or machine named (a machine
    session's door secret among them)."""
    from core.credentials import credential_exit
    doors = [(secret, "<door-secret>") for sid in list(credential_exit._sessions)
             if (secret := credential_exit.machine_secret(sid))]
    return _named(normalize(value), [(str(world.tmp_path), "<tmp>"), (str(config.PLATFORM_ROOT), "<platform-root>"),
                                     (f":{config.PORT}", ":<port>"), *doors])


def _names(manifests) -> list[str]:
    return [m.name for m in manifests]


def _get(name: str):
    return mcp_registry.get_manifest(name)


# ── the manifest store ────────────────────────────────────────────────────


def test_the_scan_finds_the_catalog_first_tree_wins_and_a_refused_manifest_stops_it(world, monkeypatch):
    def _context(name: str) -> list:
        return normalize(_get(name).agent_context)

    got = {
        "found": list(world.found),
        "shadowed_label": _get("char-tools").label,
        "states": mcp_store.get_all_mcp_states(),
        "builder_blocks_kept": _context("char-tools"),
    }
    # A strict refusal stops the scan, and the registry stays what it was.
    _write_catalog(world.tmp_path / "refused", {"custom": {"char-refused": _m("Refused", audience="team")}})
    monkeypatch.setattr(config, "MCPS_DIR", world.tmp_path / "refused")
    with pytest.raises(ValueError) as refused:
        mcp_registry.scan_manifests()
    got["refused"] = str(refused.value)
    got["registry_after_the_refusal"] = len(mcp_registry.get_all_manifests()) == len(world.found)
    check_golden("registry/manifest-scan", _scrub(world, got))


def test_the_lookups(world):
    tools, peruser = _get("char-tools"), _get("char-peruser")
    conflict = {"provider_id": "char-oauth", "authorization_server": {"registration": "dynamic"}}
    check_golden("registry/manifest-lookups", _scrub(world, {
        "unknown": _get("char-nobody"),
        "by_config_key": {k: getattr(mcp_registry.get_manifest_by_config_key(k), "name", None)
                          for k in ("char-tools", "local", "char-nobody")},
        "core": mcp_registry.core_mcp_names(),
        "by_provider": {p: _names(mcp_registry.get_mcps_by_provider(p)) for p in ("char-oauth", "char-git", "", "nobody")},
        "effective_config": {
            "tools": mcp_registry.effective_config(tools),
            "saved_given": mcp_registry.effective_config(tools, {"CHAR_MODE": "", "_X": "1"}),
            "none": mcp_registry.effective_config(None),
            "peruser": mcp_registry.effective_config(peruser),
        },
        "tool_filter": {n: mcp_registry.get_tool_filter(n) for n in ("char-tools", "char-sse", "char-nobody")},
        "authorization_server_conflict": {
            "other": mcp_registry.authorization_server_conflict("incoming", conflict),
            "self": mcp_registry.authorization_server_conflict("char-bearer", {"provider_id": "char-oauth"}),
            "same_way": mcp_registry.authorization_server_conflict("incoming", {"provider_id": "char-oauth"}),
            "none": mcp_registry.authorization_server_conflict("incoming", None),
        },
        "protected_subpaths": mcp_registry.get_protected_credentials_subpaths(),
        "oauth_scopes": {
            "services": mcp_registry.build_oauth_scopes("char-bearer", ["cal", "mail", "cal", "nope"]),
            "no_oauth": mcp_registry.build_oauth_scopes("char-tools", ["mail"]),
        },
        "device_capability": {n: mcp_registry.device_capability_for_server(n) for n in ("local", "char-tools", "", "char-nobody")},
        "skill_provider": {s: getattr(mcp_registry.find_skill_provider(s), "name", None)
                           for s in ("char-guide", "delegation-guide", "nobody")},
        "constants": {n: getattr(mcp_registry, n) for n in (
            "BROWSER_SERVER_KEY", "BROWSER_TOKEN_ENV", "DEVICE_CAPABILITIES", "EXTERNAL_CONTEXT", "EXTERNAL_CONTEXT_REASON",
            "EXTERNAL_DENIED_MCPS", "EXTERNAL_DENIED_REASON", "SHARED_CHAT_CONTEXT")},
    }))


def test_the_capability_gate(world, monkeypatch):
    monkeypatch.setattr(audio_service, "transcribe_capability_available", lambda: True)
    monkeypatch.setattr(audio_service, "tts_capability_available", lambda: False)
    monkeypatch.setattr(audio_service, "phone_calls_available", lambda: True)
    tools = _get("char-tools")
    check_golden("registry/capability-gate", {
        **{cap: mcp_registry.manifest_capability_available(dataclasses.replace(tools, requires_capability=cap))
           for cap in ("audio_transcribe", "audio_tts", "phone_calls", "no_such_capability")},
        "none": mcp_registry.manifest_capability_available(tools),
    })


def test_the_agents_mcps_by_placement(world):
    check_golden("registry/agent-mcps", _scrub(world, {
        "local": _names(mcp_registry.get_agent_mcps(AGENT)),
        "admin_machine": _names(mcp_registry.get_agent_mcps(AGENT, placement=_ADMIN_BOX)),
        "ungranted_machine": _names(mcp_registry.get_agent_mcps(AGENT, placement=_UNGRANTED_BOX)),
        "dark_machine": _names(mcp_registry.get_agent_mcps(AGENT, placement=_DARK_BOX)),
        "all_placements": _names(mcp_registry.get_agent_mcps_all_placements(AGENT)),
        "visible": _names(mcp_registry.get_visible_mcps_for_agent(AGENT)),
        "unauthorized_explicit": _names(mcp_registry.get_unauthorized_explicit_mcps_for_agent(AGENT)),
        "agent_with_no_rows": _names(mcp_registry.get_agent_mcps("char-nobody")),
    }))


def test_assigning_the_core_mcps(world):
    agent_store.create_agent("char-core-agent", "Char Core")
    assigned = mcp_registry.assign_core_mcps("char-core-agent")
    check_golden("registry/assign-core", {
        "assigned": assigned,
        "named": mcp_registry.assign_core_mcps("char-core-agent", ["char-sse", "char-nobody"]),
        "rows": sorted(mcp_store.get_manager_enabled_mcps("char-core-agent")),
        "skills": mcp_store.get_agent_skills("char-core-agent"),
    })


def test_the_credential_schemas(world):
    check_golden("registry/credential-schemas", _scrub(world, {
        "all": mcp_registry.get_all_credential_schemas(),
        "unknown": mcp_registry.get_credential_schema("char-nobody"),
    }))


# ── the session filter ────────────────────────────────────────────────────

# The picks of the outside cases: char-tools and char-no-many picked, the
# rest not.
_PICKS = OutsidePicks(tools={"char-tools": ToolPick("read_only"), "char-no-many": ToolPick("read_write")})
# "external" carries the picks (_PICKS); "external-unpicked" runs the rule
# with none, which admits nothing.
_CONTEXTS = {
    "dashboard": ({"dashboard"}, False, None), "phone": ({"phone"}, False, None), "task": ({"task"}, False, None),
    "meeting": ({"task", "meeting"}, False, None), "terminal": ({"terminal"}, False, None),
    "shared": ({"dashboard", "shared_chat"}, False, None), "external": ({"phone"}, True, _PICKS),
    "external-unpicked": ({"phone"}, True, None), "none": (set(), False, None),
}


def test_the_session_filter(world):
    names = ["char-tools", "char-no-phone", "char-no-task", "char-no-meeting", "char-no-terminal", "char-no-external",
             "char-no-shared-chat", "char-no-many", "delegation-mcp"]
    filtered = {}
    for ctx, (contexts, external, picks) in _CONTEXTS.items():
        kept = mcp_registry.filter_manifests_for_session(
            [_get(n) for n in names], contexts=contexts, external=external, picks=picks)
        attached, excluded = mcp_registry.preview_session_mcps(AGENT, contexts=contexts, external=external,
                                                               picks=picks)
        filtered[ctx] = {"kept": _names(kept), "preview_attached": _names(attached), "preview_excluded": excluded}
    check_golden("registry/session-filter", {
        "reason": {n: {ctx: mcp_registry.session_exclusion_reason(_get(n), contexts=c, external=e, picks=pk)
                       for ctx, (c, e, pk) in _CONTEXTS.items()} for n in names},
        "filter": filtered,
    })


def test_the_audience(world):
    roles = [None, "", "viewer", "contributor", "editor", "manager", "admin"]
    skill = {s.id: s for s in _get("char-tools").skills}
    check_golden("registry/audience", {
        "admits": {a or "<none>": {str(r): mcp_registry.audience_admits(a, r) for r in roles}
                   for a in ("", "owner", "editor", "workspace", "bogus")},
        "skill_admits": {s: {str(r): mcp_registry.skill_audience_admits(skill[s], r) for r in roles}
                         for s in ("char-guide", "char-editor", "char-owner")},
        "refusal": {scope: {str(r): mcp_registry.audience_refusal(_get("char-editors"), user_role=r, task_scope=scope)
                            for r in roles} for scope in ("user", "agent")},
        "refusal_of_no_audience": mcp_registry.audience_refusal(_get("char-tools"), user_role="viewer", task_scope="user"),
    })


# ── egress ────────────────────────────────────────────────────────────────


def _egress(**kw) -> dict:
    forwards, allow = mcp_registry.resolve_sandbox_egress(AGENT, **kw)
    return {"forwards": ["<port>" if f == str(config.PORT) else f for f in forwards], "allow_hosts": allow}


def _egress_view(world) -> dict:
    extra = ["http://localhost:11434", "localhost:11435", "192.168.77.60:11436", "http://lan-box.test:1",
             "http://multi.test:2", "", "http://[::1]:11437", "///"]
    urls = [f"http://{_HOST_SELF}:8080/v1", "http://[::1]:8080/v1", "::1", "http://localhost:8080", "http://127.0.0.1:1",
            "http://192.168.77.9:8080", "", "http://lan-box.test:9"]
    return _scrub(world, {
        "admin": _egress(user_sub="user-admin"),
        "viewer": _egress(user_sub="user-viewer"),
        "editor": _egress(user_sub="user-manager"),
        "no_person": _egress(),
        "extra_targets": _egress(extra_targets=extra),
        "network_access": {n: mcp_registry.network_access_enabled(_get(n)) for n in (
            "char-tools", "char-homelab", "char-toggle-off", "char-default-off", "char-toggle-on")},
        "targets": {n: mcp_registry.enumerate_mcp_network_targets(_get(n), AGENT, user_sub="user-admin") for n in (
            "char-tools", "char-homelab", "char-env-inst", "char-peruser", "char-toggle-off", "char-loopback")},
        "targets_no_person": mcp_registry.enumerate_mcp_network_targets(_get("char-peruser"), AGENT),
        "loopback_if_host_self": [[u, mcp_registry.loopback_if_host_self(u)] for u in urls],
    })


def test_egress_on_bare_metal(world):
    check_golden("registry/egress-bare-metal", _egress_view(world))


def test_egress_in_compose(world, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    check_golden("registry/egress-compose", _egress_view(world))


# ── the agent's skills and the prompt's catalog ───────────────────────────

_ADMIN_BOX = place.PlacementCapabilities(kind=place.KIND_ADMIN_REMOTE, machine_id="m-admin",
                                         device_grants={"browser"}, has_display=True)
_USER_BOX = place.PlacementCapabilities(kind=place.KIND_USER_REMOTE, machine_id="m-user",
                                        device_grants={"browser"}, has_display=True)
_DARK_BOX = place.PlacementCapabilities(kind=place.KIND_ADMIN_REMOTE, machine_id="m-dark",
                                        device_grants={"browser"}, has_display=False)
_UNGRANTED_BOX = place.PlacementCapabilities(kind=place.KIND_USER_REMOTE, machine_id="m-none")
# A paired Linux computer whose exit answers: the placeholder path most paired
# computers take (the boxes above report no OS, which takes the per-command one).
_LINUX_BOX = place.PlacementCapabilities(kind=place.KIND_ADMIN_REMOTE, machine_id="m-linux", os="linux",
                                         exit_port=41111, exit_ok=True, reported_otodock_dir="/home/alice/.oto-dock",
                                         device_grants={"browser"}, has_display=True)

_PROMPT_CASES = {
    "dashboard": {"context": "dashboard"},
    "no-context": {"context": ""},
    "phone": {"context": "phone"},
    "task": {"context": "task"},
    "meeting": {"context": "meeting"},
    "terminal": {"context": "terminal"},
    "viewer": {"context": "dashboard", "user_role": "viewer"},
    "editor": {"context": "dashboard", "user_role": "editor"},
    "manager": {"context": "dashboard", "user_role": "manager"},
    "external": {"context": "phone", "external": True, "user_role": "external", "outside_picks": _PICKS},
    "external-unpicked": {"context": "phone", "external": True, "user_role": "external"},
    "shared": {"context": "dashboard", "shared_chat": True},
    "machine": {"context": "dashboard", "placement": _ADMIN_BOX},
}


def test_the_skill_lists(world):
    lists = {case: {"inline": [[sid, loading] for sid, _body, loading in mcp_registry.get_skills_for_agent(AGENT, **kw)],
                    "catalog": mcp_registry.get_skill_catalog_for_agent(AGENT, **kw)}
             for case, kw in _PROMPT_CASES.items()}
    bodies = dict((sid, body) for sid, body, _l in mcp_registry.get_skills_for_agent(AGENT, "dashboard"))
    check_golden("registry/skills", _scrub(world, {"lists": lists, "body_without_frontmatter": bodies["char-always"]}))


def test_the_materialized_skills(world):
    check_golden("registry/skills-materialize", _scrub(world, {
        who: mcp_registry.get_on_demand_skills_for_materialization(AGENT, username=username)
        for who, username in (("agent", None), ("admin", "ada"), ("viewer", "vic"), ("editor", "max"), ("nobody", "ghost"))
    }))


def test_the_available_mcps_section(world):
    check_golden("registry/available-mcps-section", {
        **{case: mcp_registry.build_available_mcps_section(AGENT, **kw) for case, kw in _PROMPT_CASES.items()},
        "no-mcps": mcp_registry.build_available_mcps_section("char-nobody"),
    })


# ── the server entries ────────────────────────────────────────────────────


def test_a_server_entry_of_each_transport(world):
    ctx = {"chat_id": "chat-1", "task_owner": "owner", "task_username": "own", "task_scope": "user", "call_ask_back": "1"}
    got = {name: {fmt or "none": mcp_registry.resolve_server_config(_get(name), AGENT, mcp_config_format=fmt)
                  for fmt in ("json", "toml", None)}
           for name in ("char-tools", "char-http", "char-sse", "char-keyed", "char-context")}
    got["session_ctx"] = mcp_registry.resolve_server_config(_get("char-tools"), AGENT, session_ctx=ctx)
    check_golden("registry/server-resolve", _scrub(world, got))


def test_the_env_instance_values(world):
    check_golden("registry/env-instance-values", {
        name: mcp_registry.env_instance_values(_get(name), AGENT)
        for name in ("char-env-inst", "char-hosted", "char-no-instance", "char-cfg-inst", "char-tools")})


def _gateway(world, name: str, url: str = "", *, sub="user-admin", scope="user", bundle=None, only_owner=None) -> dict:
    entry = {"type": "http", "url": url or f"https://{_get(name).server.url_template.split('/')[2]}/mcp"}
    kw = {"only_owner": only_owner} if only_owner is not None else {}
    cred, reason = mcp_registry.gateway_entry(entry, _get(name), srv_key=name, user_sub=sub, agent_name=AGENT,
                                              task_scope=scope, bundle_env=bundle or {}, **kw)
    return _scrub(world, {"credential": cred, "reason": reason, "entry": entry})


def test_the_gateway_entry(world):
    stdio = {"type": "stdio", "command": "x", "args": []}
    untouched = mcp_registry.gateway_entry(stdio, _get("char-tools"), srv_key="x", user_sub=None, agent_name=AGENT,
                                           task_scope="user", bundle_env={})
    credential_store.set_user_credentials("user-manager", "char-bearer", {"CHAR_EMAIL": "max@example.test"}, account_label="main")
    _token_file("max", "char-oauth", extra={"refresh_failed": "invalid_grant"})
    check_golden("registry/gateway-entry", {
        "stdio": [untouched, stdio],
        "uncredentialed": _gateway(world, "char-sse", "https://sse.example.test/sse"),
        "key": _gateway(world, "char-keyed", bundle={"CHAR_KEY": "key-value"}),
        "key_empty": _gateway(world, "char-keyed"),
        "key_unapproved": _gateway(world, "char-unapproved", bundle={"CHAR_KEY": "key-value"}),
        "key_on_loopback": _gateway(world, "char-keyed", "http://localhost:9/mcp", bundle={"CHAR_KEY": "key-value"}),
        "bearer": _gateway(world, "char-bearer"),
        "bearer_agent_scope": _gateway(world, "char-bearer", sub=None, scope="agent"),
        "bearer_no_account": _gateway(world, "char-bearer", sub="user-viewer"),
        "bearer_dead_account": _gateway(world, "char-bearer", sub="user-manager"),
        "bearer_owner_filter_nobody": _gateway(world, "char-bearer", sub=None, scope="agent", only_owner=""),
        "bearer_sidecar": _gateway(world, "char-bearer-sidecar", "http://localhost:8934/mcp/"),
    })


# ── the session config ────────────────────────────────────────────────────

_ADMIN = {"user_sub": "user-admin", "username": "ada", "user_role": "admin"}
_VIEWER = {"user_sub": "user-viewer", "username": "vic", "user_role": "viewer"}


def _own_browser(**kw) -> BrowserTargetSettings:
    return BrowserTargetSettings(mode="own", extension_token="browser-token", owner_sub="user-admin", **kw)


_SESSIONS = {
    "admin": {**_ADMIN, "chat_id": "chat-a", "session_id": "s-admin", "delegation_targets": ["char-peer", "char-other"]},
    "viewer": {**_VIEWER, "chat_id": "chat-v"},
    "editor": {"user_sub": "user-manager", "username": "max", "user_role": "editor"},
    "shared-chat": {**_VIEWER, "shared_chat": True},
    "task-user": {**_ADMIN, "task_mode": True, "task_owner": "user-admin", "task_username": "ada"},
    "task-agent": {"user_sub": None, "task_mode": True, "task_scope": "agent", "delegation_targets": []},
    "meeting": {**_ADMIN, "task_mode": True, "meeting_mode": True, "extra_mcps": [
        "char-platform", "char-satellite", "char-tools", "char-nobody", "char-no-instance", "char-no-file-instance"]},
    "chat-worker": {**_ADMIN, "task_mode": True, "chat_worker": True, "shared_chat": True},
    "phone-person": {**_VIEWER, "phone_mode": True},
    # The picks a manager made: char-tools every function, char-http Read
    # only (unlisted, so it attaches with no function granted); the rest is
    # not picked, the hard set denied, the channel tool (char-platform) whole.
    "phone-external": {"user_sub": None, "phone_mode": True, "external": True, "user_role": "external",
                       "shared_chat": True, "extra_mcps": ["char-platform"], "call_ask_back": True,
                       "outside_picks": OutsidePicks(
                           tools={"char-tools": ToolPick("read_write"), "char-http": ToolPick("read_only")},
                           channel=frozenset({"char-platform"}))},
    "terminal": {**_ADMIN, "interactive_local": True},
    "admin-machine": {**_ADMIN, "placement": _ADMIN_BOX, "target_browser": _own_browser(profile="Work")},
    "viewer-machine": {**_VIEWER, "placement": _UNGRANTED_BOX},
    "person-on-own-machine": {**_ADMIN, "placement": _USER_BOX, "target_browser": _own_browser()},
    "no-display-machine": {**_ADMIN, "placement": _DARK_BOX},
    "linux-machine-with-its-exit": {**_ADMIN, "session_id": "s-linux", "placement": _LINUX_BOX},
    "agent-scope-on-owners-machine": {"user_sub": "user-admin", "task_scope": "agent", "placement": _USER_BOX,
                                      "target_browser": _own_browser(), "personal_owner": "user-admin"},
    "agent-scope-on-anothers-machine": {"user_sub": "user-viewer", "task_scope": "agent", "placement": _USER_BOX,
                                        "target_browser": _own_browser(), "personal_owner": ""},
    "unattended-token-off": {"user_sub": None, "task_mode": True, "task_scope": "agent", "placement": _ADMIN_BOX,
                             "target_browser": _own_browser()},
    "unattended-token-on": {"user_sub": None, "task_mode": True, "task_scope": "agent", "placement": _ADMIN_BOX,
                            "target_browser": _own_browser(unattended=True)},
    "judge": {**_ADMIN, "only_mcps": ["char-tools", "char-sse", "char-nobody"]},
    "direct": {**_ADMIN, "mcp_config_format": None},
}


def _view(world, result: tuple) -> dict:
    path, flat_env, excluded, bundles, bash_keys = result
    text = path.read_text() if path else None
    file = json.loads(text) if path and path.suffix == ".json" else text
    servers = file["mcpServers"] if isinstance(file, dict) else {}
    instance_files = {}
    for key, entry in servers.items():
        args = entry.get("args") or []
        if "--config-file" in args:
            with open(args[args.index("--config-file") + 1]) as fh:
                instance_files[key] = json.load(fh)
    return _scrub(world, {
        "path": path, "file": file, "server_order": list(servers), "instance_files": instance_files,
        "flat_env": flat_env, "excluded": excluded, "bundles": bundles, "bash_env_keys": sorted(bash_keys),
    })


@pytest.mark.parametrize("case", sorted(_SESSIONS))
def test_the_session_config_matrix(world, case, monkeypatch):
    kw = dict(_SESSIONS[case])
    sub = kw.pop("user_sub")
    check_golden(f"registry/session-config-{case}",
                 _view(world, mcp_registry.build_session_mcp_config(AGENT, sub, **kw)))


def test_the_session_config_in_compose(world, monkeypatch):
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: True)
    check_golden("registry/session-config-compose",
                 _view(world, mcp_registry.build_session_mcp_config(AGENT, "user-admin", username="ada", user_role="admin")))


def test_a_session_config_with_nothing_to_load(world, monkeypatch):
    folder = "registry"
    agent_store.create_agent("char-device-only", "Char Device Only")
    mcp_store.set_manager_enabled_mcps("char-device-only", ["char-browser"])
    agent_store.create_agent("char-context-only", "Char Context Only")
    mcp_store.set_manager_enabled_mcps("char-context-only", ["char-context"])
    check_golden(f"{folder}/session-config-edges", {
        "no_rows": _view(world, mcp_registry.build_session_mcp_config("char-nobody", "user-admin")),
        "device_only": _view(world, mcp_registry.build_session_mcp_config("char-device-only", "user-admin")),
        "context_only": _view(world, mcp_registry.build_session_mcp_config("char-context-only", "user-admin")),
        "context_on_user_machine": _view(world, mcp_registry.build_session_mcp_config(
            "char-context-only", "user-admin", placement=_USER_BOX)),
        "only_nothing": _view(world, mcp_registry.build_session_mcp_config(AGENT, "user-admin", only_mcps=[])),
    })


def test_the_hosted_relay_entry(world, monkeypatch):
    def _build() -> dict:
        return _view(world, mcp_registry.build_session_mcp_config(AGENT, "user-admin", only_mcps=["char-hosted"]))

    def _refuse(sub):
        raise relay_client.RelayNotConfigured("the relay said no")

    got = {"not_available": _build()}
    monkeypatch.setattr(relay_client, "is_available", lambda: True)
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://relay.example.test")
    monkeypatch.setattr(relay_client, "mint_session_token", lambda sub: f"relay-token-of-{sub}")
    got["available"] = _build()
    monkeypatch.setattr(relay_client, "mint_session_token", _refuse)
    got["refused"] = _build()
    check_golden("registry/session-config-relay", got)


def test_the_toml_writers(world, monkeypatch):
    folder = "registry"
    only = ["char-tools", "char-http", "char-sse", "char-bearer", "char-keyed", "char-gitish", "char-browser"]
    path, flat_env, excluded, bundles, bash_keys = mcp_registry.build_session_mcp_config(
        AGENT, "user-admin", username="ada", user_role="admin", mcp_config_format="toml", session_id="s-toml", only_mcps=only)
    built = path.read_text()
    sidecar = path.with_suffix(".servers.json")
    servers_saved = json.loads(sidecar.read_text())
    env = {"CHAR_INJECTED": 'va"lue\\with\nall', "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "k"}
    untouched = {
        "json_format": str(mcp_registry.inject_toml_credentials(path, env, mcp_format="json")) == str(path),
        "no_format": str(mcp_registry.inject_toml_credentials(path, env, mcp_format=None)) == str(path),
        "no_config": mcp_registry.inject_toml_credentials(None, env, mcp_format="toml"),
        "no_env": str(mcp_registry.inject_toml_credentials(path, {}, mcp_format="toml")) == str(path),
        "still_built": path.read_text() == built,
        "sidecar_kept": sidecar.exists(),
    }
    returned = mcp_registry.inject_toml_credentials(path, env, mcp_format="toml", exclude_keys=set(bash_keys) | {"GIT_CONFIG_KEY_0"})
    injected = path.read_text()
    sidecar_gone = not sidecar.exists()
    again = mcp_registry.inject_credential_env_into_toml(path, {"CHAR_AGAIN": "1"})
    sidecar.write_text("not json")
    corrupt = mcp_registry.inject_credential_env_into_toml(path, {"CHAR_AGAIN": "1"})
    check_golden(f"{folder}/toml-writers", _scrub(world, {
        "path": path, "built": built, "servers_saved": servers_saved, "flat_env": flat_env, "excluded": excluded,
        "bundles": bundles, "bash_env_keys": sorted(bash_keys), "untouched": untouched,
        "returned_same_path": returned == path, "injected": injected, "sidecar_gone": sidecar_gone,
        "without_a_sidecar": [again == path, path.read_text() == injected],
        "with_a_bad_sidecar": [corrupt == path, path.read_text() == injected],
    }))


def test_a_community_tool_its_compute_machine_has_not_built_is_left_out_of_the_session(world, monkeypatch):
    """A session placed on the compute machine that builds its own tools
    gets a community tool once that machine built it at the platform's
    version. Until then the build leaves it out with the reason and waits
    for nothing. The bundled tools come with the machine's image."""
    from services.mcp import mcp_sync
    from services.remote import machine_tools
    machine = place.PlacementCapabilities(kind=place.KIND_COMPUTE, machine_id="compute-own")
    mcp_store.set_mcp_enabled("char-disabled", True)       # a community program
    monkeypatch.setattr(machine_tools, "_machines", {})
    monkeypatch.setattr(machine_tools, "_wanted", set())
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                        lambda: SimpleNamespace(builds_own_tools=lambda mid: mid == "compute-own"))
    machine_tools.note_verify("compute-own", {})

    def _build():
        path, _env, excluded, _bundles, _keys = mcp_registry.build_session_mcp_config(
            AGENT, "user-admin", username="ada", user_role="admin", placement=machine)
        return json.loads(path.read_text())["mcpServers"], excluded

    servers, excluded = _build()
    assert excluded["char-disabled"] == ("Stays Disabled is being installed on OtoDock's compute machine. "
                                         "A new chat can use it once it is done")
    assert "char-disabled" not in servers and "char-tools" in servers
    assert machine_tools._wanted == {"compute-own"}

    built = mcp_sync._version_hash(mcp_registry.get_manifest("char-disabled"))
    machine_tools.note_installed("compute-own", "char-disabled", built, category="community")
    servers, excluded = _build()
    assert "char-disabled" in servers and "char-disabled" not in excluded
