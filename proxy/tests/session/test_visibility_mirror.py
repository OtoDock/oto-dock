"""The visibility mirror (``lib/visibility.ts``) in lock-step with
``core/session/visibility.py``: the two scopes, the four mode keys and the
questions. Core-seams phase 5 (the scope hygiene)."""

from __future__ import annotations

import re
import sys

from tests._paths import PROXY_DIR, REPO_ROOT

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from core.session import visibility as vis  # noqa: E402

_MIRROR = REPO_ROOT / "dashboard" / "src" / "lib" / "visibility.ts"


def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, f"{name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def _ts_union(text: str, name: str) -> list[str]:
    m = re.search(rf"export type {name}\s*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, f"type {name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def test_the_scopes():
    assert vis.SCOPES == ("user", "agent")
    assert vis.SCOPE_USER == "user" and vis.SCOPE_AGENT == "agent"


def test_the_dashboard_mirror_equals_the_authority():
    text = _MIRROR.read_text(encoding="utf-8")
    assert _ts_const_strings(text, "SCOPE") == list(vis.SCOPES)
    assert _ts_union(text, "DefaultScope") == list(vis.SCOPES)
    assert _ts_union(text, "VisibilityMode") == [
        vis.MODE_PERSONAL_SHARED, vis.MODE_SHARED_PERSONAL, vis.MODE_PERSONAL_ONLY, vis.MODE_SHARED_ONLY,
    ]
    for fn in ("modeOf", "columnsOf", "modeOfAgent", "availableScopes"):
        assert f"export function {fn}(" in text, fn
