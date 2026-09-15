"""Shared helpers for the agent-management API package."""

from pathlib import Path

from fastapi import HTTPException

import config
from storage.agents import agent_store


def _get_agent_dir(name: str) -> Path:
    if not agent_store.agent_exists(name):
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return config.get_agent_dir(name)


def _get_execution_paths(agent_data: dict | None) -> list[str]:
    """Compute the full list of execution paths for an agent."""
    primary = (agent_data or {}).get("execution_path", "claude-code-cli")
    extra_json = (agent_data or {}).get("execution_paths", "")
    extra = []
    if extra_json:
        import json
        try:
            extra = json.loads(extra_json)
        except (json.JSONDecodeError, TypeError):
            extra = []
    # Primary first, then extras (deduped)
    paths = [primary]
    for p in extra:
        if p not in paths:
            paths.append(p)
    return paths
