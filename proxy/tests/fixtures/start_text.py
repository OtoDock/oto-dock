"""What a model reads at a session's start: its text and the start's riding
part (``core/session/start_context.py``), for the tests that read the data
the text no longer carries (memory, notes, the Notebook's entries, the tree)."""

import config as app_config
from core.session import start_context

SESSION = "start-text-session"


def prompt_at_start(slug: str, **kwargs) -> str:
    """The text ``build_agent_prompt`` gives, followed by the whole riding
    part rendered from the arguments it recorded, as a terminal on a
    satellite without the start hook receives both."""
    text = app_config.build_agent_prompt(slug, session_id=SESSION, **kwargs) or ""
    return text + start_context.inline_block(SESSION) if text else ""
