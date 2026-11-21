"""A session's chosen account per MCP, as its config carries it."""

from __future__ import annotations

from collections.abc import Mapping


def session_account_choices(config) -> dict[str, str]:
    """The chat's choice of account per MCP a session's config carries
    (``AgentConfig.account_choices``), ``{}`` when it carries none (a stub
    config, a session without a choice)."""
    choices = getattr(config, "account_choices", None)
    if not isinstance(choices, Mapping):
        return {}
    return {str(k): str(v) for k, v in choices.items() if k and v}
