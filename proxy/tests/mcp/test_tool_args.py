"""The custom MCPs check a tool call's arguments themselves (``_tool_args.py``):
a near-miss enum value is taken as its member, anything else invalid is
refused with the argument named and an enum's allowed values listed.

Each server copies ``mcps/_shared/tool_args.py`` byte for byte. Each runs here
in a child interpreter (its own siblings and environment, no request leaving
it: the proxy URL is a closed port) and is called through the SDK's own
request handlers, so the test fails if the SDK stops listing the tools before
a call or the server stops answering with the refusal.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from tests._paths import CUSTOM_MCPS, REPO_ROOT, load_mcp_server

REFERENCE = REPO_ROOT / "mcps" / "_shared" / "tool_args.py"

# The servers that check their own arguments, with their entry file.
SERVERS = {
    "agent-config-mcp": "server.py", "agent-creator-mcp": "server.py", "checks-mcp": "server.py",
    "computer-mcp": "server.py", "delegation-mcp": "server.py", "display-mcp": "display_server.py",
    "file-tools-mcp": "server.py", "image-gen-mcp": "server.py", "image-search-mcp": "server.py",
    "location-mcp": "server.py", "mcps-mcp": "server.py", "meetings-mcp": "server.py",
    "music-gen-mcp": "server.py", "notifications-mcp": "server.py", "schedules-mcp": "server.py",
    "ssh-hosts": "server.py", "transcribe-mcp": "server.py", "triggers-mcp": "server.py",
    "tts-mcp": "server.py", "video-gen-mcp": "server.py",
}

# Runs in the child: load the server from its folder, call one tool through
# the SDK's request handler with an argument it must refuse, print the outcome.
CHILD = r'''
import asyncio, importlib.util, json, os, sys
folder, entry = sys.argv[1], sys.argv[2]
sys.path.insert(0, folder)
spec = importlib.util.spec_from_file_location("server_under_test", os.path.join(folder, entry))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if hasattr(mod, "_ENGINE_CATALOG"):
    mod._ENGINE_CATALOG = {"claude-code-cli": {"runtime": {"supports_interactive_pty": True}},
                           "codex-cli": {"runtime": {}}, "direct-llm": {"runtime": {}}}
server = getattr(mod, "server", None) or getattr(mod, "mcp_server")
from mcp import types

def filler(prop):
    return {"string": "x", "integer": 1, "number": 1, "boolean": True,
            "array": [], "object": {}}.get(prop.get("type"), "x")

async def main():
    listed = (await server.request_handlers[types.ListToolsRequest](None)).root.tools
    server._tool_cache.clear()            # the call must list again on its own
    probe = None
    for tool in listed:
        props = tool.inputSchema.get("properties") or {}
        for key, prop in props.items():
            if isinstance(prop.get("enum"), list) and all(isinstance(m, str) for m in prop["enum"]):
                args = {k: filler(props.get(k, {})) for k in tool.inputSchema.get("required", [])}
                args[key] = "not-a-member"
                probe = ("enum", tool.name, key, prop["enum"], args)
                break
        if probe:
            break
    if probe is None:
        tool = next((t for t in listed if t.inputSchema.get("required")), None)
        if tool is not None:
            probe = ("required", tool.name, tool.inputSchema["required"][0], None, {})
    def formats(node):
        if isinstance(node, dict):
            return any(k == "format" and isinstance(v, str) or formats(v) for k, v in node.items())
        if isinstance(node, list):
            return any(formats(v) for v in node)
        return False

    report = {"tools": [t.name for t in listed],
              "formats": [t.name for t in listed if formats(t.inputSchema)]}
    if probe is None:
        name = listed[0].name
        report.update(kind="passthrough", tool=name,
                      check=mod.TOOL_ARGS.check(name, {"unexpected": 1})[1] is None,
                      recorded=sorted(mod.TOOL_ARGS._schemas))
    else:
        kind, name, key, members, args = probe
        req = types.CallToolRequest(method="tools/call",
                                    params=types.CallToolRequestParams(name=name, arguments=args))
        res = (await server.request_handlers[types.CallToolRequest](req)).root
        report.update(kind=kind, tool=name, key=key, members=members, isError=res.isError,
                      text=res.content[0].text)
    print(json.dumps(report))

asyncio.run(main())
'''


def _child_env(tmp_path) -> dict:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PROXY_URL": "http://127.0.0.1:9",
        "OTO_CAN_CREATE_AGENTS": "true",
        "OTO_CAN_MANAGE_AGENT": "true",
        "OTO_CAN_EDIT_AGENT": "true",
        "OTO_SCOPE": "user",
        "OTO_USERNAME": "dev-admin",
    }


def _load_reference():
    spec = importlib.util.spec_from_file_location("tool_args_reference", REFERENCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("mcp", sorted(SERVERS))
def test_each_server_carries_the_reference_copy(mcp):
    folder = CUSTOM_MCPS / mcp
    if not folder.is_dir():
        pytest.skip(f"{mcp} is not in this checkout")
    assert (folder / "_tool_args.py").read_bytes() == REFERENCE.read_bytes(), (
        f"{mcp}/_tool_args.py drifted from mcps/_shared/tool_args.py — copy it again")


def test_every_server_that_checks_is_listed_here():
    wired = {p.parent.name for p in CUSTOM_MCPS.glob("*/_tool_args.py")}
    assert wired <= set(SERVERS)


@pytest.mark.parametrize("mcp", sorted(SERVERS))
def test_each_server_refuses_with_the_argument_named(mcp, tmp_path):
    folder = CUSTOM_MCPS / mcp
    if not folder.is_dir():
        pytest.skip(f"{mcp} is not in this checkout")
    r = subprocess.run([sys.executable, "-c", CHILD, str(folder), SERVERS[mcp]],
                       capture_output=True, text=True, timeout=120, env=_child_env(tmp_path))
    assert r.returncode == 0, r.stderr[-2000:]
    report = json.loads(r.stdout.strip().splitlines()[-1])
    assert report["tools"], report
    assert report["formats"] == []  # no `format` keyword: leaving the checker out changes nothing
    if report["kind"] == "passthrough":
        assert report["check"] is True and report["tool"] in report["recorded"]
        return
    assert report["isError"] is True
    text = report["text"]
    assert text.startswith(f"Invalid arguments for {report['tool']}: "), text
    assert text.endswith(f"Call {report['tool']} again with corrected arguments."), text
    if report["kind"] == "enum":
        allowed = ", ".join(json.dumps(m) for m in report["members"])
        assert f'{report["key"]} must be one of {allowed} (got "not-a-member")' in text, text
    else:
        assert f"'{report['key']}' is a required property" in text, text


def test_a_near_miss_reaches_the_tool_as_its_member(monkeypatch):
    """schedules-mcp through the SDK: 'Manual' and ' manual ' are taken as
    'manual'; 'manualish' is refused before any request is made."""
    from mcp import types

    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:9")
    mod = load_mcp_server(CUSTOM_MCPS / "schedules-mcp")
    sent = []

    async def _post(path, body, headers=None):
        sent.append(body)
        return {"task_id": "t1"}

    monkeypatch.setattr(mod, "_post", _post)

    async def call(mode):
        req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name="create_scheduled_task",
            arguments={"name": "n", "prompt": "p", "schedule": "0 9 * * *", "notification_mode": mode}))
        return (await mod.server.request_handlers[types.CallToolRequest](req)).root

    for mode in ("Manual", " manual "):
        res = asyncio.run(call(mode))
        assert res.isError is False and "Notification mode: manual" in res.content[0].text
    assert [b["notification_mode"] for b in sent] == ["manual", "manual"]
    res = asyncio.run(call("manualish"))
    assert res.isError is True and len(sent) == 2
    assert 'notification_mode must be one of "auto", "manual", "none" (got "manualish")' in res.content[0].text


def _schema(**props) -> dict:
    return {"type": "object", "properties": props}


def _args_check(schema, arguments, name="tool"):
    from mcp import types

    ta = _load_reference()
    reg = ta.ToolArgs()
    reg.listed([types.Tool(name=name, description="", inputSchema=schema)])
    return reg.check(name, arguments)


def test_the_fold_takes_case_and_separators():
    schema = _schema(mode={"type": "string", "enum": ["one_time", "recurring"]})
    for raw in ("One_Time", "one-time", " ONE TIME ", "one time"):
        args, refusal = _args_check(schema, {"mode": raw})
        assert refusal is None and args == {"mode": "one_time"}, raw


def test_the_fold_walks_nested_objects_and_array_items():
    schema = _schema(
        notify={"type": "object", "properties": {"severity": {"type": "string", "enum": ["info", "warning"]}}},
        layers={"type": "array", "items": {"type": "string", "enum": ["claude-code-cli", "codex-cli"]}},
    )
    original = {"notify": {"severity": "WARNING"}, "layers": ["Codex-CLI", "claude-code-cli"]}
    args, refusal = _args_check(schema, original)
    assert refusal is None
    assert args == {"notify": {"severity": "warning"}, "layers": ["codex-cli", "claude-code-cli"]}
    assert original == {"notify": {"severity": "WARNING"}, "layers": ["Codex-CLI", "claude-code-cli"]}


def test_the_fold_never_guesses():
    ambiguous = _schema(x={"type": "string", "enum": ["A", "a"]})
    _, refusal = _args_check(ambiguous, {"x": "à"})
    assert refusal is not None
    args, refusal = _args_check(ambiguous, {"x": "a"})
    assert refusal is None and args == {"x": "a"}  # an exact member stays itself
    numbers = _schema(rate={"type": "integer", "enum": [8000, 16000]})
    _, refusal = _args_check(numbers, {"rate": "8000"})
    assert refusal is not None
    blank = _schema(mode={"type": "string", "enum": ["interactive", ""]})
    assert _args_check(blank, {"mode": "  "})[0] == {"mode": ""}
    assert _args_check(blank, {"mode": "Interactive-"})[1] is not None


def test_the_refusal_names_every_problem_the_sdk_would_have_led_with_first():
    import jsonschema

    schema = {
        "type": "object",
        "properties": {
            "surface": {"type": "string", "enum": ["chat", "task"]},
            "timeout_seconds": {"type": "integer"},
            "operations": {"type": "array", "items": {"type": "object", "properties": {
                "op": {"type": "string", "enum": ["add", "remove"]}}}},
        },
        "required": ["prompt"],
    }
    arguments = {"surface": "chats", "timeout_seconds": "ten", "operations": [{"op": "add"}, {"op": "drop"}]}
    _, refusal = _args_check(schema, arguments, name="delegate")
    text = refusal.content[0].text
    assert refusal.isError is True
    best = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(schema).iter_errors(arguments))
    assert best.message == "'prompt' is a required property"
    assert text.startswith("Invalid arguments for delegate: 'prompt' is a required property; ")
    assert 'surface must be one of "chat", "task" (got "chats")' in text
    assert "timeout_seconds: 'ten' is not of type 'integer'" in text
    assert 'operations[1].op must be one of "add", "remove" (got "drop")' in text


def test_an_unlisted_tool_and_a_listing_that_replaces_the_record():
    from mcp import types

    ta = _load_reference()
    reg = ta.ToolArgs()
    assert reg.check("anything", {"x": 1}) == ({"x": 1}, None)
    assert reg.check("anything", "not a dict") == ({}, None)
    strict = {"type": "object", "properties": {}, "required": ["a"]}
    reg.listed([types.Tool(name="one", description="", inputSchema=strict)])
    assert reg.check("one", {})[1] is not None
    reg.listed([types.Tool(name="two", description="", inputSchema=strict)])
    assert reg.check("one", {})[1] is None  # no longer listed: unchecked, as with the SDK


def test_a_schema_jsonschema_cannot_read_raises_as_the_sdk_did():
    import jsonschema

    with pytest.raises(jsonschema.SchemaError):
        _args_check({"type": "object", "properties": {"x": {"type": "nonsense"}}}, {"x": 1})
