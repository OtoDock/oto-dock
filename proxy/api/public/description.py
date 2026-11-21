"""The description (OpenAPI) of the public routes, generated from the listed
routes alone: never the app's whole route table, which stays unpublished
(``app.py`` serves no schema). ``scripts/generate-openapi.py`` writes it to
``openapi.json`` beside this file, the install serves that file to a key or a
signed-in person, and the docs site publishes it at a release.
"""

from __future__ import annotations

from fastapi.openapi.utils import get_openapi

TITLE = "OtoDock API"
VERSION = "1.0"
SUMMARY = (
    "Use your OtoDock from outside with a personal API key: your agents, chats, tasks, files and "
    "notifications. A key acts as you, on the agents it names, with the permissions you gave it, "
    "and never does more than you can. Make a key in your settings. Send it as "
    "`Authorization: Bearer otok_...`."
)


def public_routes() -> list:
    from api.public import chats, files, me, notifications, tasks
    routes: list = []
    for module in (me, chats, tasks, files, notifications):
        routes.extend(module.router.routes)
    return routes


def build() -> dict:
    """The document, the same for the same routes (no dates, no version of
    the install)."""
    doc = get_openapi(title=TITLE, version=VERSION, description=SUMMARY, routes=public_routes())
    doc.setdefault("components", {}).setdefault("securitySchemes", {})["key"] = {
        "type": "http", "scheme": "bearer", "description": "A personal API key (otok_...)"}
    doc["security"] = [{"key": []}]
    return doc
