"""An agent's set of a person's own accounts of a tool, written from the
Integrations page: add an account to the set or change the reach the set
gives it, take one out, make one the set's default (the account a chat that
chose none runs on). Dashboard only, with the gates of the agent-binding
routes in ``api/mcp/credentials.py``, whose router includes this one
(``proxy/app.py`` mounts only that one). ``credentials.py`` imports this
module, so the handlers import it in turn when they run.
"""

from __future__ import annotations

import asyncio
import functools

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import github_reach
from auth.providers import UserContext, get_current_user, require_user
from storage.identity import credential_store

router = APIRouter(dependencies=[Depends(require_user)])


class AgentAccountRequest(BaseModel):
    agent_name: str
    account_label: str
    # The person's own GitHub reach for the agent with this account
    # (``auth/github_reach``): absent = keep what the account's row holds,
    # or the whole account on a new row.
    repositories: list[str] | None = Field(default=None, max_length=github_reach.MAX_REPOSITORIES)
    read_only: bool | None = None
    make_default: bool = False


class AgentDefaultAccountRequest(BaseModel):
    account_label: str


def _not_in_set(mcp_name: str, agent_name: str, account_label: str) -> HTTPException:
    return HTTPException(
        404, f"Account '{account_label}' is not one of the {mcp_name} accounts of agent {agent_name!r}.",
    )


@router.put("/v1/users/me/integrations/{mcp_name}/agent-accounts")
async def put_agent_account(
    mcp_name: str,
    body: AgentAccountRequest,
    user: UserContext = Depends(get_current_user),
):
    """Add one of the caller's accounts to the agent's set, or change the
    reach the set gives it. The first account of a set is its default;
    ``make_default`` moves the default to it. The gates are the 1.9.1
    PUT's (``credentials._own_choice_gates``), the reach is judged on this
    account's own row (``credentials._check_own_reach``). Answers
    ``{status, repositories, read_only, is_default}`` of the row as written.
    """
    from api.mcp import credentials as credentials_api
    await credentials_api._own_choice_gates(user, mcp_name, body.agent_name, body.account_label)
    await credentials_api._check_own_reach(body, mcp_name, user, replaces_set=False)
    ok = await asyncio.to_thread(
        functools.partial(
            credential_store.add_account_to_agent,
            user.sub, mcp_name, body.agent_name, body.account_label,
            repositories=body.repositories, read_only=body.read_only,
            make_default=body.make_default,
        ),
    )
    if not ok:
        raise HTTPException(404, f"Account '{body.account_label}' not found for {mcp_name}")
    row = await asyncio.to_thread(
        credential_store.get_account_agent_binding, user.sub, mcp_name, body.agent_name,
        body.account_label,
    )
    return {
        "status": "ok",
        "repositories": list(row.repositories) if row else [],
        "read_only": row.read_only if row else False,
        "is_default": row.is_default if row else False,
    }


@router.delete("/v1/users/me/integrations/{mcp_name}/agent-accounts/{agent_name}")
async def delete_agent_account(
    mcp_name: str,
    agent_name: str,
    account_label: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """Take one account out of the agent's set, with the reach it carried
    and any chat's choice of it. When it was the default, the oldest
    account left becomes the default; the last one out leaves the agent on
    the person's default account. The assigned-agent gate only, as the
    1.9.1 DELETE (an MCP turned off since is still cleared)."""
    from api.mcp import credentials as credentials_api
    await credentials_api._assigned_gate(user, agent_name)
    gone = await asyncio.to_thread(
        credential_store.remove_account_agent_binding, user.sub, mcp_name, agent_name, account_label,
    )
    if not gone:
        raise _not_in_set(mcp_name, agent_name, account_label)
    return {"status": "ok"}


@router.put("/v1/users/me/integrations/{mcp_name}/agent-accounts/{agent_name}/default")
async def put_agent_default_account(
    mcp_name: str,
    agent_name: str,
    body: AgentDefaultAccountRequest,
    user: UserContext = Depends(get_current_user),
):
    """Make one account of the agent's set its default. The assigned-agent
    gate only, as the DELETE."""
    from api.mcp import credentials as credentials_api
    await credentials_api._assigned_gate(user, agent_name)
    ok = await asyncio.to_thread(
        credential_store.set_agent_default_account, user.sub, mcp_name, agent_name, body.account_label,
    )
    if not ok:
        raise _not_in_set(mcp_name, agent_name, body.account_label)
    return {"status": "ok"}
