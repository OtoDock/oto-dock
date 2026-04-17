"""Shared FastAPI router for the core-auth API modules.

``identity`` / ``admin_users`` / ``platform`` all attach their handlers to
this one router, which ``app.py`` mounts (prefix-less) as ``auth.router``.
"""

from fastapi import APIRouter

router = APIRouter()
