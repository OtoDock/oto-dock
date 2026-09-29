"""App secrets (APPS.md "Secrets"): the declared names live in the signed
``secrets`` block of the row, the values in ``app_secrets`` (Fernet, the
credential store's key), set by a person after the deploy and never by the
agent. Three uses, said by the declaration: ``sends_to`` — the platform adds
the value to the app's outbound calls to one egress host (``api/apps/
app_egress.py``) and the value never enters the sandbox; ``env`` — the
server reads it from its environment (the supervisor merges it at launch
and scrubs it from the log); neither — the platform alone uses it (an
inbound hook's signing secret).

Synchronous helpers over the store; the routes and the supervisor call
them through ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging

from api.apps import manifest as _mf
from storage import db_app_secrets

logger = logging.getLogger("claude-proxy.apps")


def declared(row: dict) -> list[dict]:
    return _mf.parse_secrets(row)


def status_for(row: dict) -> list[dict]:
    """Every declared secret with ``set``, ``set_by`` and ``updated_at`` (a
    names-only read — nothing decrypted), then any stored name the manifest
    no longer declares (``declared: False``) so it can be removed."""
    stored = db_app_secrets.list_set(row["id"])
    out: list[dict] = []
    names: set[str] = set()
    for s in declared(row):
        names.add(s["name"])
        hit = stored.get(s["name"])
        out.append({**s, "required": bool(s.get("required")), "declared": True,
                    "set": hit is not None,
                    "set_by": hit["set_by"] if hit else "",
                    "updated_at": hit["updated_at"] if hit else ""})
    for name, hit in stored.items():
        if name not in names:
            out.append({"name": name, "required": False, "declared": False, "set": True,
                        "set_by": hit["set_by"], "updated_at": hit["updated_at"]})
    return out


def missing_required(row: dict) -> list[str]:
    """The declared required names without a value, in declaration order."""
    wanted = [s["name"] for s in declared(row) if s.get("required")]
    if not wanted:
        return []
    stored = db_app_secrets.list_set(row["id"])
    return [n for n in wanted if n not in stored]


def waiting_reason(row: dict) -> str:
    """Why a release waits on its secrets, in the card's words; empty when
    nothing waits."""
    missing = missing_required(row)
    if not missing:
        return ""
    if len(missing) == 1:
        return f"{missing[0]} is not set"
    return f"{', '.join(missing)} are not set"


def env_values(row: dict) -> dict[str, str]:
    """The values of the ``env`` secrets, for a live or preview launch."""
    names = [s["name"] for s in declared(row) if s.get("env")]
    return db_app_secrets.values(row["id"], names)


def outbound_headers(row: dict, host: str) -> dict[str, str]:
    """The headers the egress route adds to a call to ``host``:
    ``{header: prefix + value}`` for every set ``sends_to`` secret of that
    host. Decrypted here, used once, never logged."""
    wanted = [s for s in declared(row) if (s.get("sends_to") or {}).get("host") == host]
    if not wanted:
        return {}
    vals = db_app_secrets.values(row["id"], [s["name"] for s in wanted])
    out: dict[str, str] = {}
    for s in wanted:
        value = vals.get(s["name"])
        if value is None:
            continue
        st = s["sends_to"]
        out[st["header"]] = f"{st.get('prefix') or ''}{value}"
    return out


async def restart_after_change(row: dict) -> int:
    """A value changed: stop the live and preview instances so the next
    request relaunches them with the new env (the rollback pattern — nobody
    starts a server no one asked for). Returns how many were stopped."""
    from services.apps import app_supervisor
    stopped = await app_supervisor.stop(row["id"], "live")
    stopped += await app_supervisor.stop(row["id"], "preview")
    return stopped
