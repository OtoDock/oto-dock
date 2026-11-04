"""Batches end to end on the test format: atomic and best_effort saves, the
caller's indexes, side outputs, the result layout, the preview only for what
was saved, and validation before any work."""

import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import toyfmt

import batch
import checks
import shared
from batch import (
    KeyLog,
    prepare_batch,
    run_batch,
    wrap_recorder,
)
from catalog import Catalog, KeySpec, OpSpec
from results import Code, OpError, ResolvedPath, Status


@pytest.fixture()
def hooks(check_registry_copy, monkeypatch, tmp_path):
    """Identity path resolution beneath the suite's write root, recorded
    hook posts, and a registry restored after the test."""
    seen = {"resolve": [], "preview": [], "written": []}

    async def resolve(value, writing=False, **kw):
        seen["resolve"].append((value, writing))
        if "forbidden" in value:
            raise ValueError(f"Cannot open '{value}': outside this agent")
        return value if value.startswith("/") else str(tmp_path / value)

    async def preview(path, filename=None):
        seen["preview"].append(path)

    async def written(path):
        seen["written"].append(path)
        return True

    monkeypatch.setattr(shared, "_resolve_path", resolve)
    monkeypatch.setattr(shared, "_push_preview", preview)
    monkeypatch.setattr(shared, "_notify_file_written", written)
    return seen


def _write(args):
    return asyncio.run(toyfmt.handle_write_notes(args))


def _saved(path):
    return json.loads(Path(path).read_text())


# ---------------------------------------------------------------- reading

@pytest.mark.parametrize("raw, count", [
    ([{"op": "add_line", "text": "a"}], 1),
    ({"op": "add_line", "text": "a"}, 1),
    ('[{"op": "add_line", "text": "a"}, {"op": "add_line", "text": "b"}]', 2),
    (['{"op": "add_line", "text": "a"}'], 1),
    (None, 0),
])
def test_the_shapes_a_batch_arrives_in(raw, count):
    prepared = prepare_batch(raw, toyfmt.CATALOG)
    assert len(prepared.ops) == count and prepared.problems == []


def test_bad_items_keep_their_index_and_help_items_renumber_nothing():
    prepared = prepare_batch(['{"op": "add_line", "text": "a"}', {"op": "help"}, "{oops", 7,
                              {"op": "add_line", "text": "b"}], toyfmt.CATALOG)
    assert [i for i, _ in prepared.ops] == [0, 4]
    assert [(p.index, p.code) for p in prepared.problems] == [(2, Code.BAD_ITEM), (3, Code.BAD_ITEM)]
    assert len(prepared.help_ops) == 1 and prepared.total == 4
    blob = prepare_batch("[{not json", toyfmt.CATALOG)
    assert blob.problems[0].index == 0 and "could not be read as JSON" in blob.problems[0].message


# ---------------------------------------------------------------- running

def _run(ops, **kw):
    job = toyfmt.ToyJob()
    canonical, problems, notes = toyfmt.TEST_CATALOG.resolve_batch(list(enumerate(ops)))
    assert problems == []
    return job, run_batch(canonical, toyfmt.TEST_CATALOG, job, pre_notes=notes, **kw)


def test_an_atomic_batch_runs_every_op_and_marks_later_failures():
    job, outcome = _run([
        {"op": "add_line", "text": "a"},
        {"op": "delete_line", "index": 5},
        {"op": "add_line", "text": "b"},
        {"op": "delete_line", "index": 9},
        {"op": "replace", "find": "zzz", "replace": "y"},
    ])
    statuses = [r.status for r in outcome.results]
    assert statuses == [Status.APPLIED, Status.FAILED, Status.APPLIED, Status.FAILED, Status.NOOP]
    assert outcome.results[1].after is None and outcome.results[3].after == 1
    assert outcome.results[1].code is Code.NOT_FOUND and outcome.results[1].location == "index 5"
    assert not outcome.should_commit and job.lines == ["a", "b"]


def test_best_effort_commits_what_applied():
    _, outcome = _run([{"op": "add_line", "text": "a"}, {"op": "delete_line", "index": 5}], best_effort=True)
    assert outcome.should_commit and outcome.failed == 1


def test_finalize_runs_only_when_the_batch_commits():
    calls = []

    def finalize(job, outcome):
        calls.append(outcome.applied)
        return ["batch warning"]

    _, ok = _run([{"op": "add_line", "text": "a"}], finalize=finalize)
    _, bad = _run([{"op": "delete_line", "index": 1}], finalize=finalize)
    assert calls == [1] and ok.warnings == ["batch warning"] and bad.warnings == []

    def broken(job, outcome):
        raise ValueError("no scan")

    _, outcome = _run([{"op": "add_line", "text": "a"}], finalize=broken)
    assert outcome.warnings == ["a final step could not run: no scan"]


def test_a_handler_fault_fails_its_op_and_memory_errors_propagate():
    _, outcome = _run([{"op": "explode"}])
    assert outcome.results[0].code is Code.ERROR and outcome.results[0].message == "boom"

    def oom(job, ctx, op):
        raise MemoryError

    catalog = toyfmt.Catalog("t", [toyfmt.OpSpec("oom", oom, (), "T")])
    with pytest.raises(MemoryError):
        run_batch([(0, {"op": "oom"})], catalog, toyfmt.ToyJob())


def test_rejected_ops_and_pre_notes_join_the_outcome():
    from results import OpResult

    rejected = [OpResult(1, "add_line", Status.FAILED, "unknown key 'txt'", Code.UNKNOWN_KEY)]
    job = toyfmt.ToyJob()
    outcome = run_batch([(0, {"op": "add_line", "text": "a"}), (2, {"op": "delete_line", "index": 7})],
                        toyfmt.CATALOG, job, rejected=rejected, pre_notes={0: ["'comment' ignored"]},
                        best_effort=True)
    assert [r.index for r in outcome.results] == [0, 1, 2]
    assert outcome.results[2].after is None and outcome.results[0].notes == ["'comment' ignored"]
    assert outcome.total == 3


def test_the_worker_refuses_unresolved_paths():
    outcome = run_batch([(0, {"op": "insert_file", "path": "/raw/lines.txt"})], toyfmt.CATALOG, toyfmt.ToyJob())
    assert outcome.results[0].code is Code.BAD_PATH


# ----------------------------------------------------------- key recording

def test_the_recorder_sees_every_kind_of_read():
    specs = {k.name: k for k in toyfmt.CATALOG.ops["add_line"].keys}
    log = KeyLog()
    op = wrap_recorder({"op": "add_line", "text": "a", "bold": True}, specs, log)
    op.get("text")
    _ = op["bold"]
    _ = "color" in op
    op.pop("missing", None)
    op.setdefault("other", 1)
    assert log.read == {"text", "bold", "color", "missing", "other"} and log.whole == []


def _splat(**kw):
    return kw


@pytest.mark.parametrize("access", [
    dict, json.dumps, copy.copy, list,
    lambda op: {**op}, lambda op: _splat(**op), lambda op: op | {}, {}.update,
    lambda op: op.copy(), lambda op: [k for k in op], lambda op: op.items(), lambda op: op.values(),
])
def test_the_recorder_sees_whole_op_access(access):
    log = KeyLog()
    access(wrap_recorder({"op": "x", "text": "a"}, {}, log))
    assert log.whole


def test_nested_dicts_record_dotted_names():
    from catalog import KeySpec

    specs = {"header": KeySpec("header", ("dict",), fields=(KeySpec("fill"),)),
             "series": KeySpec("series", ("list",), item=(KeySpec("name"),))}
    log = KeyLog()
    op = wrap_recorder({"header": {"fill": "red"}, "series": [{"name": "a"}]}, specs, log)
    op["header"].get("fill")
    op["series"][0]["name"]
    assert log.read == {"header", "header.fill", "series", "series[].name"}
    assert batch.declared_names(tuple(specs.values())) == {"op", "header", "header.fill", "series",
                                                          "series[].name"}


# ------------------------------------------------------------- the parent

def test_a_good_batch_saves_and_previews_once(hooks, tmp_path):
    target = tmp_path / "notes.json"
    out = _write({"path": str(target), "operations": [
        {"op": "add_line", "text": "a"}, {"op": "set_title", "title": "T"}, {"op": "add", "text": "b", "color": "red"},
    ]})
    assert out.splitlines()[0] == f"Notes saved: {target} (3 operations applied: 2 add_line, 1 set_title)"
    assert "Notes:\n  - op #2 add_line: coloured red." in out and out.endswith("Readback — a | b")
    assert _saved(target) == {"title": "T", "lines": ["a", "b"]}
    assert hooks["preview"] == [str(target)] and hooks["written"] == []


def test_an_invalid_batch_is_answered_before_any_work(hooks, tmp_path, monkeypatch):
    async def no_worker(*a, **k):
        raise AssertionError("a worker ran for an invalid batch")

    monkeypatch.setattr("isolation.run_parse", no_worker)
    out = _write({"path": "notes.json", "operations": [
        {"op": "add_line", "text": "a"}, {"op": "add_line", "txt": "b"}, {"op": "fly"}, 5,
    ]})
    assert out.startswith("Nothing saved: notes.json was not touched. 3 of 4 operations are not valid, so none ran.")
    assert "op #1 add_line: unknown key 'txt'" in out and "op #2: unknown operation 'fly'" in out
    assert "op #3: this item is not an operation object" in out
    # The path is looked up only to keep the batch for a fix: no worker ran,
    # nothing was previewed or created.
    assert hooks["resolve"] == [("notes.json", True)]
    assert hooks["preview"] == [] and not (tmp_path / "notes.json").exists()


def test_a_failing_op_saves_nothing_and_previews_nothing(hooks, tmp_path):
    target = tmp_path / "notes.json"
    target.write_text(json.dumps({"title": "", "lines": ["keep"]}))
    out = _write({"path": str(target), "operations": [
        {"op": "add_line", "text": "new"}, {"op": "delete_line", "index": 9}, {"op": "export", "out_path": "x.txt"},
    ]})
    assert out.startswith(f"Nothing saved: {target} is unchanged. 1 of 3 operations failed.")
    assert _saved(target)["lines"] == ["keep"] and hooks["preview"] == []
    assert not (tmp_path / "x.txt").exists() and "Readback" not in out


def test_best_effort_keeps_what_worked(hooks, tmp_path):
    target = tmp_path / "notes.json"
    out = _write({"path": str(target), "best_effort": True, "operations": [
        {"op": "add_line", "text": "a"}, {"op": "add_line", "txt": "b"}, {"op": "insert_file", "path": "forbidden.txt"},
        {"op": "delete_line", "index": 9},
    ]})
    first = out.splitlines()[0]
    # The unknown key and the refused path were refused before they ran; the
    # missing line was found by the handler, which changed nothing.
    assert first == (f"Notes saved: {target} (1 operations applied: 1 add_line; 1 failed and may be partly applied; "
                     "2 failed and changed nothing)")
    assert "op #3 delete_line: index 9: line 9 does not exist (the notes have 1). Read the notes" in out
    assert "may follow" not in out  # the earlier failures never ran, so they caused nothing
    assert "outside this agent" in out and _saved(target)["lines"] == ["a"]
    assert hooks["preview"] == [str(target)]


def test_a_batch_that_changes_nothing_is_not_saved(hooks, tmp_path):
    target = tmp_path / "notes.json"
    target.write_text(json.dumps({"title": "", "lines": ["a"]}))
    before = target.stat().st_mtime_ns
    out = _write({"path": str(target), "operations": [{"op": "replace", "find": "zzz", "replace": "y"}]})
    assert out.splitlines()[0] == f"Nothing saved: {target} is unchanged. No operation changed anything."
    assert "Changed nothing:\n  - op #0 replace: 'zzz' was not found." in out
    assert target.stat().st_mtime_ns == before and hooks["preview"] == []


def test_create_new_with_no_ops_creates_the_file(hooks, tmp_path):
    target = tmp_path / "fresh.json"
    out = _write({"path": str(target), "operations": [], "create_new": True})
    assert out.splitlines()[0] == f"Notes saved: {target} (0 operations applied)"
    assert _saved(target) == {"title": "", "lines": []} and hooks["preview"] == [str(target)]


def test_a_missing_file_that_was_not_saved_was_not_created(hooks, tmp_path):
    out = _write({"path": str(tmp_path / "never.json"), "operations": [{"op": "delete_line", "index": 0}]})
    assert " was not created." in out.splitlines()[0]


def test_help_alone_resolves_nothing_and_help_follows_a_write(hooks, tmp_path):
    out = _write({"path": "notes.json", "operations": [{"op": "help", "name": "add"}]})
    assert out.startswith("add_line: text, bold? (default false), color?") and hooks["resolve"] == []
    out = _write({"path": str(tmp_path / "n.json"), "operations": [
        {"op": "add_line", "text": "a"}, {"op": "help", "name": "replace"}]})
    assert out.index("Notes saved") < out.index("Readback") < out.index("replace: find, replace")


def test_side_outputs_land_after_the_save_and_are_announced(hooks, tmp_path):
    target = tmp_path / "notes.json"
    out = _write({"path": str(target), "operations": [
        {"op": "add_line", "text": "a"}, {"op": "export", "out_path": "out/export.txt"}]})
    assert (tmp_path / "out" / "export.txt").read_text() == "a"
    assert hooks["written"] == [str(tmp_path / "out" / "export.txt")] and hooks["preview"] == [str(target)]
    assert out.splitlines()[0].startswith("Notes saved")


def test_side_outputs_alone_leave_the_main_file_untouched(hooks, tmp_path):
    target = tmp_path / "notes.json"
    target.write_text(json.dumps({"title": "", "lines": ["x"]}))
    before = target.stat().st_mtime_ns
    out = _write({"path": str(target), "operations": [{"op": "export", "out_path": "e.txt"}]})
    assert out.startswith(f"Notes unchanged: {target}. Side files written: {tmp_path / 'e.txt'}")
    assert target.stat().st_mtime_ns == before and hooks["preview"] == []
    assert hooks["written"] == [str(tmp_path / "e.txt")]


def test_a_path_refused_after_resolution_knows_whether_the_file_exists(hooks, tmp_path):
    target = tmp_path / "notes.json"
    out = _write({"path": str(target), "operations": [{"op": "insert_file", "path": "forbidden.txt"}]})
    assert out.startswith(f"Nothing saved: {target} was not created. 1 of 1 operations are not valid")
    target.write_text(json.dumps({"title": "", "lines": []}))
    out = _write({"path": str(target), "operations": [{"op": "insert_file", "path": "forbidden.txt"}]})
    assert out.startswith(f"Nothing saved: {target} is unchanged. 1 of 1 operations are not valid")


def test_a_side_output_aimed_at_the_main_file_is_refused(hooks, tmp_path):
    target = tmp_path / "notes.json"
    out = _write({"path": str(target), "operations": [{"op": "export", "out_path": str(target)}]})
    assert out.startswith("Nothing saved") and "cannot overwrite the file the batch edits" in out


def test_the_work_folder_is_removed(hooks, tmp_path, monkeypatch):
    import tempfile

    made = []
    real = tempfile.mkdtemp

    def mkdtemp(**kw):
        path = real(dir=tmp_path, **kw)
        made.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    _write({"path": str(tmp_path / "n.json"), "operations": [{"op": "export", "out_path": "e.txt"},
                                                            {"op": "add_line", "text": "a"}]})
    assert made and not os.path.exists(made[0])


def test_a_worker_dying_after_landing_reports_the_save(hooks, tmp_path, monkeypatch):
    target = tmp_path / "notes.json"

    async def dies_after_landing(core, call, **kw):
        target.write_text(json.dumps({"title": "", "lines": ["landed"]}))
        raise RuntimeError("the parse worker was terminated")

    monkeypatch.setattr("isolation.run_parse", dies_after_landing)
    out = _write({"path": str(target), "operations": [{"op": "add_line", "text": "a"}]})
    assert out.startswith(f"Notes saved: {target}. The report could not be built: the parse worker")
    assert hooks["preview"] == [str(target)]


def test_a_worker_dying_before_landing_raises(hooks, tmp_path, monkeypatch):
    async def dies(core, call, **kw):
        raise RuntimeError("killed")

    monkeypatch.setattr("isolation.run_parse", dies)
    with pytest.raises(RuntimeError, match="killed"):
        _write({"path": str(tmp_path / "n.json"), "operations": [{"op": "add_line", "text": "a"}]})
    assert hooks["preview"] == []


# ------------------------------------------------------- the commit halves

def test_stage_then_commit_with_a_transform(tmp_path):
    job = toyfmt.ToyJob()
    outcome = run_batch([(0, {"op": "add_line", "text": "a"})], toyfmt.CATALOG, job, work_dir=str(tmp_path))

    def save(fh):
        fh.write(json.dumps({"title": "", "lines": job.lines}).encode())

    staged = batch.stage_write(outcome, work_dir=str(tmp_path), save=save, readback=lambda: "Readback — a")
    assert staged.staged_path == str(tmp_path / batch.STAGED_NAME) and staged.readback == "Readback — a"

    def splice(path):
        data = json.loads(Path(path).read_text())
        data["lines"].append("computed")
        Path(path).write_text(json.dumps(data))

    target = tmp_path / "n.json"
    report = batch.commit_staged(staged, str(target), original=None, kind="Notes", fmt="toy", transform=splice)
    assert report.saved and _saved(target)["lines"] == ["a", "computed"]


def test_the_staged_baseline_reaches_worker_b(hooks, tmp_path):
    """Worker B reads the original's baseline off the staged write, so a
    finding the original already had is reported as already there."""
    def finds(ctx):
        return [checks.Finding(checks.Severity.WARNING, "", location="index 0", message="an old line")]

    checks.register("toy", "old_line", finds)
    outcome = run_batch([(0, {"op": "add_line", "text": "a"})], toyfmt.CATALOG, toyfmt.ToyJob())
    known = checks.Baseline([checks.Finding(checks.Severity.WARNING, "toy.old_line", location="index 0",
                                            message="an old line")])
    for baseline, known_before in ((None, False), (known, True)):
        staged = batch.stage_write(outcome, work_dir=str(tmp_path), save=lambda fh: fh.write(b'{"lines": []}'),
                                   baseline=baseline)
        report = batch.commit_staged(staged, str(tmp_path / "n.json"), original=None, kind="Notes", fmt="toy")
        assert "an old line" in report.text
        assert ("Already in the file before this edit" in report.text) is known_before


def test_declared_removals_reach_the_after_write_checks(hooks, tmp_path):
    """An operation declares what it removed on purpose; only operations
    that applied count, and the checks of the saved file see the list."""
    def removes(job, ctx, op):
        ctx.removed(op["part"])
        if op.get("fail"):
            raise OpError(Code.ERROR, "it went wrong after the removal")
        job.lines.append(op["part"])

    catalog = Catalog("write_notes", [OpSpec("remove_part", removes, (KeySpec("part", ("str",), required=True),
                                                                       KeySpec("fail", ("bool",))), "PARTS")])
    outcome = run_batch([(0, {"op": "remove_part", "part": "/ppt/slides/slide2.xml"}),
                         (1, {"op": "remove_part", "part": "ppt/media/image9.png", "fail": True})],
                        catalog, toyfmt.ToyJob(), best_effort=True)
    assert outcome.removed == ["ppt/slides/slide2.xml"]
    seen = []
    checks.register("toy", "removals", lambda ctx: seen.append(sorted(ctx.removed)) or [])
    staged = batch.stage_write(outcome, work_dir=str(tmp_path), save=lambda fh: fh.write(b'{"lines": []}'))
    assert batch.commit_staged(staged, str(tmp_path / "n.json"), original=None, kind="Notes", fmt="toy").saved
    assert seen == [["ppt/slides/slide2.xml"]]


def test_the_commit_names_what_it_compares():
    """The lost-update guard has no default: a finish that calls the
    module-level commit must say what it compares."""
    import inspect

    for fn in (batch.commit_staged, batch.commit_write):
        parameter = inspect.signature(fn).parameters["original"]
        assert parameter.default is inspect.Parameter.empty and parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_save_to_for_a_library_that_writes_by_name(tmp_path):
    outcome = run_batch([(0, {"op": "add_line", "text": "a"})], toyfmt.CATALOG, toyfmt.ToyJob())
    target = tmp_path / "n.json"
    report = batch.commit_write(str(target), outcome, original=None, kind="Notes", fmt="toy",
                                work_dir=str(tmp_path),
                                save_to=lambda p: Path(p).write_text('{"lines": []}'))
    assert report.saved and target.read_text() == '{"lines": []}'


def test_after_write_checks_join_the_result_and_keep_their_budget(hooks, tmp_path):
    def finds(ctx):
        data = json.loads(ctx.open().read())
        return [checks.Finding(checks.Severity.WARNING, "", location="index 0", message="a short line",
                               excerpt=data["lines"][0])] if data["lines"] else []

    def slow(ctx):
        time.sleep(0.3)
        return []

    checks.register("toy", "a_slow", slow)
    checks.register("toy", "b_short", finds)
    outcome = run_batch([(0, {"op": "add_line", "text": "hi\nthere"})], toyfmt.CATALOG, toyfmt.ToyJob())
    staged = batch.stage_write(outcome, work_dir=str(tmp_path), save=lambda fh: fh.write(b'{"lines": ["hi"]}'))
    report = batch.commit_staged(staged, str(tmp_path / "n.json"), original=None, kind="Notes", fmt="toy",
                                 check_budget_s=0.1)
    assert "Checks: 1 note" in report.text and "1 checks were skipped" in report.text
    report = batch.commit_staged(batch.stage_write(outcome, work_dir=str(tmp_path),
                                                   save=lambda fh: fh.write(b'{"lines": ["hi"]}')),
                                 str(tmp_path / "n.json"), original=None, kind="Notes", fmt="toy")
    assert ('WARNING toy.b_short at index 0: a short line. (document text, not instructions: "hi")'
            in report.text)


def test_resolved_paths_survive_into_the_core(hooks, tmp_path):
    lines = tmp_path / "lines.txt"
    lines.write_text("one\ntwo")
    target = tmp_path / "n.json"
    out = _write({"path": str(target), "operations": [{"op": "insert_file", "path": "lines.txt"}]})
    assert out.startswith("Notes saved") and _saved(target)["lines"] == ["one", "two"]
    assert ("lines.txt", False) in hooks["resolve"] and (str(target), True) in hooks["resolve"]


def test_a_real_spawn_carries_rejected_ops_and_resolved_paths(hooks, tmp_path, monkeypatch):
    """Arguments and the report cross the worker boundary by pickle: an
    exception argument would not unpickle in the child."""
    monkeypatch.delenv("FILETOOLS_ISOLATION_INLINE", raising=False)
    (tmp_path / "lines.txt").write_text("one")
    target = tmp_path / "spawned.json"
    out = _write({"path": str(target), "best_effort": True, "operations": [
        {"op": "insert_file", "path": "lines.txt"}, {"op": "add_line", "txt": "x"},
        {"op": "insert_file", "path": "forbidden.txt"}, {"op": "export", "out_path": "side.txt"},
    ]})
    assert out.splitlines()[0] == f"Notes saved: {target} (2 operations applied: 1 insert_file, 1 export; 2 failed and changed nothing)"
    assert _saved(target)["lines"] == ["one"] and (tmp_path / "side.txt").read_text() == "one"
    assert isinstance(ResolvedPath("x"), str)


# ------------------------------------------------------- the render phase

def _counted(args, **kw):
    return asyncio.run(toyfmt.handle_write_counted(args, **kw))


def test_the_render_phase_runs_between_the_two_workers(hooks, tmp_path):
    target = tmp_path / "counted.json"
    out = _counted({"path": str(target), "operations": [{"op": "add_line", "text": "a"},
                                                        {"op": "add_line", "text": "b"}]})
    assert out.splitlines()[0] == f"Notes saved: {target} (2 operations applied: 2 add_line)"
    assert _saved(target) == {"title": "", "lines": ["a", "b"], "count": 2}
    assert out.endswith("Readback — a | b (count 2, 2 added)") and hooks["preview"] == [str(target)]


def test_a_failed_render_phase_still_commits_and_says_so(hooks, tmp_path):
    async def broken(staged, call, budget_s):
        raise RuntimeError("no slot came free")

    target = tmp_path / "counted.json"
    out = _counted({"path": str(target), "operations": [{"op": "add_line", "text": "a"}]}, render=broken)
    assert out.startswith(f"Notes saved: {target}") and "the render phase failed: no slot came free." in out
    assert "count" not in _saved(target)


def test_a_render_phase_past_its_budget_is_skipped(hooks, tmp_path, monkeypatch):
    async def slow(staged, call, budget_s):
        await asyncio.sleep(5)

    monkeypatch.setattr(batch, "RENDER_BUDGET_S", 0.05)
    out = _counted({"path": str(tmp_path / "c.json"), "operations": [{"op": "add_line", "text": "a"}]},
                   render=slow)
    assert "the render phase ran past 0.05 s and was skipped" in out and out.startswith("Notes saved")


def test_worker_a_facts_reach_the_render_phase(hooks, tmp_path):
    target = tmp_path / "linked.json"
    out = _counted({"path": str(target), "operations": [{"op": "add_line", "text": "https://example.com"}]})
    assert out.startswith(f"Notes saved: {target}") and "nothing was computed: a line links out" in out
    assert "count" not in _saved(target)


def test_worker_a_commits_at_once_when_there_is_nothing_to_render(hooks, tmp_path):
    async def never(staged, call, budget_s):
        raise AssertionError("the render phase ran")

    target = tmp_path / "titled.json"
    _counted({"path": str(target), "operations": [{"op": "add_line", "text": "a"}]})
    out = _counted({"path": str(target), "operations": [{"op": "set_title", "title": "T"}]}, render=never)
    assert out.splitlines()[0] == f"Notes saved: {target} (1 operations applied: 1 set_title)" and "render phase" not in out
    assert _saved(target) == {"title": "T", "lines": ["a"]} and out.endswith("Readback — a")


def test_worker_b_reads_what_worker_a_carried(hooks, tmp_path):
    async def broken(staged, call, budget_s):
        raise RuntimeError("no slot came free")

    target = tmp_path / "carried.json"
    _counted({"path": str(target), "operations": [{"op": "add_line", "text": "a"}]})
    out = _counted({"path": str(target), "operations": [{"op": "add_line", "text": "b"},
                                                        {"op": "add_line", "text": "c"}]}, render=broken)
    assert out.endswith("Readback — a | b | c (not counted, 2 added)")


def _gather(*calls):
    async def both():
        return await asyncio.gather(*calls)

    return asyncio.run(both())


def _recording(events, name):
    async def render(staged, call, budget_s):
        events.append(("start", name))
        await asyncio.sleep(0.05)
        events.append(("end", name))
        return await toyfmt.toy_render(staged, call, budget_s)

    return render


def test_two_writes_to_one_file_take_turns(hooks, tmp_path):
    """The second write's worker A starts after the first committed, so
    it edits the first's result and neither undoes the other."""
    events = []
    target = tmp_path / "shared.json"
    outs = _gather(*(toyfmt.handle_write_counted({"path": str(target), "operations": [{"op": "add_line",
                                                                                       "text": name}]},
                                                 render=_recording(events, name)) for name in ("x", "y")))
    assert all(out.startswith(f"Notes saved: {target}") for out in outs)
    assert [kind for kind, _ in events] == ["start", "end", "start", "end"]
    assert sorted(_saved(target)["lines"]) == ["x", "y"] and _saved(target)["count"] == 2
    assert batch._PATH_LOCKS == {}


def test_writes_to_two_files_do_not_wait_for_each_other(hooks, tmp_path):
    events = []
    _gather(*(toyfmt.handle_write_counted({"path": str(tmp_path / f"{name}.json"),
                                           "operations": [{"op": "add_line", "text": name}]},
                                          render=_recording(events, name)) for name in ("x", "y")))
    assert [kind for kind, _ in events] == ["start", "start", "end", "end"]


def test_a_path_that_never_comes_free_saves_nothing(hooks, tmp_path, monkeypatch):
    target = tmp_path / "busy.json"

    async def scenario():
        async with batch._path_lock(str(target), float("inf")):
            monkeypatch.setattr(batch, "PIPELINE_BUDGET_S", 0.05)
            return await toyfmt.handle_write_notes({"path": str(target), "operations": [{"op": "add_line",
                                                                                        "text": "a"}]})

    out = asyncio.run(scenario())
    assert out.startswith(f"Nothing saved: {target} was not created. Another write to the same file was still "
                          "running") and not target.exists()
    assert batch._PATH_LOCKS == {} and hooks["preview"] == []


def test_a_target_saved_by_something_else_during_the_edit_is_not_overwritten(hooks, tmp_path):
    target = tmp_path / "raced.json"
    _counted({"path": str(target), "operations": [{"op": "add_line", "text": "a"}]})

    async def someone_saves(staged, call, budget_s):
        target.write_text('{"title": "theirs", "lines": []}')
        return await toyfmt.toy_render(staged, call, budget_s)

    out = _counted({"path": str(target), "operations": [{"op": "add_line", "text": "b"}]}, render=someone_saves)
    assert out == (f"Nothing saved: {target} is unchanged. It changed while this edit ran (something else saved "
                   "it), so this edit would have undone that save. Read it again and redo the edit.")
    assert _saved(target) == {"title": "theirs", "lines": []} and hooks["preview"] == [str(target)]

    fresh = tmp_path / "appeared.json"

    async def someone_creates(staged, call, budget_s):
        fresh.write_text("{}")

    out = _counted({"path": str(fresh), "operations": [{"op": "add_line", "text": "b"}]}, render=someone_creates)
    assert out.startswith(f"Nothing saved: {fresh} was not created. It changed while this edit ran")
    assert fresh.read_text() == "{}"


def test_the_commit_checks_the_target_it_was_given(tmp_path):
    outcome = run_batch([(0, {"op": "add_line", "text": "a"})], toyfmt.CATALOG, toyfmt.ToyJob())
    target = tmp_path / "n.json"
    target.write_text("{}")

    def commit(original):
        staged = batch.stage_write(outcome, work_dir=str(tmp_path), save=lambda fh: fh.write(b'{"lines": ["a"]}'))
        return batch.commit_staged(staged, str(target), kind="Notes", fmt="toy", original=original).saved

    assert not commit((1, 2, 3)) and not commit(()) and target.read_text() == "{}"
    assert commit(batch._stat(str(target))) and commit(None)


def test_no_render_phase_when_nothing_is_saved(hooks, tmp_path):
    calls = []

    async def render(staged, call, budget_s):
        calls.append(1)

    target = tmp_path / "c.json"
    target.write_text(json.dumps({"title": "", "lines": ["x"]}))
    out = _counted({"path": str(target), "operations": [{"op": "delete_line", "index": 5}]}, render=render)
    assert calls == [] and out.startswith("Nothing saved") and hooks["preview"] == []


def test_the_pipeline_keeps_its_budget(hooks, tmp_path, monkeypatch):
    import isolation

    ends = []
    real = isolation.run_parse

    async def recording(target, *args, _deadline_at=None, **kw):
        ends.append(_deadline_at)
        return await real(target, *args, _deadline_at=_deadline_at, **kw)

    monkeypatch.setattr(isolation, "run_parse", recording)
    began = time.monotonic()
    _counted({"path": str(tmp_path / "c.json"), "operations": [{"op": "add_line", "text": "a"}]})
    stage_end, finish_end = ends
    budget = batch.PIPELINE_BUDGET_S
    assert stage_end - began <= budget - batch.RENDER_BUDGET_S - batch.FINISH_MIN_S + 1
    assert began + budget - 1 <= finish_end <= began + budget + 1
    ends.clear()
    began = time.monotonic()
    _write({"path": str(tmp_path / "one.json"), "operations": [{"op": "add_line", "text": "a"}]})
    assert len(ends) == 1 and ends[0] - began <= budget + 1
    with pytest.raises(ValueError, match="both render and finish"):
        asyncio.run(batch.handle_write_call({"path": "x", "operations": []}, toyfmt.TEST_CATALOG,
                                            toyfmt.toy_stage, kind="Notes", render=toyfmt.toy_render))


def test_worker_b_never_starts_without_its_minimum(hooks, tmp_path, monkeypatch):
    """Worker A (or its wait for a worker) used the time: nothing is saved
    rather than a commit racing the tool's timeout."""
    def slow_stage(call):
        time.sleep(0.4)
        return toyfmt.toy_stage(call)

    monkeypatch.setattr(batch, "PIPELINE_BUDGET_S", 0.6)
    monkeypatch.setattr(batch, "RENDER_BUDGET_S", 0.05)
    monkeypatch.setattr(batch, "FINISH_MIN_S", 0.3)
    target = tmp_path / "late.json"
    out = asyncio.run(batch.handle_write_call(
        {"path": str(target), "operations": [{"op": "add_line", "text": "a"}]}, toyfmt.TEST_CATALOG, slow_stage,
        kind="Notes", render=toyfmt.toy_render, finish=toyfmt.toy_finish))
    assert out.startswith(f"Nothing saved: {target} was not created. The call ran out of time")
    assert not target.exists() and hooks["preview"] == []


def test_a_late_render_value_is_discarded(hooks, tmp_path, monkeypatch):
    async def stubborn(staged, call, budget_s):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0.1)
            return "late"

    monkeypatch.setattr(batch, "RENDER_BUDGET_S", 0.05)
    target = tmp_path / "c.json"
    out = _counted({"path": str(target), "operations": [{"op": "add_line", "text": "a"}]}, render=stubborn)
    assert "the render phase ran past 0.05 s and was skipped" in out and "count" not in _saved(target)


def test_a_tool_output_path_is_previewed_and_cleaned(hooks, tmp_path, monkeypatch):
    import shared

    def mirror_core(call):
        with shared.safe_open_write(call.extra["mirror_path"]) as fh:
            fh.write(b'{"mirror": true}')
        report = toyfmt.toy_core(call)
        report.outputs.append(call.extra["mirror_path"])
        return report

    target = tmp_path / "n.json"
    mirror = tmp_path / "copies" / "m.json"
    out = asyncio.run(batch.handle_write_call(
        {"path": str(target), "mirror_path": "copies/m.json", "operations": [{"op": "add_line", "text": "a"}]},
        toyfmt.TEST_CATALOG, mirror_core, kind="Notes", carry_paths={"mirror_path": True}))
    assert out.startswith("Notes saved") and mirror.read_text() == '{"mirror": true}'
    assert hooks["preview"] == [str(target), str(mirror)]

    def dies_mid_write(call):
        partial = Path(call.extra["mirror_path"]).with_name(".m.json.0123456789ab.partial")
        partial.write_bytes(b"half")
        raise RuntimeError("killed")

    with pytest.raises(RuntimeError, match="killed"):
        asyncio.run(batch.handle_write_call(
            {"path": str(target), "mirror_path": "copies/m.json", "operations": [{"op": "add_line", "text": "b"}]},
            toyfmt.TEST_CATALOG, dies_mid_write, kind="Notes", carry_paths={"mirror_path": True}))
    assert not [p.name for p in mirror.parent.iterdir() if p.name.endswith(".partial")]


def test_an_output_that_landed_before_a_worker_died_is_reported_saved(hooks, tmp_path):
    def lands_then_dies(call):
        Path(call.extra["mirror_path"]).write_text("landed")
        raise RuntimeError("the parse worker was terminated")

    mirror = tmp_path / "m.json"
    out = asyncio.run(batch.handle_write_call(
        {"path": str(tmp_path / "n.json"), "mirror_path": "m.json", "operations": [{"op": "add_line", "text": "a"}]},
        toyfmt.TEST_CATALOG, lands_then_dies, kind="Notes", carry_paths={"mirror_path": True}))
    assert out.startswith(f"Notes saved: {mirror}. The report could not be built") and hooks["preview"] == [str(mirror)]


def test_tool_arguments_ride_on_the_call(hooks, tmp_path):
    target = tmp_path / "n.json"
    _write({"path": str(target), "prefix": "Draft: ", "mirror_path": "copies/n.json", "unrelated": 1,
            "operations": [{"op": "add_line", "text": "a"}]})
    saved = _saved(target)
    assert saved["title"] == "Draft: " and saved["lines"] == [f"mirror={tmp_path / 'copies' / 'n.json'}", "a"]
    assert ("copies/n.json", True) in hooks["resolve"]


def test_the_staged_file_keeps_the_target_suffix(tmp_path):
    outcome = run_batch([(0, {"op": "add_line", "text": "a"})], toyfmt.CATALOG, toyfmt.ToyJob())
    names = []
    batch.commit_write(str(tmp_path / "n.json"), outcome, original=None, kind="Notes", fmt="toy",
                       work_dir=str(tmp_path), save_to=lambda p: names.append(os.path.basename(p)) or Path(p).write_text("{}"))
    assert names == ["staged.json"]


def test_the_render_phase_in_real_spawns(hooks, tmp_path, monkeypatch):
    monkeypatch.delenv("FILETOOLS_ISOLATION_INLINE", raising=False)
    target = tmp_path / "spawned-counted.json"
    out = _counted({"path": str(target), "operations": [{"op": "add_line", "text": "a"}]})
    assert out.startswith(f"Notes saved: {target}") and _saved(target)["count"] == 1
    assert out.endswith("(count 1, 1 added)")
    out = _counted({"path": str(target), "operations": [{"op": "set_title", "title": "T"}]})
    assert out.startswith(f"Notes saved: {target}") and _saved(target)["title"] == "T"


# ---------------------------------------------------------------- a core in a thread of this process

def _in_thread(args, core=toyfmt.toy_core):
    return batch.handle_write_call(args, toyfmt.TEST_CATALOG, core, kind="Notes", in_thread=True)


def test_a_write_in_thread_runs_its_core_in_this_process(hooks, tmp_path, monkeypatch):
    """For a core a worker cannot hold (an outside program it starts): no
    worker, the call's deadline in ``call.extra``, the same verdict and
    preview."""
    import isolation

    async def no_worker(*a, **k):
        raise AssertionError("a thread write went to a worker")

    seen = []

    def core(call):
        seen.append(call.extra.get("deadline"))
        return toyfmt.toy_core(call)

    monkeypatch.setattr(isolation, "run_parse", no_worker)
    target = tmp_path / "t.json"
    out = asyncio.run(_in_thread({"path": str(target), "operations": [{"op": "add_line", "text": "a"}]}, core))
    assert out.startswith(f"Notes saved: {target} (1 operations applied: 1 add_line)") and _saved(target)["lines"] == ["a"]
    assert hooks["preview"] == [str(target)] and seen[0] > time.monotonic()
    with pytest.raises(ValueError, match="one worker"):
        asyncio.run(batch.handle_write_call({"path": "x", "operations": []}, toyfmt.TEST_CATALOG, toyfmt.toy_stage,
                                            kind="Notes", render=toyfmt.toy_render, finish=toyfmt.toy_finish,
                                            in_thread=True))


def test_a_cancelled_thread_write_holds_its_lock_until_it_ends(hooks, tmp_path):
    """A thread cannot be stopped: a cancelled call returns at once, and the
    next write to the same file waits for the thread instead of running
    beside it."""
    events = []

    def slow(call):
        events.append("start")
        time.sleep(0.4)
        events.append("end")
        return toyfmt.toy_core(call)

    args = {"path": str(tmp_path / "s.json"), "operations": [{"op": "add_line", "text": "a"}]}

    async def main():
        first = asyncio.ensure_future(_in_thread(dict(args), slow))
        await asyncio.sleep(0.1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        events.append("cancelled")
        await _in_thread(dict(args), slow)

    asyncio.run(main())
    assert events == ["start", "cancelled", "end", "start", "end"]


def test_a_cancelled_thread_write_that_fails_is_logged(hooks, tmp_path, caplog):
    def failing(call):
        time.sleep(0.2)
        raise RuntimeError("the outside program fell over")

    args = {"path": str(tmp_path / "f.json"), "operations": [{"op": "add_line", "text": "a"}]}

    async def main():
        first = asyncio.ensure_future(_in_thread(dict(args), failing))
        await asyncio.sleep(0.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.gather(*batch._THREAD_WRITES, return_exceptions=True)

    with caplog.at_level("WARNING"):
        asyncio.run(main())
    assert "the outside program fell over" in caplog.text and not (tmp_path / "f.json").exists()
