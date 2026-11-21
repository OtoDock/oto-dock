"""Every proxy route the platform's tools and hooks call with a session token
is on the session token's route list (``auth/external_endpoints.SESSION_ROUTES``),
and every route a first-party tool calls names its tools in the limits table
(``ROUTE_TOOLS``), so a limit on the tool holds on the route.

The sources are swept for the ``/v1/`` paths they name in a call (an AST walk
over call arguments and the constants and f-strings they are built from,
never a docstring or a comment), so a caller added without a row fails here
before a walk finds it refused. ``satellite/terminal`` is left out: the
``otodock`` verbs use a personal key and refuse to run inside a session.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from auth.external_endpoints import (
    ROUTE_TOOLS,
    SESSION_ROUTES,
    is_session_endpoint_allowed,
    route_tools,
)
from tests._paths import REPO_ROOT

_UUID = "0123abcd-0123-4abc-8abc-0123456789ab"
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_SOURCES = (
    *sorted((REPO_ROOT / "mcps" / "custom").glob("*/*.py")),
    *sorted((REPO_ROOT / "proxy" / "hooks").glob("*.py")),
    *sorted((REPO_ROOT / "satellite" / "sessions").glob("*.py")),
)
# The routes a tool reaches on a token of another kind, or that are no
# session token's business: the broker's fetch, the render principal's
# check, and the public API a skill's text tells a person about.
_OTHER_KINDS = ("/v1/hooks/mcp-credentials", "/v1/api/", "/v1/internal/render/")
# The routes of the list no first-party tool calls with a session token: the
# Docker tools' callbacks (a callback token, judged before the limits), the
# credential gateway, and a server's own read at its start.
_NO_TOOLS = ("/v1/hooks/resolve-path", "/v1/hooks/file-written", "/v1/hooks/document-preview",
             "/v1/mcp-gateway/", "/v1/conversations/kind")
_MCP_SOURCES = [s for s in _SOURCES if "mcps" in s.parts]
# The words a row's alternation offers for a placeholder in a path's last
# segment (``f"/v1/hooks/apps/{op}"``).
_WORDS = sorted({
    w for pattern, _m in SESSION_ROUTES
    for group in re.findall(r"\(([a-z0-9|_-]+)\)", pattern.pattern)
    for w in group.split("|")
})


def _text_of(node: ast.AST) -> str | None:
    """A path-shaped string a call argument names, with every piece that is
    not a constant as a ``{}`` placeholder; None for a value that is no
    string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out += part.value
            else:
                out += "{}"
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text_of(node.left), _text_of(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    return None


def _paths_named(source: Path) -> list[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in [*node.args, *(k.value for k in node.keywords)]:
            text = _text_of(arg)
            if text is None:
                continue
            # The path may follow a base (``f"{proxy_url}/v1/..."``) and
            # carry a query string.
            m = re.search(r"/v1/[^\s\"'?]*", text)
            if m is None:
                continue
            # A placeholder written for a person (``<app_id>``) is one too.
            found.append(re.sub(r"<[^>/]+>", "{}", m.group(0)).rstrip("/") or m.group(0))
    return found


def _candidates(path: str) -> list[str]:
    """The concrete paths a named path may be: each placeholder an id, and,
    for a placeholder in the last segment, each word a row offers there."""
    if "{}" not in path:
        return [path]
    filled = path.replace("{}", _UUID)
    head, _sep, last = path.rpartition("/")
    if last == "{}" and head:
        return [filled, *(f"{head.replace('{}', _UUID)}/{w}" for w in _WORDS)]
    return [filled]


def _route_regexes() -> list:
    """The route table's own matchers: a named path is the proxy's when one
    of them takes it (a vendor's ``/v1/images/generations`` is nobody's)."""
    from fastapi.routing import APIRoute

    from app import app

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                # The dashboard shell's catch-all takes every path: not a route
                # a tool calls.
                if r.path.startswith("/v1/"):
                    yield r.path_regex
            elif getattr(r, "original_router", None) is not None:
                yield from walk(r.original_router.routes)
            elif hasattr(r, "routes"):
                yield from walk(r.routes)
    return list(walk(app.routes))


_ROUTES = _route_regexes()


def _is_a_route(path: str) -> bool:
    return any(rx.match(c) for c in _candidates(path) for rx in _ROUTES)


def _on_list(path: str) -> bool:
    return any(is_session_endpoint_allowed(m, c, _UUID) for m in _METHODS for c in _candidates(path))


def _has_tools(path: str) -> bool:
    return any(route_tools(m, c) for m in _METHODS for c in _candidates(path))


@pytest.mark.parametrize("source", _SOURCES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_path_a_tool_or_hook_names_is_on_the_list(source):
    named = [p for p in _paths_named(source) if not p.startswith(_OTHER_KINDS) and _is_a_route(p)]
    off = sorted({p for p in named if not _on_list(p)})
    assert not off, f"{source.relative_to(REPO_ROOT)} names routes off the session list: {off}"


@pytest.mark.parametrize("source", _MCP_SOURCES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_route_a_tool_names_has_its_tools_row(source):
    """A first-party tool's route without a row in ``ROUTE_TOOLS`` fails
    here: its limit would hold on the tool call and not on the route."""
    named = [p for p in _paths_named(source)
             if not p.startswith(_OTHER_KINDS + _NO_TOOLS) and _is_a_route(p) and _on_list(p)]
    off = sorted({p for p in named if not _has_tools(p)})
    assert not off, f"{source.relative_to(REPO_ROOT)} names routes with no tools row: {off}"


def test_every_tools_row_names_a_tool_of_a_first_party_server():
    """A row's server key is a manifest's under ``mcps/custom`` (its
    ``server_name``, else its name) and its function a name that server's
    sources carry, so a renamed tool or server fails here."""
    import json
    folders = {}
    for m in (REPO_ROOT / "mcps" / "custom").glob("*/manifest.json"):
        doc = json.loads(m.read_text(encoding="utf-8"))
        folders[doc.get("server_name") or doc["name"]] = m.parent
    for pattern, _methods, tools in ROUTE_TOOLS:
        assert tools, pattern.pattern
        for name, function in tools:
            assert name in folders, (pattern.pattern, name)
            text = "".join(p.read_text(encoding="utf-8") for p in folders[name].glob("*.py"))
            assert f'"{function}"' in text, (pattern.pattern, name, function)


def test_the_sweep_sees_the_callers():
    """The sweep reads real calls: the hooks' routes and the tools' are found."""
    hooks = {p for src in _SOURCES if src.parent.name == "hooks" for p in _paths_named(src)}
    assert {"/v1/hooks/permission", "/v1/hooks/stop", "/v1/hooks/tool-result",
            "/v1/hooks/cli-credential"} <= hooks
    tools = {p for src in _SOURCES if "mcps" in src.parts for p in _paths_named(src)}
    assert "/v1/tasks/scheduled" in tools and "/v1/internal/memory/op" in tools
    assert any(p.startswith("/v1/hooks/apps/") for p in tools)
