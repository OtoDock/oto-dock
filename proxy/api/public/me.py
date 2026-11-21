"""The key itself, the agents it reaches, the description of the public
routes and the ``otodock`` command as one file.

``GET /v1/api/openapi.json`` and ``GET /v1/api/otodock.py`` answer a key or
a signed-in person (the middleware refuses everyone else); the install
serves no route map to a visitor without an account.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from api.public.common import key_user, reaches
from auth.providers import UserContext, get_current_user, require_auth
from storage.pg import run_db

router = APIRouter(tags=["key"])

_HERE = Path(__file__).resolve().parent
DESCRIPTION = _HERE / "openapi.json"
COMMAND = _HERE.parents[2] / "satellite" / "terminal" / "otodock_api.py"


class KeyView(BaseModel):
    name: str
    scopes: list[str]
    agents: list[str]
    expires_at: str | None
    run_without_asking: bool


class MeAnswer(BaseModel):
    name: str
    email: str
    key: KeyView


class AgentView(BaseModel):
    agent: str
    name: str
    description: str
    role: str
    shared_chats: bool


class AgentsAnswer(BaseModel):
    agents: list[AgentView]


@router.get("/v1/api/me", response_model=MeAnswer)
async def me(u: UserContext = Depends(key_user)) -> MeAnswer:
    """Who the key acts as, and what it may do."""
    from storage.identity import api_key_store
    row = await run_db(api_key_store.get_user_api_key, u.api_key_id) or {}
    return MeAnswer(
        name=u.display_name or u.name, email=u.email,
        key=KeyView(name=u.api_key_name, scopes=list(u.api_key_scopes),
                    agents=list(u.api_key_agents), expires_at=row.get("expires_at"),
                    run_without_asking=u.api_key_runs_unasked))


@router.get("/v1/api/agents", response_model=AgentsAnswer)
async def agents(u: UserContext = Depends(key_user)) -> AgentsAnswer:
    """The agents the key reaches, with the person's role on each."""
    from core.session.visibility import is_shared_only
    from storage.agents import agent_store

    def _job() -> list[AgentView]:
        out = []
        for row in agent_store.get_all_agents():
            slug = row.get("slug") or ""
            if not reaches(u, slug):
                continue
            out.append(AgentView(agent=slug, name=row.get("display_name") or slug,
                                 description=row.get("description") or "",
                                 role=u.acting_role(slug), shared_chats=is_shared_only(slug)))
        return out

    return AgentsAnswer(agents=await run_db(_job))


@router.get("/v1/api/openapi.json", include_in_schema=False)
async def description(user: UserContext | None = Depends(get_current_user)) -> Response:
    require_auth(user)
    if not DESCRIPTION.is_file():
        raise HTTPException(404, "This install has no description of its public routes.")
    return FileResponse(DESCRIPTION, media_type="application/json")


@router.get("/v1/api/otodock.py", include_in_schema=False)
async def command(user: UserContext | None = Depends(get_current_user)) -> Response:
    require_auth(user)
    if not COMMAND.is_file():
        raise HTTPException(404, "This install does not carry the otodock command.")
    return FileResponse(COMMAND, media_type="text/x-python", filename="otodock.py")
