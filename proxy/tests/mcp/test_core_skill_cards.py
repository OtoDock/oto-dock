"""The bundled core skills are split into an always-loaded CARD (judgment and
rules, small) and an on-demand GUIDE twin (parameters, examples). This pins
the shape so a future edit cannot quietly grow the always-loaded prompt
back to the 65 KB it was before Plan B: every card stays under the cap,
every guide is a real skill folder the materializer and the Skill builtin
can serve, and the card names its guide."""

from __future__ import annotations

import json

import pytest

import config as app_config
from services.mcp.skill_format import parse_frontmatter

# (mcp, card id, guide id)
CORE_SPLIT = [
    ("schedules-mcp", "task-scheduling", "task-scheduling-guide"),
    ("delegation-mcp", "delegation", "delegation-guide"),
    ("file-tools-mcp", "file-tools-usage", "file-tools-guide"),
    ("triggers-mcp", "trigger-instructions", "triggers-guide"),
    ("notifications-mcp", "notification-instructions", "notifications-guide"),
    ("meetings-mcp", "meetings", "meetings-guide"),
    ("memory-mcp", "memory-usage", "memory-guide"),
    ("display-mcp", "display-tools", "app-authoring"),
]
CARD_MAX_BYTES = 4500
CARDS_TOTAL_MAX_BYTES = 30_000


def _manifest(mcp: str) -> dict:
    return json.loads((app_config.MCPS_DIR / "custom" / mcp / "manifest.json").read_text())


@pytest.mark.parametrize("mcp,card_id,guide_id", CORE_SPLIT)
def test_card_is_always_and_small_and_names_its_guide(mcp, card_id, guide_id):
    m = _manifest(mcp)
    skills = {s["id"]: s for s in m["skills"]}
    card = skills[card_id]
    assert card["loading"] == "always"
    path = app_config.MCPS_DIR / "custom" / mcp / card["file"]
    text = path.read_text()
    assert len(text.encode()) <= CARD_MAX_BYTES, f"{card_id} card is {len(text.encode())} bytes"
    assert f"`{guide_id}`" in text or f"{guide_id} skill" in text, \
        f"{card_id} card must point at its guide {guide_id}"


@pytest.mark.parametrize("mcp,card_id,guide_id", CORE_SPLIT)
def test_guide_is_an_on_demand_skill_folder(mcp, card_id, guide_id):
    m = _manifest(mcp)
    skills = {s["id"]: s for s in m["skills"]}
    guide = skills[guide_id]
    assert guide["loading"] == "on_demand"
    assert guide["description"]
    path = app_config.MCPS_DIR / "custom" / mcp / guide["file"]
    assert path.name == "SKILL.md" and path.parent.name == guide_id
    fm, body = parse_frontmatter(path.read_text())
    assert fm.get("name") == guide_id and fm.get("description")
    assert body.strip()


def test_cards_total_stays_under_the_budget():
    total = 0
    for mcp, card_id, _g in CORE_SPLIT:
        m = _manifest(mcp)
        card = next(s for s in m["skills"] if s["id"] == card_id)
        total += len((app_config.MCPS_DIR / "custom" / mcp / card["file"]).read_bytes())
    assert total <= CARDS_TOTAL_MAX_BYTES, f"always-loaded core skills total {total} bytes"
