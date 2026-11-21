"""Characterization of the MCP manifest parser: for a table of manifests (a
good one with every block, a refused or dropped case per block rule) the parsed
result or the exact refusal text, called through ``_parse_manifest`` as the
registry does, and the public helpers beside it. A drift is a behaviour change,
and the commit that records it says why."""
import json
import logging
import os

import pytest

import config
from services.mcp import mcp_manifest_parse as mp
from tests.fixtures.characterization.golden_master import check_golden
from tests.fixtures.loop_builder_rig import normalize

_STDIO = {"runtime": "python", "transport": "stdio", "command": "python3", "args": ["server.py"]}
_BASE = {
    "name": "char-mcp", "label": "Char", "description": "Char tools.", "version": "1.0.0",
    "category": "community", "server": _STDIO, "credentials": {"type": "none"},
}
_HEADER_SERVER = {"transport": "streamable_http", "url_template": "https://api.example.test/mcp"}
_HEADER_FIELD = {"key": "CHAR_KEY", "label": "Key", "input_type": "password"}
_HEADER = {"name": "X-Char-Key", "value_from": "CHAR_KEY", "proposed_hosts": ["api.example.test"]}
_OAUTH = {
    "provider_id": "char-oauth", "flows": ["authorization_code"],
    "authorization_url": "https://auth.example.test/authorize",
    "token_url": "https://auth.example.test/token",
    "base_scopes": ["base"],
    "services": [{"key": "mail", "label": "Mail", "description": "d", "scopes": ["mail.read"]}],
}
_WEBHOOKS = {
    "available": True, "provider_id": "char-oauth",
    "signature": {"algorithm": "hmac-sha256", "header": "X-Sig", "per_subscription_secret": True},
    "url_verification": {"kind": "none"},
    "registration": {"mode": "auto", "create": {
        "method": "POST", "url_template": "https://api.example.test/hooks", "expected_status": [201]}},
    "event_catalog": [{"key": "push", "label": "Pushed", "required_scopes": ["mail.read"]}],
    "payload_normalization": {"event_type_path": "headers.X-Event"},
    "vendor_target_spec": {"kind": "free_text", "label": "Target"},
}

# Every block the parser reads, together. ``hosted.api_key_relay`` cannot stand
# beside a header-style key; the hosted table carries it.
_FULL = {
    **_BASE,
    "server_name": "char-key", "author": "OtoDock", "author_url": "https://example.test",
    "assignment_mode": "explicit", "audience": "editor", "requires_capability": "audio_tts",
    "server": {
        **_HEADER_SERVER, "runtime": "docker", "command": "python3", "args": ["a", "b"],
        "source": "remote:api.example.test", "docker_compose": "deploy/compose.yml", "port": 8932,
        "health_endpoint": "/health", "proxy_callbacks": True, "service_name": "char-svc",
        "image": "char:1", "version_constraint": ">=2,<3", "source_build": ["psutil", "bad name"],
        "tool_timeout_sec": 120,
    },
    "credentials": {
        "type": "per_user", "label": "Char Account", "description": "An account.",
        "fields": [_HEADER_FIELD], "server_config_fields": [{"key": "CHAR_SMTP", "label": "SMTP"}],
        "service_account": True, "ui_type": "custom",
        "oauth": {**_OAUTH, "app_credential_fields": [{"key": "CLIENT_ID", "label": "Client"}]},
        "webhooks": _WEBHOOKS, "api_key_header": _HEADER,
    },
    "config": [{"key": "CHAR_MODE", "label": "Mode", "input_type": "select", "default": "fast",
                "required": True, "user_overridable": True}, {"key": "CHAR_RAW"}],
    "env": {"CHAR_STATIC": "1"}, "agent_env": {"CHAR_AGENT": "${agent_name}"},
    "exclude_from": ["phone", "external"],
    "skills": [{"id": "char-guide", "file": "skills/guide.md", "description": "A guide.",
                "default_exclude_from": ["phone"], "loading": "always", "audience": "owner"}],
    "system_requirements": {"debian": ["ffmpeg"], "ubuntu": ["ffmpeg"], "rhel": ["ffmpeg-free"],
                            "arch": ["ffmpeg"], "macos_brew": ["ffmpeg"], "node_min": 18, "notes": "n"},
    "instances": {"delivery": "env", "max_instances": 2, "transform": "ssh_hosts",
                  "config_file_arg": "--c", "config_file_name": "c.json",
                  "fields": [{"key": "INST_HOST", "label": "Host", "input_type": "url", "default": "x",
                              "required": True, "secret": True}, {"key": "INST_RAW"}]},
    "network_targets": [{"source": "instance", "host_key": "INST_HOST", "port_key": "INST_PORT",
                         "port_default": 22}, {"source": "config"}],
    "network_access_default": False,
    "data_dirs": {"cache": "cache"},
    "sandbox": {"mounts": [{"host": "/mounted/x", "sandbox": "/x", "mode": "rw"}, {"host": "/y"}]},
    "hosted": {"oauth_app": {"available": True, "default_mode": "self_managed"}},
    "outputs": [{"source": "${mcp_dir}/out", "destination_template": "${workspace_dir}/out",
                 "after_tools": ["render"], "keep_recent": 5, "gc_after": 86400},
                {"source": "no-destination", "after_tools": ["render"]}],
    "result_files": {"tools": ["fetch_map"], "subdir": "maps"},
    "path_env": {"CHAR_FILES": {"role": "workspace", "subpath": "files"},
                 "CHAR_ROOTS": {"values": [{"role": "user_root"}, {"role": "config"}], "join": "|"}},
    "tool_arg_paths": {"fetch": {"path": {"mode": "write", "optional": True, "relative_anchor": "/workspace"}}},
    "costs": {"currency": "USD", "provider": " vendor ",
              "rules": [{"tool": "fetch", "amount": 2, "match": {"size": ["a", "b"], "hd": True},
                         "multiply_by": "count"}]},
    "permissions": {"default_tier": "open", "rules": [{"tool": "fetch*", "tier": "sensitive"}]},
    "agent_context": [{"template": "Hello ${agent_name}", "requires": ["a"], "scope": ["user"],
                       "builder": {"tool": "mcp__char-key__peek", "args": {"a": 1},
                                   "timeout_seconds": 3, "account_label": "main"}}],
    "tool_filter": {"arg_name": "--enabled-tools", "env_var_name": "CHAR_F", "regex_env_var_name": "CHAR_R"},
    "always_load": True, "placement": "satellite_only", "remote_policy": "admin_paired_only",
    "requires_display": True, "device_capability": "browser",
    "companion_app": {"program": "Char", "connect": {"host": "127.0.0.2", "port": 9},
                      "plugin": {"asset": "a.zip", "supported_host_versions": ["1"]},
                      "status_tool": "status", "auth": "none"},
    "replaces": [{"source": "npm:old", "credentials": {"OLD_KEY": "NEW_KEY"}}],
    "patched": True, "patch_note": "note",
}


def _write(folder, body) -> os.PathLike:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(body))
    return folder / "manifest.json"


def _named(value, tmp: str):
    if isinstance(value, dict):
        return {k: _named(v, tmp) for k, v in value.items()}
    if isinstance(value, list):
        return [_named(v, tmp) for v in value]
    return value.replace(tmp, "<tmp>") if isinstance(value, str) else value


def _scrub(value, tmp_path):
    """Plain JSON values with the run's temp folder named."""
    return _named(normalize(value), str(tmp_path))


def _warnings(caplog, tmp_path) -> list[str]:
    return [_scrub(r.getMessage(), tmp_path) for r in caplog.records
            if r.name.startswith("services.") and r.levelno >= logging.WARNING]


def _outcome(tmp_path, caplog, case: str, body, pick: str | None) -> dict:
    """One manifest through the parser: the refusal, or the parsed value (the
    one field ``pick`` names, else all of it) and the warnings it logged."""
    caplog.clear()
    path = _write(tmp_path / "cases" / case.replace(" ", "-"), body)
    with caplog.at_level(logging.WARNING):
        try:
            parsed = mp._parse_manifest(path)
        except ValueError as exc:
            return {"refused": _scrub(str(exc), tmp_path)}
        except Exception as exc:  # noqa: BLE001  # an uncaught defect is part of the behaviour the golden pins
            return {"raised": type(exc).__name__, "text": _scrub(str(exc), tmp_path)}
    out = {"parsed": _scrub(getattr(parsed, pick) if pick else parsed, tmp_path)}
    found = _warnings(caplog, tmp_path)
    if found:
        out["warnings"] = found
    return out


def _table(tmp_path, caplog, pick: str | None, cases: dict[str, dict]) -> dict:
    return {case: _outcome(tmp_path, caplog, case, {**_BASE, **over}, pick)
            for case, over in cases.items()}


def _block(key: str, cases: dict) -> dict[str, dict]:
    return {case: {key: value} for case, value in cases.items()}


_RELAY = {"available": True, "relay_path": "/relay/char"}


def _rule(**kw) -> dict:
    return {"tool": "fetch", "amount": 1, **kw}


def _builder(**kw) -> list:
    return [{"template": "t", "builder": {"tool": "mcp__x__y", **kw}}]


def _tool_arg(path: str = "a", **decl) -> dict:
    return {"fetch": {path: decl}}


def _header(**kw) -> dict:
    return {"credentials": {"type": "infra", "fields": [_HEADER_FIELD],
                            "api_key_header": {**_HEADER, **kw}},
            "server": _HEADER_SERVER}


BLOCKS: dict[str, tuple[str | None, dict[str, dict]]] = {
    "permissions": ("permissions", _block("permissions", {
        "absent": None,
        "not an object": [],
        "bad default tier": {"default_tier": "loud"},
        "rules not a list": {"rules": {}},
        "rule not an object": {"rules": ["x"]},
        "empty tool": {"rules": [{"tool": " ", "tier": "open"}]},
        "prefixed tool": {"rules": [{"tool": "mcp__x__y", "tier": "open"}]},
        "bad tier": {"rules": [{"tool": "a", "tier": "loud"}]},
        "unknown key": {"rules": [{"tool": "a", "tier": "open", "note": 1}]},
        "duplicate exact": {"rules": [{"tool": "a", "tier": "open"}, {"tool": "a", "tier": "standard"}]},
        "repeated glob": {"rules": [{"tool": "a*", "tier": "open"}, {"tool": "a*", "tier": "standard"}]},
        "default only": {},
    })),
    "costs": ("costs", _block("costs", {
        "absent": None,
        "not an object": [],
        "bad currency": {"currency": "EUR"},
        "empty provider": {"currency": "USD", "provider": " "},
        "no rules": {"currency": "USD", "provider": "v", "rules": []},
        "rule not an object": {"currency": "USD", "provider": "v", "rules": [1]},
        "empty tool": {"currency": "USD", "provider": "v", "rules": [{"tool": "", "amount": 1}]},
        "amount text": {"currency": "USD", "provider": "v", "rules": [_rule(amount="1")]},
        "amount bool": {"currency": "USD", "provider": "v", "rules": [_rule(amount=True)]},
        "amount negative": {"currency": "USD", "provider": "v", "rules": [_rule(amount=-1)]},
        "match not an object": {"currency": "USD", "provider": "v", "rules": [_rule(match=[])]},
        "match empty list": {"currency": "USD", "provider": "v", "rules": [_rule(match={"k": []})]},
        "match nested list": {"currency": "USD", "provider": "v", "rules": [_rule(match={"k": [[1]]})]},
        "match object": {"currency": "USD", "provider": "v", "rules": [_rule(match={"k": {}})]},
        "multiply_by number": {"currency": "USD", "provider": "v", "rules": [_rule(multiply_by=3)]},
        "duplicate rule": {"currency": "USD", "provider": "v", "rules": [_rule(), _rule(amount=2)]},
        "good": {"currency": "USD", "provider": " v ", "rules": [
            _rule(), _rule(match={"size": ["a", "b"]}), _rule(match={"hd": True}, multiply_by="n")]},
    })),
    "agent-context": ("agent_context", _block("agent_context", {
        "absent": None,
        "not a list": {},
        "block not an object": ["t"],
        "unknown key": [{"template": "t", "when": 1}],
        "empty template": [{"template": " "}],
        "bad requires": [{"template": "t", "requires": [""]}],
        "scope not a list": [{"template": "t", "scope": "user"}],
        "bad scope": [{"template": "t", "scope": ["user", "team"]}],
        "builder not an object": [{"template": "t", "builder": []}],
        "credential token": [{"template": "${credential.TOKEN}", "requires": ["credential.OTHER"]}],
        "good": [{"template": "t", "requires": ["a"], "scope": ["user", "agent"]}],
    })),
    "builder": ("agent_context", _block("agent_context", {
        "unknown key": _builder(retries=1),
        "empty tool": [{"template": "t", "builder": {"tool": " "}}],
        "bad tool": [{"template": "t", "builder": {"tool": "mcp__X__y"}}],
        "args not an object": _builder(args=[]),
        "timeout bool": _builder(timeout_seconds=True),
        "timeout text": _builder(timeout_seconds="5"),
        "timeout low": _builder(timeout_seconds=0),
        "timeout high": _builder(timeout_seconds=31),
        "label not text": _builder(account_label=1),
        "defaults": _builder(),
    })),
    "tool-filter": ("tool_filter", {
        "absent": {"tool_filter": None},
        "not an object": {"tool_filter": []},
        "no arg name": {"tool_filter": {}},
        "short flag": {"tool_filter": {"arg_name": "-t"}},
        "empty env": {"tool_filter": {"arg_name": "--t", "env_var_name": ""}},
        "bad env": {"tool_filter": {"arg_name": "--t", "env_var_name": "1BAD"}},
        "bad regex env": {"tool_filter": {"arg_name": "--t", "regex_env_var_name": "1BAD"}},
        "same names": {"tool_filter": {"arg_name": "--t", "env_var_name": "A", "regex_env_var_name": "A"}},
        "env taken": {"tool_filter": {"arg_name": "--t"}, "env": {"ENABLED_TOOLS_FLAG": "1"}},
        "agent env taken": {"tool_filter": {"arg_name": "--t"}, "agent_env": {"ENABLED_TOOLS_REGEX": "1"}},
        "config taken": {"tool_filter": {"arg_name": "--t", "env_var_name": "CFG"}, "config": [{"key": "CFG"}]},
        "credential taken": {"tool_filter": {"arg_name": "--t", "env_var_name": "CRED"},
                             "credentials": {"type": "infra", "fields": [{"key": "CRED"}]}},
        "instance taken": {"tool_filter": {"arg_name": "--t", "env_var_name": "INST"},
                           "instances": {"fields": [{"key": "INST"}]}},
        "path taken": {"tool_filter": {"arg_name": "--t", "env_var_name": "PATHY"},
                       "path_env": {"PATHY": {"role": "workspace"}}},
        "defaults": {"tool_filter": {"arg_name": "--enabled-tools"}},
    }),
    "result-files": ("result_files", {
        "absent": {},
        "not an object": {"result_files": []},
        "no tools": {"result_files": {"tools": []}},
        "too many": {"result_files": {"tools": [f"t{i}" for i in range(65)]}},
        "glob": {"result_files": {"tools": ["a*"]}},
        "tool not text": {"result_files": {"tools": [3]}},
        "twice": {"result_files": {"tools": ["a", "a"]}},
        "climbing": {"result_files": {"tools": ["a"], "subdir": ".."}},
        "reserved": {"result_files": {"tools": ["a"], "subdir": "Uploads"}},
        "device": {"result_files": {"tools": ["a"], "subdir": "COM1"}},
        "relocated tool": {"result_files": {"tools": ["a"]}, "outputs": [
            {"source": "s", "destination_template": "d", "after_tools": ["a"]}]},
        "relocated star": {"result_files": {"tools": ["a"]}, "outputs": [
            {"source": "s", "destination_template": "d"}]},
        "another tool relocated": {"result_files": {"tools": ["a"]}, "outputs": [
            {"source": "s", "destination_template": "d", "after_tools": ["b"]}]},
        "default subdir": {"result_files": {"tools": ["a"]}},
        "default subdir of a dotted name": {"name": "char.mcp", "result_files": {"tools": ["a"]}},
        "named": {"result_files": {"tools": ["a", "b.c"], "subdir": "maps"}},
    }),
    "path-env": ("path_env", _block("path_env", {
        "absent": None,
        "not an object": "x",
        "entry not an object": {"A": "workspace"},
        "both": {"A": {"role": "workspace", "values": [{"role": "workspace"}]}},
        "neither": {"A": {}},
        "unknown role": {"A": {"role": "nope"}},
        "credentials without subpath": {"A": {"role": "credentials_dir"}},
        "values not a list": {"A": {"values": "x"}},
        "value not an object": {"A": {"values": ["x"]}},
        "value unknown role": {"A": {"values": [{"role": "nope"}]}},
        "value credentials without subpath": {"A": {"values": [{"role": "credentials_dir"}]}},
        "good": {"A": {"role": "workspace", "subpath": "files"},
                 "B": {"role": "credentials_dir", "subpath": "tok"},
                 "C": {"values": [{"role": "user_root"}, {"role": "credentials_dir", "subpath": "x"}]},
                 "D": {"values": [{"role": "config"}], "join": "|"},
                 "E": {"role": "nope"}},
    })),
    "tool-arg-paths": ("tool_arg_paths", _block("tool_arg_paths", {
        "absent": None,
        "not an object": [],
        "empty tool name": {"": {}},
        "tool not an object": {"fetch": []},
        "recursive": _tool_arg("a..b"),
        "double star": _tool_arg("a**"),
        "predicate": _tool_arg("a[?(@.b)]"),
        "leading dot": _tool_arg(".a"),
        "trailing dot": _tool_arg("a."),
        "bracket string": _tool_arg('a["k"]'),
        "numeric index": _tool_arg("a[3]"),
        "other syntax": _tool_arg("a b"),
        "empty path": _tool_arg(""),
        "declaration not an object": {"fetch": {"a": 1}},
        "bad mode": _tool_arg(mode="exec"),
        "bad anchor": _tool_arg(relative_anchor="rel"),
        "good": {"fetch": {"a.b": {}, "items[*].path": {"mode": "write", "optional": 1,
                                                        "relative_anchor": "/workspace"}},
                 "store": {"file": {"mode": "read", "relative_anchor": None}}},
    })),
    "hosted": ("hosted", {
        "absent": {"hosted": None},
        "empty": {"hosted": {}},
        "not an object": {"hosted": "x"},
        "oauth not an object": {"hosted": {"oauth_app": []}},
        "bad oauth mode": {"hosted": {"oauth_app": {"default_mode": "disabled"}}},
        "relay not an object": {"hosted": {"api_key_relay": []}},
        "relay no path": {"hosted": {"api_key_relay": {"available": True}}},
        "relay no instances": {"hosted": {"api_key_relay": _RELAY}},
        "relay file delivery": {"hosted": {"api_key_relay": _RELAY},
                                "instances": {"delivery": "config_file", "fields": []}},
        "bad relay mode": {"hosted": {"api_key_relay": {"default_mode": "x"}}},
        "neither block": {"hosted": {"other": 1}},
        "both": {"hosted": {"oauth_app": {"available": True},
                            "api_key_relay": {**_RELAY, "default_mode": "self_managed",
                                              "min_balance_to_enable_usd": 5,
                                              "billing_setup_url": "https://billing.test"}},
                 "instances": {"delivery": "env", "fields": [{"key": "K"}]}},
    }),
    "api-key-header": ("credentials", {
        "not an object": {"credentials": {"type": "infra", "api_key_header": []}},
        "missing name": {"credentials": {"type": "infra", "api_key_header": {"value_from": "K"}}},
        "missing hosts": {"credentials": {"type": "infra", "api_key_header": {"name": "X", "value_from": "K"}}},
        "unknown key": _header(extra=1),
        "bad name": _header(name="not a header"),
        "owned name": _header(name="Authorization"),
        "owned family": _header(name="X-Forwarded-For"),
        "bad value_from": _header(value_from="1bad"),
        "beside a bearer": {"server": _HEADER_SERVER, "credentials": {
            "type": "infra", "api_key_header": _HEADER, "oauth": {"bearer_required": True}}},
        "beside the relay": {**_header(), "hosted": {"api_key_relay": {"available": False}}},
        "beside an empty relay": {**_header(), "hosted": {"api_key_relay": {}}},
        "undeclared key": _header(value_from="OTHER"),
        "text field": {"server": _HEADER_SERVER, "credentials": {
            "type": "infra", "api_key_header": _HEADER, "fields": [{"key": "CHAR_KEY", "input_type": "text"}]}},
        "hosts empty": _header(proposed_hosts=[]),
        "bad host": _header(proposed_hosts=["https://x"]),
        "stdio": {"credentials": {"type": "infra", "fields": [_HEADER_FIELD], "api_key_header": _HEADER}},
        "template": {"server": {"transport": "streamable_http", "url_template": "https://${x}/mcp"},
                     "credentials": _header()["credentials"]},
        "no scheme": {"server": {"transport": "http", "url_template": "api.example.test/mcp"},
                      "credentials": _header()["credentials"]},
        "other host": {"server": {"transport": "streamable-http", "url_template": "https://other.test/mcp"},
                       "credentials": _header()["credentials"]},
        "instance field": {"server": _HEADER_SERVER, "instances": {"fields": [{"key": "CHAR_KEY"}]},
                           "credentials": {"type": "none", "api_key_header": _HEADER}},
        "good": _header(proposed_hosts=["*.example.test"]),
    }),
    "credentials": ("credentials", {
        "not an object": {"credentials": "x"},
        "oauth not an object": {"credentials": {"type": "per_user", "oauth": "x"}},
        "oauth rule": {"credentials": {"type": "per_user", "oauth": {"provider_id": " "}}},
        "webhooks not an object": {"credentials": {"type": "infra", "webhooks": []}},
        "webhooks rule": {"credentials": {"type": "infra", "webhooks": {"available": "yes"}}},
        "webhooks off and incomplete": {"credentials": {"type": "infra", "webhooks": {"available": False}}},
        "provider mismatch": {"credentials": {"type": "per_user", "oauth": {**_OAUTH, "provider_id": "other"},
                                              "webhooks": _WEBHOOKS}},
        "good": {"credentials": {"type": "per_user", "oauth": _OAUTH, "webhooks": _WEBHOOKS,
                                 "fields": [{"key": "K"}]}},
    }),
    "top-level": (None, {
        "null name": {"name": None},
        "server not an object": {"server": "x"},
        "config without key": {"config": [{}]},
        "instance field without key": {"instances": {"fields": [{}]}},
        "bad audience": {"audience": "team"},
        "always_load text": {"always_load": "yes"},
        "bad placement": {"placement": "moon"},
        "bad remote policy": {"remote_policy": "never"},
        "bad device capability": {"device_capability": "printer"},
        "companion not an object": {"companion_app": []},
        "companion connect": {"companion_app": {"connect": ["x"]}},
        "companion plugin": {"companion_app": {"plugin": ["x"]}},
        "companion auth": {"companion_app": {"auth": "password"}},
        "companion defaults": {"companion_app": {}},
        "outputs defaults": {"outputs": [{"source": "s", "destination_template": "d"}, {"source": "s"}]},
    }),
    "skills": ("skills", _block("skills", {
        "absent": None,
        "not a list": "x",
        "entry not an object": ["x"],
        "id not text": [{"id": 1, "file": "a.md"}],
        "file not text": [{"id": "a", "file": 1}],
        "bad id": [{"id": "Not_Valid", "file": "a.md"}],
        "long id": [{"id": "a" * 65, "file": "a.md"}],
        "absolute file": [{"id": "a", "file": "/abs/skill.md"}],
        "climbing file": [{"id": "a", "file": "../a.md"}],
        "bad loading": [{"id": "a", "file": "a.md", "loading": "sometimes"}],
        "bad audience": [{"id": "a", "file": "a.md", "audience": "team"}],
        "good": [{"id": "a", "file": "a.md"},
                 {"id": "b-c", "file": "skills/b.md", "description": "d", "default_exclude_from": ["task"],
                  "loading": "always", "audience": "workspace"}],
    })),
    "server": ("server", _block("server", {
        "npm range": {**_STDIO, "version_constraint": "^2"},
        "good range": {**_STDIO, "version_constraint": ">=2,<3"},
        "source build entries": {**_STDIO, "source_build": ["ok-pkg", "bad name", 3]},
        "compose outside": {**_STDIO, "docker_compose": "../compose.yml"},
        "compose inside": {**_STDIO, "docker_compose": "deploy/compose.yml"},
        "timeout zero": {**_STDIO, "tool_timeout_sec": 0},
        "timeout high": {**_STDIO, "tool_timeout_sec": 3601},
        "timeout text": {**_STDIO, "tool_timeout_sec": "5"},
        "timeout bool": {**_STDIO, "tool_timeout_sec": True},
        "timeout good": {**_STDIO, "tool_timeout_sec": 3600},
        "defaults": {},
    })),
}


@pytest.mark.parametrize("block", sorted(BLOCKS))
def test_block_rules(block, tmp_path, caplog):
    pick, cases = BLOCKS[block]
    check_golden(f"manifest-parse/{block}", _table(tmp_path, caplog, pick, cases))


def test_the_manifest_with_every_block_and_the_minimal_one(tmp_path, caplog):
    check_golden("manifest-parse/full", {
        "full": _outcome(tmp_path, caplog, "full", _FULL, None),
        "minimal": _outcome(tmp_path, caplog, "minimal", {"name": "bare"}, None),
        "nameless": _outcome(tmp_path, caplog, "nameless", {}, None),
    })


def test_a_file_that_cannot_be_read_is_no_manifest(tmp_path, caplog):
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "manifest.json").write_text("{nope")
    got = {}
    for case, path in (("not json", tmp_path / "bad" / "manifest.json"),
                       ("missing", tmp_path / "gone" / "manifest.json")):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            parsed = mp._parse_manifest(path)
        # The sentence up to the reader's own words, which belong to Python.
        got[case] = {"parsed": parsed, "warnings": [w.split(": ", 1)[0] for w in _warnings(caplog, tmp_path)]}
    check_golden("manifest-parse/unreadable", got)


def test_critical_is_the_platforms_own_tier(tmp_path, caplog, monkeypatch):
    """A manifest under the platform's ``custom`` tree keeps ``critical``;
    anywhere else it loads as ``sensitive`` with one warning."""
    monkeypatch.setattr(config, "MCPS_DIR", tmp_path / "mcps")
    body = {**_BASE, "permissions": {"default_tier": "critical", "rules": [
        {"tool": "wipe", "tier": "critical"}, {"tool": "read", "tier": "open"}]}}
    got = {}
    for tree in ("custom", "community"):
        caplog.clear()
        folder = tmp_path / "mcps" / tree / "char-mcp"
        with caplog.at_level(logging.WARNING):
            parsed = mp._parse_manifest(_write(folder, body))
        got[tree] = {"ships_with_platform": mp.ships_with_platform(folder),
                     "permissions": normalize(parsed.permissions),
                     "warnings": _warnings(caplog, tmp_path)}
    check_golden("manifest-parse/tiers", got)


def test_the_skill_file_rule(tmp_path):
    root = tmp_path / "mcp"
    (root / "skills" / "dir.md").mkdir(parents=True)
    (root / "skills" / "ok.md").write_text("ok")
    outside = tmp_path / "secret.md"
    outside.write_text("secret")
    (root / "skills" / "link.md").symlink_to(outside)
    (root / "skills" / "inner.md").symlink_to(root / "skills" / "ok.md")
    values = ["", None, 3, "a" * 513, "a\tb.md", "a\\b.md", "/abs.md", "C:/x.md", "c:x.md", "../x.md",
              "a/../b.md", "a//b.md", "./a.md", "a/", ".", "skills/ok.md", "skills/.hidden.md", "a b.md"]
    resolves = ["skills/ok.md", "skills/inner.md", "skills/link.md", "skills/dir.md", "skills/none.md",
                "../secret.md", str(outside), ""]
    check_golden("manifest-parse/skill-file", {
        "skill_file_error": [[repr(v)[:40], mp.skill_file_error(v)] for v in values],
        "resolve_skill_file": _scrub([[v, mp.resolve_skill_file(root, v)] for v in resolves], tmp_path),
    })


def test_the_audience_rule():
    values = ["", None, "owner", "editor", "workspace", "manager", "Owner", 3, ["owner"]]
    check_golden("manifest-parse/skill-audience",
                 [[repr(v), mp.skill_audience_error(v)] for v in values])


def test_the_replaces_rule(tmp_path, caplog):
    entries = [
        None, "x", [], {}, {"source": " "}, {"source": 3}, {"source": "npm:a"}, {"source": " npm:a ", "credentials": None},
        {"source": "npm:a", "credentials": []}, {"source": "npm:a", "credentials": {"_OLD": "NEW"}},
        {"source": "npm:a", "credentials": {"OLD": "_NEW"}}, {"source": "npm:a", "credentials": {"OLD": 3}},
        {"source": "npm:a", "credentials": {"OLD": "NEW", "O2": "N2"}},
    ]
    parsed = {}
    for case, raw in (("absent", None), ("empty", []), ("not a list", {"source": "npm:a"}), ("mixed", entries)):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            out = mp.parse_replaces(raw, "char-mcp")
        parsed[case] = {"parsed": normalize(out), "warnings": _warnings(caplog, tmp_path)}
    check_golden("manifest-parse/replaces", {
        "replaces_entry_error": [[repr(e), mp.replaces_entry_error(e)] for e in entries],
        "parse_replaces": parsed,
    })
