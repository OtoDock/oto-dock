"""``services.scheduler.schedule_text``: a task's schedule in words, pinned
case by case against the dashboard's ``describeSchedule`` through the one
fixture both sides read (``dashboard/src/tests/fixtures/scheduleText.json``).
``text`` is the zone-free wording, ``with_zone`` names the zone when the
words carry a clock time — which is what the feed and the REST view send."""

from __future__ import annotations

import json
import sys

import pytest

from tests._paths import PROXY_DIR, REPO_ROOT
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

_FIXTURE = REPO_ROOT / "dashboard" / "src" / "tests" / "fixtures" / "scheduleText.json"


def _cases() -> list[dict]:
    if not _FIXTURE.is_file():
        pytest.skip(f"{_FIXTURE} is absent from this checkout")
    return json.loads(_FIXTURE.read_text())


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_matches_the_dashboard_fixture(case):
    from services.scheduler import schedule_text
    assert schedule_text.words(case["task"]) == case["text"]
    assert schedule_text.describe(case["task"]) == case["with_zone"]


def test_a_row_with_no_zone_and_a_bad_zone_still_reads():
    from services.scheduler import schedule_text
    assert schedule_text.describe({"schedule": "0 8 * * *"}) == "Daily at 08:00"
    assert schedule_text.describe({"schedule": "0 8 * * *", "user_tz": "Asia/Tokyo"}) == "Daily at 08:00 (Asia/Tokyo)"
    # An aware run_at with an unknown zone name prints as given.
    assert schedule_text.words({"run_at": "2026-09-20T02:00:00+00:00", "effective_tz": "Mars/Olympus"}) == \
        "Once on Sun 20 Sep, 02:00"
    assert schedule_text.words({"run_at": "not a date"}) == "Once on not a date"
    assert schedule_text.describe({}) == "—"
