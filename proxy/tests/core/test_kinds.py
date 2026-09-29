"""The kind vocabularies (core-seams phase 9): the artifact kinds and their
behaviour facts, the task / run / trigger kinds and the classifier, the app
kind's capabilities, the MCP runtime, the department delegation wiring
(the modes and what each wires, the reaches), and the dashboard mirrors
under ``lib/kinds/`` in lock-step (read by regex, the way
``tests/storage/test_status_vocabularies.py`` reads ``lib/status/``).

Every stored and wire spelling is frozen — the assertions here compare the
strings, never rename them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.config import task_config_builder
from core.events import artifact_events
from services.mcp import mcp_manifest_types as mt
from services.scheduler import task_kinds
from storage import db_apps
from tests._paths import PROXY_DIR, REPO_ROOT
from ws import wire_events as wire

_KINDS_DIR = REPO_ROOT / "dashboard" / "src" / "lib" / "kinds"


def _mirror(name: str) -> str:
    return (_KINDS_DIR / name).read_text(encoding="utf-8")


def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, f"{name} not found in the mirror"
    return re.findall(r"'([^']*)'", m.group(1))


def _ts_facts_table(text: str, name: str) -> dict[str, dict[str, object]]:
    """``{key: {fact: value}}`` from an ``as const`` object of objects keyed
    by ``[WIRE.X]`` / ``[X.Y]`` / a quoted word; a value is a boolean,
    ``null``, a quoted word or a ``WIRE.X`` reference."""
    m = re.search(rf"export const {name}\b[^=]*=\s*\{{(.+?)\n\}}", text, re.S)
    assert m, f"{name} not found in the mirror"
    out: dict[str, dict[str, object]] = {}
    for row in re.finditer(r"\[?([A-Z_]+\.[A-Z_]+|'[a-z_]+')\]?:\s*\{([^}]*)\}", m.group(1)):
        key = row.group(1).strip("'")
        facts: dict[str, object] = {}
        for k, v in re.findall(r"(\w+):\s*(true|false|null|'[^']*'|[A-Z_]+\.[A-Z_]+)", row.group(2)):
            facts[k] = {"true": True, "false": False, "null": None}.get(v, v.strip("'"))
        out[key] = facts
    return out


def _wire_name(ref: str) -> str:
    """``WIRE.IMAGE_GENERATING`` → ``image_generating``; a quoted word as is."""
    return getattr(wire, ref.split(".", 1)[1]) if ref.startswith("WIRE.") else ref


# ---------------------------------------------------------------------------
# the leaves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module, loaded", [
    ("services.scheduler.task_kinds", ["services", "services.scheduler", "services.scheduler.task_kinds"]),
    ("core.events.artifact_events", ["core", "core.events", "core.events.artifact_events",
                                     "core.events.common_events", "ws", "ws.wire_events"]),
])
def test_the_leaf_imports_nothing_else_of_the_tree(module, loaded):
    script = (f"import sys, json\nimport {module}\n"
              "print(json.dumps(sorted(m for m in sys.modules if m.startswith("
              "('core', 'services', 'storage', 'auth', 'config', 'ws', 'api')))))\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=str(PROXY_DIR),
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == loaded


# ---------------------------------------------------------------------------
# the artifact kinds
# ---------------------------------------------------------------------------

_ARTIFACT_WORDS = {"images", "image_generating", "image_gen_failed", "url", "file", "video", "audio",
                   "media_processing", "media_failed", "document_preview", "ui"}


def test_the_artifact_table_and_its_derived_sets():
    assert set(artifact_events.KINDS) == _ARTIFACT_WORDS
    assert artifact_events.ARTIFACT_EVENT_TYPES == frozenset(_ARTIFACT_WORDS)
    assert artifact_events.REPLAYABLE_ARTIFACT_EVENT_TYPES == {
        "images", "url", "file", "video", "audio", "document_preview", "ui"}
    assert artifact_events.BLOCK_KINDS == _ARTIFACT_WORDS - {"image_gen_failed", "media_failed"}
    assert artifact_events.SHAREABLE == {"ui", "images", "video", "audio", "file", "url"}
    assert artifact_events.SAVED == artifact_events.BLOCK_KINDS - {"media_processing"}
    for k in artifact_events.KINDS.values():
        assert k.name in wire.FRAMES
        if not k.block:  # a removal carries no block: never persisted, replayed or shared
            assert not (k.placeholder or k.saved or k.shareable or k.deferred or k.identity)
        if k.placeholder:  # a placeholder is replaced by its twin: never replayed or shared
            assert k.evicts is None and not k.shareable
            assert k.name not in artifact_events.REPLAYABLE_ARTIFACT_EVENT_TYPES
        if k.evicts:
            assert artifact_events.KINDS[k.evicts].placeholder
        if k.deferred:
            assert k.identity, "a deferred kind replaces in place by its identity"
    assert artifact_events.kind_of("images").evicts == "image_generating"
    assert artifact_events.kind_of("image_gen_failed").evicts == "image_generating"
    assert artifact_events.kind_of("video").evicts == "media_processing"
    assert artifact_events.kind_of("audio").evicts == "media_processing"
    assert artifact_events.kind_of("media_failed").evicts == "media_processing"
    assert artifact_events.kind_of("document_preview").identity == "file_id"
    assert artifact_events.kind_of("document_preview").deferred
    assert artifact_events.kind_of("ui").identity == "path"
    assert not artifact_events.kind_of("document_preview").shareable  # by design (see the table)


def test_the_artifact_lookup_is_total():
    # The pump's save loop walks EVERY turn block (text, tool, thinking …).
    for word in ("text", "tool", "thinking", "task_spawn", "plan_review", "permission_prompt", "", None):
        assert artifact_events.kind_of(word) is None, word


def test_saved_agrees_with_the_wire_catalogue():
    # The pump persists exactly the kinds the catalogue says persist.
    for k in artifact_events.KINDS.values():
        assert k.saved == (wire.FRAMES[k.name].persisted is not None), k.name


def test_the_artifact_mirror():
    text = _mirror("artifact.ts")
    table = _ts_facts_table(text, "ARTIFACT_KINDS")
    assert {_wire_name(k) for k in table} == _ARTIFACT_WORDS
    for ref, facts in table.items():
        k = artifact_events.KINDS[_wire_name(ref)]
        assert facts["block"] is k.block, ref
        assert facts["placeholder"] is k.placeholder, ref
        assert (None if facts["evicts"] is None else _wire_name(facts["evicts"])) == k.evicts, ref
        assert {None: None, "fileId": "file_id", "path": "path"}[facts["identity"]] == k.identity, ref
        assert facts["deferred"] is k.deferred, ref
        assert facts["saved"] is k.saved, ref
        assert facts["shareable"] is k.shareable, ref
    # the derived sets are derived, not retyped, and the old copy is gone
    assert "REPLAYABLE_ARTIFACT_EVENT_TYPES: ReadonlySet<string> = new Set(\n  kinds.filter" in text
    wire_ts = (REPO_ROOT / "dashboard" / "src" / "api" / "wireEvents.ts").read_text(encoding="utf-8")
    assert "REPLAYABLE_ARTIFACT_EVENT_TYPES: ReadonlySet" not in wire_ts


# ---------------------------------------------------------------------------
# the task, run and trigger kinds
# ---------------------------------------------------------------------------

def _task(**kw):
    base = dict(task_type="", schedule="", interval_seconds=None, run_at=None, delay_seconds=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_task_kinds_and_their_facts():
    assert task_kinds.TASK_KINDS == {"scheduled", "one_time", "trigger", "continuation", "app", "delegate", "check"}
    assert task_kinds.RUN_KIND_NAMES == {"scheduled", "one-time", "trigger", "delegate", "app", "check"}
    assert task_kinds.TRIGGER_KINDS == {"scheduled", "manual", "trigger", "check", "app_handler", "app_action"}
    k = task_kinds.KINDS
    assert not k["trigger"].clocked and all(v.clocked for n, v in k.items() if n != "trigger")
    assert {n for n, v in k.items() if v.unscheduled} == {"one_time", "trigger"}
    assert k["continuation"].fires == task_kinds.WAKE and k["continuation"].run_kind is None
    assert k["app"].fires == task_kinds.HANDLER
    assert {n for n, v in k.items() if v.fires == task_kinds.SESSION} == {
        "scheduled", "one_time", "trigger", "delegate", "check"}
    assert k["check"].judge and not any(v.judge for n, v in k.items() if n != "check")
    assert k["delegate"].worker and not any(v.worker for n, v in k.items() if n != "delegate")
    assert {n for n, v in k.items() if v.knowledge_rw} == {"scheduled", "one_time", "trigger", "delegate"}
    assert {n for n, v in k.items() if v.button_target} == {"scheduled", "trigger"}
    assert {n: v.run_kind for n, v in k.items()} == {
        "scheduled": "scheduled", "one_time": "one-time", "trigger": "trigger", "continuation": None,
        "app": "app", "delegate": "delegate", "check": "check"}
    assert {v.run_kind for v in k.values() if v.run_kind} == task_kinds.RUN_KIND_NAMES
    assert task_kinds.of_word("nonsense") is None and task_kinds.of_word(None) is None
    assert task_kinds.of(_task(task_type="delegate")).worker
    assert task_kinds.of(_task(schedule="0 9 * * *")).name == "scheduled"
    assert task_kinds.of(_task(run_at="2099-01-01T00:00:00")).name == "one_time"
    # an unknown definition word reads as the timing rule's kind (every flag False)
    unknown = task_kinds.of(_task(task_type="memory_run"))
    assert unknown.name == "one_time" and unknown.clocked and unknown.fires == task_kinds.SESSION
    assert not (unknown.judge or unknown.worker)


@pytest.mark.parametrize("trigger_type", sorted(task_kinds.TRIGGER_KINDS))
def test_the_classifier_by_the_definitions_kind(trigger_type):
    # The origin never changes the run's kind; the definition's kind does.
    assert task_kinds.run_kind_of(_task(task_type="trigger")) == "trigger"
    assert task_kinds.run_kind_of(_task(task_type="delegate")) == "delegate"
    assert task_kinds.run_kind_of(_task(task_type="app", schedule="0 9 * * *")) == "app"
    assert task_kinds.run_kind_of(_task(task_type="check")) == "check"
    assert task_kinds.run_kind_of(_task(task_type="scheduled", schedule="0 9 * * *")) == "scheduled"
    assert task_kinds.run_kind_of(_task(task_type="scheduled", interval_seconds=60)) == "scheduled"
    assert task_kinds.run_kind_of(_task(task_type="one_time", run_at="2099-01-01T00:00:00")) == "one-time"
    # the timing rule for the words it classifies, as the runner always did
    assert task_kinds.run_kind_of(_task(task_type="scheduled")) == "one-time"
    assert task_kinds.run_kind_of(_task(task_type="one_time", schedule="0 9 * * *")) == "scheduled"
    assert task_kinds.run_kind_of(_task(task_type="continuation", schedule="0 9 * * *")) == "scheduled"
    assert task_kinds.run_kind_of(_task(task_type="")) == "one-time"
    assert task_kinds.run_kind_of(_task(task_type="", interval_seconds=5)) == "scheduled"
    assert task_kinds.run_kind_of(_task(task_type="memory_run", interval_seconds=5)) == "scheduled"


def test_derive_self_removes_and_spaced():
    assert task_kinds.derive("0 9 * * *", None) == "scheduled"
    assert task_kinds.derive(None, 60) == "scheduled"
    assert task_kinds.derive(None, None) == "one_time"
    assert task_kinds.derive("", None) == "one_time"
    assert task_kinds.self_removes(_task(task_type="one_time", run_at="2099-01-01T00:00:00"))
    assert task_kinds.self_removes(_task(task_type="delegate"))
    assert task_kinds.self_removes(_task(task_type="check"))
    assert not task_kinds.self_removes(_task(task_type="trigger"))
    assert not task_kinds.self_removes(_task(task_type="scheduled", schedule="0 9 * * *"))
    assert not task_kinds.self_removes(_task(task_type="", interval_seconds=0))
    assert task_kinds.spaced("scheduled")
    assert not any(task_kinds.spaced(t) for t in task_kinds.TRIGGER_KINDS - {"scheduled"})


def test_the_knowledge_write_opt_in_answers_what_it_always_answered():
    # At fire, from the DEFINITION's kind.
    for word, expected in (("scheduled", True), ("one_time", True), ("trigger", True), ("delegate", True),
                           ("continuation", False), ("app", False), ("check", False), ("", False),
                           ("memory_run", False)):
        assert task_config_builder.task_allows_knowledge_rw(_task(task_type=word)) is expected, word
    # At a re-warm, from the RUN row's word: a one-time and a trigger task's
    # continued chat never opted in (the run column spells ``one-time``; a
    # trigger run read ``one-time`` before the classifier was fixed).
    assert {n: v.knowledge_rw for n, v in task_kinds.RUN_KINDS.items()} == {
        "scheduled": True, "one-time": False, "trigger": False, "delegate": True, "app": False, "check": False}
    for word, expected in (("scheduled", True), ("delegate", True), ("one-time", False), ("trigger", False),
                           ("one_time", False), ("app", False), ("check", False), ("", False), (None, False)):
        assert task_config_builder.run_allows_knowledge_rw(word) is expected, word
        assert task_kinds.run_allows_knowledge_rw(word) is expected, word


def test_the_task_mirror():
    text = _mirror("task.ts")
    assert set(_ts_const_strings(text, "TASK_KIND")) == task_kinds.TASK_KINDS
    assert set(_ts_const_strings(text, "RUN_KIND")) == task_kinds.RUN_KIND_NAMES
    assert set(_ts_const_strings(text, "TRIGGER_KIND")) == task_kinds.TRIGGER_KINDS
    for name in ("RUN_KIND_LABEL", "RUN_KIND_STYLE", "TRIGGER_KIND_LABEL", "formatTrigger"):
        assert name in text
    assert "'triggered'" not in text


# ---------------------------------------------------------------------------
# the app kind
# ---------------------------------------------------------------------------

_APP_FACTS = ("serves_tree", "keeps_data", "may_serve", "has_settings", "has_preview_build", "deletable")


def test_the_app_kind_and_its_facts():
    assert db_apps.APP_KIND_FILE == "file" and db_apps.APP_KIND_FOLDER == "folder"
    assert set(db_apps.APP_KINDS) == {"file", "folder"}
    folder, file = db_apps.APP_KINDS["folder"], db_apps.APP_KINDS["file"]
    assert all(getattr(folder, f) for f in _APP_FACTS) and not any(getattr(file, f) for f in _APP_FACTS)
    assert db_apps.app_kind_of({"kind": "folder"}) is folder
    assert db_apps.app_kind_of({"kind": "file"}) is file
    assert db_apps.app_kind_of({"kind": ""}) is file and db_apps.app_kind_of({}) is file
    assert db_apps.app_kind_of({"kind": None}) is file and db_apps.app_kind_of(None) is file
    assert db_apps.app_kind_of({"kind": "tree"}) is file  # an unknown word is the column's default


def test_the_app_mirror():
    text = _mirror("app.ts")
    assert set(_ts_const_strings(text, "APP_KIND")) == set(db_apps.APP_KINDS)
    table = _ts_facts_table(text, "APP_KINDS")
    assert {k.split(".")[-1].lower() for k in table} == {"file", "folder"}
    ts_of = {"servesTree": "serves_tree", "keepsData": "keeps_data", "mayServe": "may_serve",
             "hasSettings": "has_settings", "hasPreviewBuild": "has_preview_build", "deletable": "deletable"}
    for ref, facts in table.items():
        py = db_apps.APP_KINDS[ref.split(".")[-1].lower()]
        assert set(facts) == set(ts_of), ref
        for ts_name, py_name in ts_of.items():
            assert facts[ts_name] is getattr(py, py_name), (ref, ts_name)


# ---------------------------------------------------------------------------
# the MCP runtime
# ---------------------------------------------------------------------------

def test_the_runtime_and_its_facts():
    assert mt.RUNTIMES == {"python", "node", "docker", "none"}
    assert mt.runtime_of("python").installed and mt.runtime_of("node").installed
    assert not mt.runtime_of("docker").installed and not mt.runtime_of("none").installed
    assert mt.runtime_of("docker").container
    assert not any(mt.runtime_of(w).container for w in ("python", "node", "none"))
    assert not mt.runtime_of("none").process
    assert all(mt.runtime_of(w).process for w in ("python", "node", "docker"))
    assert mt.runtime_of("Docker") is mt.runtime_of("docker")  # the parser never validates the case
    assert mt.runtime_of("hosted") is None and mt.runtime_of("") is None and mt.runtime_of(None) is None
    srv = mt.ServerConfig(runtime="docker", transport="sse")
    assert mt.is_container(srv) and not mt.installs_on_host(srv) and mt.has_process(srv)
    srv = mt.ServerConfig(runtime="none", transport="none")
    assert not mt.is_container(srv) and not mt.installs_on_host(srv) and not mt.has_process(srv)
    srv = mt.ServerConfig(runtime="python", transport="stdio")
    assert not mt.is_container(srv) and mt.installs_on_host(srv) and mt.has_process(srv)
    assert not mt.is_container(None) and not mt.installs_on_host(None) and not mt.has_process(None)


def test_the_runtime_mirror():
    text = _mirror("mcpRuntime.ts")
    assert set(_ts_const_strings(text, "MCP_RUNTIME")) == mt.RUNTIMES
    for f in ("api/mcps.ts", "api/community.ts"):
        src = (REPO_ROOT / "dashboard" / "src" / f).read_text(encoding="utf-8")
        assert "'python' | 'node' | 'docker'" not in src, f


# ---------------------------------------------------------------------------
# the department delegation wiring
# ---------------------------------------------------------------------------

def test_the_department_wiring_and_its_facts():
    from storage.agents import db_departments as d
    assert d.MODES == ("off", "down", "down_across", "both")
    assert d.DEFAULT_MODE == "down" and d.REACHES == ("adjacent", "subtree")
    off, down, across, both = (d.MODE_WIRING[m] for m in d.MODES)
    assert not (off.down or off.up or off.peers)
    assert down.down and not down.up and not down.peers
    assert across.down and not across.up and across.peers
    assert both.down and both.up and both.peers


def test_the_department_mirror():
    from storage.agents import db_departments as d
    text = _mirror("department.ts")
    assert _ts_const_strings(text, "DEPARTMENT_MODES") == list(d.MODES)
    assert _ts_const_strings(text, "DEPARTMENT_REACHES") == list(d.REACHES)
    assert _ts_const_strings(text, "DEFAULT_MODE") == [d.DEFAULT_MODE]
    # a label, a hint and a summary word for every mode and reach, no other
    for name, words in (
        ("MODE_LABEL", d.MODES), ("MODE_HINT", d.MODES), ("MODE_SUMMARY", d.MODES),
        ("REACH_LABEL", d.REACHES), ("REACH_HINT", d.REACHES), ("REACH_SUMMARY", d.REACHES),
    ):
        m = re.search(rf"export const {name}\b[^=]*=\s*\{{(.+?)\n\}}", text, re.S)
        assert m, name
        assert re.findall(r"^\s+(\w+):", m.group(1), re.M) == list(words), name
    # the reach shows under every mode that wires something, never under off
    m = re.search(r"export const MODE_SHOWS_REACH\b[^=]*=\s*\{(.+?)\n\}", text, re.S)
    assert m
    shows = dict(re.findall(r"(\w+):\s*(true|false)", m.group(1)))
    assert shows == {w: str(any(d.MODE_WIRING[w])).lower() for w in d.MODES}
