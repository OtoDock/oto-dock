"""The outside people's rows (``external_identities``): touched once per
conversation, backfilled from the trees and the call log, gone with the
tree."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from services.phone import outside_upgrade
from storage.identity import external_identity_store as store
from storage.phone import phone_call_log_store

AGENT = "ext-ident-store"


def test_a_touch_makes_the_row_once_and_counts_the_conversations(temp_db):
    first = store.touch(AGENT, "phone", "+302101234567", "302101234567")
    again = store.touch(AGENT, "phone", "+302101234567", "302101234567", verified=True)
    assert first == again
    row = store.get(AGENT, "phone", "302101234567")
    assert row["external_id"] == "+302101234567" and row["conversations"] == 2
    assert row["verified_at"] and row["first_seen"] <= row["last_seen"]
    verified_at = row["verified_at"]
    # An unverified call keeps the last verification.
    store.touch(AGENT, "phone", "+302101234567", "302101234567")
    assert store.get(AGENT, "phone", "302101234567")["verified_at"] == verified_at
    # With and without the plus: one person, by the slug the caller resolved.
    assert store.touch(AGENT, "phone", "302101234567", "302101234567") == first
    assert [r["slug"] for r in store.list_for_agent(AGENT)] == ["302101234567"]


def test_forget_drops_the_row(temp_db):
    store.touch(AGENT, "phone", "+3021", "3021")
    assert store.forget(AGENT, "phone", "3021") is True
    assert store.get(AGENT, "phone", "3021") is None
    assert store.forget(AGENT, "phone", "3021") is False


def _call(agent: str, identity: str, started: str) -> None:
    phone_call_log_store.insert_call({
        "route_id": None, "route_name": "", "phone_server_id": None, "agent": agent,
        "direction": "inbound", "from_number": "", "to_number": "", "transport": "twilio",
        "call_uuid": f"CA{uuid.uuid4().hex[:12]}", "outcome": "completed", "pin_attempts": 0,
        "started_at": started, "ended_at": started, "duration_s": 10, "session_id": "",
        "identity": identity, "tools_run": [],
    })


def test_the_backfill_reads_the_call_log_and_falls_back_to_the_folder(temp_db, tmp_path):
    logged = tmp_path / "302101234567"
    silent = tmp_path / "306900000000"
    logged.mkdir()
    silent.mkdir()
    _call(AGENT, "caller:+302101234567", "2026-09-01T10:00:00+00:00")
    _call(AGENT, "caller-pin:+302101234567", "2026-10-05T10:00:00+00:00")
    _call(AGENT, "shared", "2026-10-06T10:00:00+00:00")
    homes = [(AGENT, "phone", "302101234567", logged), (AGENT, "phone", "306900000000", silent)]
    assert outside_upgrade.backfill_from_disk(homes) == 2
    row = store.get(AGENT, "phone", "302101234567")
    assert row["external_id"] == "+302101234567" and row["conversations"] == 2
    assert row["first_seen"] == "2026-09-01T10:00:00+00:00"
    assert row["last_seen"] == "2026-10-05T10:00:00+00:00"
    other = store.get(AGENT, "phone", "306900000000")
    assert other["external_id"] == "306900000000" and other["conversations"] == 0
    stamp = datetime.fromtimestamp(silent.stat().st_mtime, tz=timezone.utc).isoformat()
    assert other["first_seen"] == stamp == other["last_seen"]
    # A second run makes nothing and keeps the rows.
    assert outside_upgrade.backfill_from_disk(homes) == 0
    assert store.get(AGENT, "phone", "302101234567")["conversations"] == 2
