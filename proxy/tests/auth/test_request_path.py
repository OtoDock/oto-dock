"""``auth.request_path.has_traversal`` — the one request-path traversal guard
(core-seams phase 3): what it refuses, what it lets through and why that is
inert, and the satellite's byte twin."""

from __future__ import annotations

import ast

import pytest

from auth.request_path import has_traversal
from tests._paths import REPO_ROOT

REFUSED = [
    "/v1/x/../y", "/v1/./y", "..", ".", "/..", "a/..", "../a", "/v1/x/.",
    "/v1/%2e%2e/y", "/v1/%2E%2E/y", "/v1/a%2fb", "/v1/a%2Fb", "/v1/a%5cb",
    "/v1/a\\b", "..\\x",
    "/v1/a%00b", "/v1/a\x00b",
]
# Inert by construction: a double encoding stays text here and is caught on
# the decoded axis (Starlette decodes ``request.url.path`` once; the app
# proxy judges the decoded argument too); ``..;`` / ``...`` / ``x.`` are not
# dot segments and httpx does not collapse them; a fullwidth dot is an
# ordinary name; an empty segment fails every anchored allowlist regex.
ACCEPTED = [
    "/v1/hooks/permission", "/v1/tasks/x.y", "/v1/tasks/x.", "/v1/tasks/...",
    "/v1/tasks/..;/x", "/v1/tasks/a//b", "/v1/tasks/．．/x",
    "/v1/tasks/%252e%252e/x", "", "/",
]


@pytest.mark.parametrize("path", REFUSED)
def test_refused(path):
    assert has_traversal(path), path


@pytest.mark.parametrize("path", ACCEPTED)
def test_accepted(path):
    assert not has_traversal(path), path


def _body_shape(path, name: str) -> str:
    """The function's AST with its docstring dropped and its parameter
    renamed — what the release gate's twin rule compares."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            param = node.args.args[0].arg

            class _Rename(ast.NodeTransformer):
                def visit_Name(self, n):
                    return ast.copy_location(ast.Name(id="_p0" if n.id == param else n.id, ctx=n.ctx), n)
            return ast.dump(_Rename().visit(ast.Module(body=body, type_ignores=[])))
    raise AssertionError(f"{path}: no {name}")


def test_the_satellite_tunnel_carries_the_same_body():
    """``satellite/transport/http_tunnel.py:_has_traversal`` is a byte twin
    (a released satellite runs its own copy): same verdicts, same shape."""
    ours = _body_shape(REPO_ROOT / "proxy" / "auth" / "request_path.py", "has_traversal")
    twin = _body_shape(REPO_ROOT / "satellite" / "transport" / "http_tunnel.py", "_has_traversal")
    assert ours == twin


def test_confinement_judges_the_path_the_router_matches(monkeypatch):
    """A decoded ``?`` / ``#`` cut ``request.url.path`` short: the master key
    on ``/v1/sessions/warmup%3F/abort`` was judged as the allowed warmup
    route while the router ran the session's abort."""
    from fastapi.testclient import TestClient

    import config
    from app import app
    monkeypatch.setattr(config, "API_KEY", "test-master-key")
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-master-key"}
    for path in ("/v1/sessions/warmup%3F/abort", "/v1/sessions/warmup%23/abort"):
        r = client.post(path, headers=headers)
        assert r.status_code == 403, (path, r.status_code, r.text)
        assert "service key" in r.json()["detail"]
