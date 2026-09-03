"""Route-registration pins for side-effect-imported section modules.

``api.agents.agents`` registers its section routes (discovery, files,
user-context, activity, knowledge libraries) by IMPORTING sibling modules
whose ``@router`` decorators attach to the shared ``api.agents._router``.
Those imports look unused to a linter; on 2026-08-28 a ``ruff --fix``
deleted the knowledge-libraries one and every knowledge-library endpoint
silently became a 404 (the suite imports endpoint functions directly, so
nothing noticed). This test inspects the router the app mounts, so a
dropped section import fails loudly here.
"""

import api.agents.agents  # noqa: F401 — triggers the section imports
from api.agents._router import router


def _paths() -> set[str]:
    return {getattr(r, "path", "") for r in router.routes}


def test_knowledge_library_routes_registered():
    paths = _paths()
    for p in (
        "/v1/knowledge-libraries",
        "/v1/agents/{name}/knowledge-attachments",
        "/v1/agents/{name}/knowledge-library",
        "/v1/agents/{name}/knowledge-attachments/{source}",
    ):
        assert p in paths, f"missing route {p} — section import dropped?"


def test_other_section_routes_registered():
    paths = _paths()
    # One representative path per side-effect-imported section module.
    for p in (
        "/v1/agents/{name}/files/{path:path}",     # files
        "/v1/agents/{name}/info",                  # discovery
        "/v1/agents/{name}/user-context",          # user_context
        "/v1/agents/activity",                     # activity
    ):
        assert p in paths, f"missing route {p} — section import dropped?"
