"""The session-kind authority (``core/session/session_kind``): the table,
the questions, the read-side resolver's answers for every row shape the
install can hold, the driver rule, the owner sentinels, and the dashboard
mirror in lock-step. Core-seams phase 4."""

from __future__ import annotations

import re
import sys

import pytest

from tests._paths import PROXY_DIR, REPO_ROOT

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from core.session import session_kind as sk  # noqa: E402
from core.session import visibility as vis  # noqa: E402


def test_the_table():
    names = [k.name for k in sk.KINDS]
    assert names == ["dashboard", "phone", "task", "meeting", "trigger", "internal", "app"]
    assert len(set(names)) == len(names)
    # The row spellings are the three the column carries; a meeting's turns
    # land in the parent chat, the other three mint no chat at all.
    assert {k.source_type for k in sk.KINDS if k.mints_chat} == {"chat", "phone", "task"}
    assert sk.MEETING.source_type == "meeting" and not sk.MEETING.mints_chat
    assert all(k.source_type == "" for k in (sk.TRIGGER, sk.INTERNAL, sk.APP))
    # Exactly one attended kind; exactly one externally driven; two judged.
    assert [k.name for k in sk.KINDS if k.attended] == ["dashboard"]
    assert [k.name for k in sk.KINDS if k.external_driven] == ["phone"]
    assert [k.name for k in sk.KINDS if k.judged] == ["dashboard", "task"]
    assert sk.EXTERNAL_DRIVEN_SOURCE_TYPES == frozenset({"phone"})


def test_the_questions():
    assert sk.of("task") is sk.TASK and sk.of("app") is sk.APP
    assert sk.of("") is None and sk.of(None) is None and sk.of("sse") is None
    # A session nobody classified is attended and judged — the gate errs
    # towards a person being there; the evaluator judges it as today.
    assert sk.attended("dashboard") and sk.attended("") and sk.attended(None)
    for name in ("task", "phone", "meeting", "trigger", "internal", "app"):
        assert not sk.attended(name), name
    assert sk.judged("dashboard") and sk.judged("task") and sk.judged("")
    for name in ("phone", "meeting", "trigger", "internal", "app"):
        assert not sk.judged(name), name
    assert sk.source_types(sk.TASK) == ["task"]
    assert sk.source_types(sk.DASHBOARD, sk.TASK) == ["chat", "task"]
    assert sk.source_types(sk.APP) == []
    # A query parameter for ``= ANY(%s)``: psycopg adapts a LIST as an array
    # (a tuple is a composite, a frozenset has no dumper) — the list form is
    # what the store passes straight through.
    assert isinstance(sk.source_types(sk.TASK), list)
    assert isinstance(sk.EXTERNAL_DRIVEN_SOURCE_TYPES, frozenset)


def test_the_id_shape():
    assert sk.task_chat_id("run-0123456789ab") == "task-run-0123456789ab"
    assert sk.is_task_chat_id("task-run-0123456789ab")
    assert sk.is_task_chat_id("task-anything")   # the shape, not the run: the store says whether a run exists
    assert not sk.is_task_chat_id("meeting-abc")
    assert not sk.is_task_chat_id("5a1a6e0e-0000-4000-8000-000000000000")
    assert not sk.is_task_chat_id("") and not sk.is_task_chat_id(None)
    assert sk.run_id_of_chat("task-run-0123456789ab") == "run-0123456789ab"
    assert sk.run_id_of_chat("task-foo") == "foo"
    assert sk.run_id_of_chat("5a1a6e0e-0000-4000-8000-000000000000") == ""
    assert sk.run_id_of_chat(None) == ""


@pytest.mark.parametrize("row, kind", [
    # the column decides
    ({"id": "task-run-1", "source_type": "task"}, sk.TASK),
    ({"id": "u1", "source_type": "task"}, sk.TASK),          # the column alone, whatever the id
    ({"id": "u1", "source_type": "phone"}, sk.PHONE),
    ({"id": "u1", "source_type": "chat"}, sk.DASHBOARD),
    # a pre-phase task row: the default spelling and the scheduler's id
    ({"id": "task-run-1", "source_type": "chat"}, sk.TASK),
    ({"id": "task-run-1", "source_type": ""}, sk.TASK),
    ({"id": "task-run-1"}, sk.TASK),
    ({"id": "task-run-1", "source_type": None}, sk.TASK),
    # a task- id that is not a run (the create→stamp window, a hand-minted
    # id): the shape says task; whether a RUN exists is the store's answer
    ({"id": "task-nope", "source_type": "chat"}, sk.TASK),
    # a dashboard row in every empty spelling
    ({"id": "u1", "source_type": ""}, sk.DASHBOARD),
    ({"id": "u1"}, sk.DASHBOARD),
    ({}, sk.DASHBOARD),
    (None, sk.DASHBOARD),
    # a spelling no row kind writes reads as the default's kind
    ({"id": "u1", "source_type": "meeting"}, sk.DASHBOARD),
    ({"id": "task-run-1", "source_type": "sse"}, sk.DASHBOARD),
])
def test_the_resolver(row, kind):
    assert sk.of_chat(row) is kind


def test_the_driver_rule():
    task_row = {"id": "task-run-1", "source_type": "task"}
    phone_row = {"id": "u1", "source_type": "phone"}
    chat_row = {"id": "u1", "source_type": "chat"}
    # the session's kind wins
    assert sk.driver_source_type("task", task_row) == "task"
    assert sk.driver_source_type("dashboard", task_row) == "chat"
    assert sk.driver_source_type("phone", phone_row) == "phone"
    assert sk.driver_source_type("meeting", chat_row) == "meeting"
    # an unrecorded session: the row's kind only when it is externally driven
    assert sk.driver_source_type("", phone_row) == "phone"
    assert sk.driver_source_type("", task_row) == "chat"
    assert sk.driver_source_type("", chat_row) == "chat"
    assert sk.driver_source_type(None, None) == "chat"
    # a kind with no spelling of its own falls through the same way
    assert sk.driver_source_type("app", task_row) == "chat"


def test_the_owner_sentinels():
    assert vis.shared_chat_owner("alpha") == "agent::alpha"
    assert vis.task_chat_owner("alpha") == "task::alpha"
    assert vis.is_shared_chat_owner("agent::alpha") and not vis.is_shared_chat_owner("task::alpha")
    assert vis.is_task_chat_owner("task::alpha") and not vis.is_task_chat_owner("agent::alpha")
    assert vis.is_phone_chat_owner(vis.PHONE_CHAT_OWNER) and not vis.is_phone_chat_owner("phoney")
    for owner in ("agent::alpha", "task::alpha", "phone"):
        assert vis.is_synthetic_owner(owner), owner
    for owner in ("", "user-alice", "alice@example.com", "phoney"):
        assert not vis.is_synthetic_owner(owner), owner
    assert not vis.is_task_chat_owner("") and not vis.is_shared_chat_owner(None)


# --- the dashboard mirror, in lock-step ---------------------------------------

_MIRROR = REPO_ROOT / "dashboard" / "src" / "lib" / "session" / "kind.ts"
_VISIBILITY_TS = REPO_ROOT / "dashboard" / "src" / "lib" / "visibility.ts"


def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, f"{name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def test_the_dashboard_mirror_equals_the_authority():
    text = _MIRROR.read_text(encoding="utf-8")
    assert set(_ts_const_strings(text, "SOURCE_TYPE")) == {k.source_type for k in sk.KINDS if k.mints_chat}
    assert _ts_const_strings(text, "TASK_CHAT_ID_PREFIX") == [sk.TASK_CHAT_ID_PREFIX]
    assert set(_ts_const_strings(text, "EXTERNAL_DRIVEN_SOURCE_TYPES")) == set(sk.EXTERNAL_DRIVEN_SOURCE_TYPES)
    for fn in ("taskChatId", "isTaskChatId", "runIdOfChat", "chatKind", "isExternalDriven"):
        assert f"export function {fn}(" in text, fn
    owners = _VISIBILITY_TS.read_text(encoding="utf-8")
    assert _ts_const_strings(owners, "SHARED_CHAT_OWNER_PREFIX") == [vis.SHARED_CHAT_OWNER_PREFIX]
    assert "export function isSharedChatOwner(" in owners
