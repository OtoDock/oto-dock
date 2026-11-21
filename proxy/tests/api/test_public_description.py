"""The description of the public API: generated from the listed routes alone,
typed, the committed file what the generator writes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIRoute

from api.public import description
from auth import api_key_endpoints as endpoints

COMMITTED = Path(description.__file__).with_name("openapi.json")
# Their answer is not one JSON shape: the file's bytes, a stream of events.
UNTYPED = {"/v1/api/agents/{agent}/files/{path:path}", "/v1/api/runs/{run_id}/stream"}


def test_the_committed_file_is_what_the_generator_writes():
    assert json.loads(COMMITTED.read_text(encoding="utf-8")) == description.build(), (
        "run scripts/generate-openapi.py")


def test_the_description_holds_the_public_routes_only_each_with_the_keys_scheme():
    doc = description.build()
    assert doc["security"] == [{"key": []}]
    assert doc["components"]["securitySchemes"]["key"]["scheme"] == "bearer"
    for path, operations in doc["paths"].items():
        assert path.startswith(endpoints.PUBLIC_PREFIX), path
        for method in operations:
            sample = path.replace("{path}", "a/b").replace("{", "").replace("}", "")
            assert endpoints.key_route(method, sample) is not None, (method, path)
    text = json.dumps(doc)
    for word in ("/v1/admin", "/v1/user-api-keys", "/auth/", "/v1/hooks"):
        assert word not in text, word


def test_every_public_route_has_a_typed_answer():
    for route in description.public_routes():
        assert isinstance(route, APIRoute)
        if route.include_in_schema and route.path not in UNTYPED:
            assert route.response_model is not None, route.path
