"""The checks MCP (mcps/custom/checks-mcp): the tool set per session shape
(everyone; personal sessions; managers with a human present), the manifest
and the routes each tool calls.

venv/bin/python -m pytest tests/mcp/test_checks_mcp.py -q
"""

import asyncio
import json
import os

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "checks-mcp"
_KEYS = ("CHECKS_MCP_AGENT", "OTO_AGENT_NAME", "OTO_ROLE", "OTO_CAN_MANAGE_AGENT", "OTO_TASK_TYPE", "OTO_USERNAME",
         "PROXY_URL", "PROXY_API_KEY")


def _load(env: dict[str, str]):
    saved = {k: os.environ.get(k) for k in _KEYS}
    try:
        for k in _KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return load_mcp_server(_MCP_DIR)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _spy(mod):
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if path.endswith("/checks") and method == "GET":
            return {"checks": [{"ref": "agent:coding", "name": "coding", "sections": ["judge"],
                                "mandatory": True, "applies": ["chats"], "condition": {},
                                "rounds": 2, "description": "d"}]}
        if path == "/v1/checks/attached":
            return {"attached": ["user:mine"], "mandatory": ["agent:coding"]}
        if path == "/v1/checks/run":
            return {"check": "coding", "status": "fail", "score": 0.3, "text": "[OtoDock check] ..."}
        return {"ref": "agent:x", "sections": ["judge"], "mandatory": False, "attached": ["agent:x"]}
    mod._request = fake_request
    return calls


def test_the_tool_set_by_session_shape():
    everyone = {"list_checks", "attach_check", "detach_check", "run_check"}
    mod = _load({"CHECKS_MCP_AGENT": "a", "OTO_ROLE": "viewer", "OTO_USERNAME": ""})
    assert mod.ENABLED_TOOLS == everyone
    mod = _load({"CHECKS_MCP_AGENT": "a", "OTO_ROLE": "editor", "OTO_USERNAME": "alice"})
    assert mod.ENABLED_TOOLS == everyone | {"create_private_check", "delete_private_check"}
    mod = _load({"CHECKS_MCP_AGENT": "a", "OTO_ROLE": "manager", "OTO_CAN_MANAGE_AGENT": "true", "OTO_USERNAME": "alice"})
    assert {"set_check", "delete_check"} <= mod.ENABLED_TOOLS
    # A task fire has no human present: no manager tools.
    mod = _load({"CHECKS_MCP_AGENT": "a", "OTO_ROLE": "manager", "OTO_CAN_MANAGE_AGENT": "true", "OTO_USERNAME": "alice",
                 "OTO_TASK_TYPE": "scheduled"})
    assert not ({"set_check", "delete_check"} & mod.ENABLED_TOOLS)
    assert "detach_check" not in mod.ENABLED_TOOLS and "attach_check" in mod.ENABLED_TOOLS
    assert set(mod._TOOL_SCHEMAS) == set(mod._TOOL_HANDLERS)
    tools = asyncio.run(mod.list_tools())
    assert {t.name for t in tools} == mod.ENABLED_TOOLS


def test_the_manifest():
    m = json.loads((_MCP_DIR / "manifest.json").read_text())
    assert m["category"] == "core" and m["exclude_from"] == ["meeting", "phone", "external"]
    assert m["skills"][0]["id"] == "checks" and (_MCP_DIR / m["skills"][0]["file"]).is_file()
    assert m["agent_env"]["CHECKS_MCP_AGENT"] == "${agent_name}"
    # Changing or removing the agent's checks prompts in every mode (a
    # mandatory check is what judges the session asking to weaken it).
    tiers = {r["tool"]: r["tier"] for r in m["permissions"]["rules"]}
    assert tiers["set_check"] == tiers["delete_check"] == "critical"
    skill = (_MCP_DIR / "skills" / "checks" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: checks\n") and "contract" not in skill.lower()


def test_the_tools_call_their_routes():
    mod = _load({"CHECKS_MCP_AGENT": "pa", "OTO_ROLE": "manager", "OTO_CAN_MANAGE_AGENT": "true", "OTO_USERNAME": "alice"})
    calls = _spy(mod)
    out = asyncio.run(mod.call_tool("list_checks", {}))[0].text
    assert "agent:coding" in out and "Attached to this chat: user:mine" in out
    asyncio.run(mod.call_tool("attach_check", {"name": "coding"}))
    asyncio.run(mod.call_tool("detach_check", {"name": "coding"}))
    out = asyncio.run(mod.call_tool("run_check", {"name": "coding"}))[0].text
    assert out.startswith("coding: fail (score 0.3)")
    asyncio.run(mod.call_tool("set_check", {"name": "coding", "doc": {"judge": {"rubric": "r"}},
                                            "script": None}))
    asyncio.run(mod.call_tool("create_private_check", {"name": "mine", "doc": json.dumps({"schema": {}})}))
    asyncio.run(mod.call_tool("delete_check", {"name": "coding"}))
    asyncio.run(mod.call_tool("delete_private_check", {"name": "mine"}))
    paths = [(m, p) for m, p, _ in calls]
    assert ("GET", "/v1/agents/pa/checks") in paths and ("GET", "/v1/checks/attached") in paths
    assert ("POST", "/v1/checks/attach") in paths and ("POST", "/v1/checks/detach") in paths
    assert ("POST", "/v1/checks/run") in paths
    assert ("PUT", "/v1/agents/pa/checks/coding") in paths
    assert ("PUT", "/v1/agents/pa/user-checks/mine") in paths
    assert ("DELETE", "/v1/agents/pa/checks/coding") in paths
    assert ("DELETE", "/v1/agents/pa/user-checks/mine") in paths
    set_body = next(b for m, p, b in calls if p == "/v1/agents/pa/checks/coding" and m == "PUT")
    assert set_body["doc"]["name"] == "coding" and set_body["doc"]["judge"]["rubric"] == "r"
    # A tool outside the set is refused, not called.
    mod2 = _load({"CHECKS_MCP_AGENT": "pa", "OTO_ROLE": "viewer"})
    out = asyncio.run(mod2.call_tool("set_check", {"name": "x", "doc": {}}))[0].text
    assert "not available" in out
