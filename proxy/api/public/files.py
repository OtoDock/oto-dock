"""The person's files on an agent through a key (scope ``files``): the tree,
a read and a text write.

A key reaches the person's own folder (``users/<them>/``), the agent's
``workspace/`` and, to read, its ``knowledge/``: never ``config/`` and never
another person's folder, an admin's key included. Its role on the agent is
at most the editor tier (``files_person``), and the existing handlers judge
each path, the resolved one included.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.public.common import files_person, key_user, record, require_agent
from auth.providers import UserContext
from core import layout
from storage import database as task_store
from storage.pg import run_db

router = APIRouter(tags=["files"])

_DIR = "dir"  # a tree node's type


class FileWrite(BaseModel):
    content: str = Field(..., description="The file's text (UTF-8)")


class FileNode(BaseModel):
    name: str
    type: str = Field(..., description="file or dir")
    path: str
    size: int
    modified: str
    children: list[FileNode]


class Tree(BaseModel):
    tree: list[FileNode]


class Saved(BaseModel):
    status: str
    path: str


async def _scopes(u: UserContext) -> tuple[str, ...]:
    username = await run_db(task_store.get_username_by_sub, u.sub) or ""
    own = layout.user_rel(username) if username else ""
    return tuple(p for p in (own, layout.WORKSPACE, layout.KNOWLEDGE) if p)


def _inside(path: str, scopes: tuple[str, ...]) -> bool:
    path = path.strip("/")
    return any(path == s or path.startswith(s + "/") for s in scopes)


@router.get("/v1/api/agents/{agent}/files", response_model=Tree)
async def tree(agent: str, u: UserContext = Depends(key_user)) -> Response:
    """The agent's files the key reaches, as a tree."""
    from api.agents.files import list_agent_files
    require_agent(u, agent)
    answer = await list_agent_files(agent, user=files_person(u, agent))
    scopes = await _scopes(u)
    body = json.loads(answer.body)

    def keep(node: dict) -> dict | None:
        path = node.get("path") or ""
        if _inside(path, scopes):
            return node
        if node.get("type") == _DIR and any(s.startswith(path + "/") for s in scopes):
            children = [k for k in (keep(c) for c in node.get("children") or []) if k]
            return {**node, "children": children} if children else None
        return None

    kept = [k for k in (keep(n) for n in body.get("tree") or []) if k]
    return Response(json.dumps({"tree": kept}, ensure_ascii=False), media_type="application/json")


@router.get("/v1/api/agents/{agent}/files/{path:path}",
            responses={200: {"description": "The file: JSON with its text, or its bytes"}})
async def read_file(agent: str, path: str, download: bool = False,
                    u: UserContext = Depends(key_user)):
    """A file's content: text as JSON, or the bytes with ``download``."""
    from api.agents.files import read_agent_file
    require_agent(u, agent)
    if not _inside(path, await _scopes(u)):
        raise HTTPException(403, "An API key reaches your own folder, the workspace and the "
                                 "knowledge only.")
    return await read_agent_file(agent, path, download=download, user=files_person(u, agent))


@router.put("/v1/api/agents/{agent}/files/{path:path}", response_model=Saved)
async def write_file(agent: str, path: str, req: FileWrite,
                     u: UserContext = Depends(key_user)) -> Saved:
    """Write a text file in your own folder or the workspace."""
    from api.agents.files import WriteFileRequest, write_agent_file
    require_agent(u, agent)
    scopes = await _scopes(u)
    if not _inside(path, tuple(s for s in scopes if s != layout.KNOWLEDGE)):
        raise HTTPException(403, "An API key writes in your own folder and the workspace only.")
    saved = await write_agent_file(agent, path, WriteFileRequest(content=req.content),
                                   user=files_person(u, agent))
    await run_db(record, u, "file", saved["path"], label=saved["path"], agent=agent)
    return Saved(status=saved["status"], path=saved["path"])
