"""The settings catalog as the proxy serves it: ``catalog.json`` beside this
module, generated from the dashboard's ``components/settings/catalog.ts``
by ``npm run settings:build`` and held current by the dashboard's suite.
Every row names a setting, where it lives, what it does and the lowest
platform role that reads and writes it; a row with ``card`` can be drawn
in a chat. No row carries a value."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auth import roles

_PATH = Path(__file__).with_name("catalog.json")


def _load() -> dict[str, Any]:
    with _PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("entries"), list) or not isinstance(data.get("version"), int):
        raise ValueError("catalog.json is not a settings catalog")
    return data


_DATA = _load()
VERSION: int = _DATA["version"]
ENTRIES: list[dict[str, Any]] = _DATA["entries"]
_BY_ID: dict[str, dict[str, Any]] = {e["id"]: e for e in ENTRIES}


def find(setting_id: str) -> dict[str, Any] | None:
    """The row with this id, or None."""
    return _BY_ID.get(setting_id)


def writes(entry: dict[str, Any], platform_role: str | None) -> bool:
    """Whether a person with ``platform_role`` changes the row."""
    floor = entry.get("writes")
    if floor is None:
        return False
    return roles.is_admin(platform_role) or not roles.is_admin(floor)


def bindings() -> set[tuple[str, str]]:
    """Every (method, path) a card reads or writes: GET for a read, the
    write's own method (PUT unless the row says otherwise). The route test
    holds each one registered."""
    out: set[tuple[str, str]] = set()
    for e in ENTRIES:
        value = e.get("value") or {}
        read = value.get("read") or {}
        write = value.get("write") or {}
        if read.get("path"):
            out.add(("GET", read["path"]))
        if write.get("path"):
            out.add((write.get("method") or "PUT", write["path"]))
    return out
