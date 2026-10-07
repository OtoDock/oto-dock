"""PostgreSQL-backed application store (facade).

The implementations are grouped by domain into the ``storage.db_*`` modules and
re-exported here so callers keep importing from ``storage.database`` unchanged:

- :mod:`storage.automation.db_tasks`    — task runs and dynamic tasks
- :mod:`storage.db_settings` — platform settings
- :mod:`storage.identity.db_users`    — users and user-agent membership
- :mod:`storage.chat.db_chats`    — chats, messages, media tokens, plans
- :mod:`storage.chat.db_previews` — a chat's document preview rows
- :mod:`storage.db_apps`     — pinned apps registry
- :mod:`storage.files.db_file_pins` — Dock file pins
- :mod:`storage.billing.db_usage`    — usage records and usage limits
- :mod:`storage.chat.db_meetings` — meetings and meeting turns

All functions are synchronous (called via ``asyncio.to_thread`` from async code).
"""

from storage.automation.db_tasks import *  # noqa: F401,F403
from storage.db_settings import *  # noqa: F401,F403
from storage.identity.db_users import *  # noqa: F401,F403
from storage.chat.db_chats import *  # noqa: F401,F403
from storage.chat.db_previews import *  # noqa: F401,F403
from storage.db_apps import *  # noqa: F401,F403
from storage.files.db_file_pins import *  # noqa: F401,F403
from storage.billing.db_usage import *  # noqa: F401,F403
from storage.chat.db_meetings import *  # noqa: F401,F403

# Private symbol accessed by name elsewhere (``import *`` skips underscores).
from storage.automation.db_tasks import _EDITABLE_TASK_COLUMNS  # noqa: F401
