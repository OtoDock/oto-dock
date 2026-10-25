"""Shared helpers for the agent-management API package."""

import json
from pathlib import Path

from fastapi import HTTPException, Response
from fastapi.encoders import jsonable_encoder

import config
from storage.agents import agent_store


def _get_agent_dir(name: str) -> Path:
    if not agent_store.agent_exists(name):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return config.get_agent_dir(name)


def json_body(payload) -> bytes:
    """The bytes ``JSONResponse`` would write for ``payload``, made where
    the payload is built (an executor job) so the loop never walks it.
    FastAPI's encoder runs only as the ``default`` hook, for a value
    ``json`` cannot write itself (a datetime, a Path, a Decimal, an Enum, a
    model); on plain rows it never runs. Keys must be strings: the hook is
    not called for keys, where the encoder would convert them."""
    return json.dumps(payload, default=jsonable_encoder, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def json_response(body: bytes) -> Response:
    """A pre-encoded JSON answer (``json_body``)."""
    return Response(content=body, media_type="application/json")
