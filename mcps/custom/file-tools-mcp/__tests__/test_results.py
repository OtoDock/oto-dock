"""Per-operation results: the records cross the worker boundary by pickle,
the context guards side outputs, and the text keeps one order with the
frozen first line."""

import os
import pickle
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import results
from results import (
    BatchOutcome,
    Code,
    Deferred,
    NoOp,
    OpCtx,
    OpError,
    OpResult,
    ResolvedPath,
    Staged,
    Status,
    WriteReport,
    render_outcome,
    render_rejected,
)


def _outcome(*statuses, best_effort=False, create_new=False, total=None):
    rs = [OpResult(i, f"op{i}", s) for i, s in enumerate(statuses)]
    return BatchOutcome(results=rs, total=len(rs) if total is None else total, best_effort=best_effort,
                        create_new=create_new)


@pytest.mark.parametrize("value", [
    OpError(Code.NOT_FOUND, "no slide 9", hint="Use slide_index 0-3.", location="slide_index 9", index=4,
            op="set_text"),
    NoOp("nothing matched"),
    ResolvedPath("/agents/a/x.docx"),
    OpResult(3, "add_table", Status.FAILED, "bad", Code.BAD_TYPE, notes=["n"], after=1),
    _outcome(Status.APPLIED, Status.FAILED),
    WriteReport("text", True, ["/agents/a/side.png"]),
    Staged(_outcome(Status.APPLIED), "/tmp/x", "readback"),
])
def test_records_survive_a_pickle_round_trip(value):
    back = pickle.loads(pickle.dumps(value))
    assert type(back) is type(value)
    if isinstance(value, Exception):
        assert back.__reduce__() == value.__reduce__()
    else:
        assert back == value


def test_a_failure_that_changed_nothing_says_so():
    results = [OpResult(0, "a", Status.APPLIED), OpResult(1, "b", Status.FAILED, unchanged=True),
               OpResult(2, "c", Status.FAILED)]
    outcome = BatchOutcome(results=results, total=3, best_effort=True)
    first = render_outcome(outcome, kind="Document", path="/agents/a/doc.docx", saved=True).splitlines()[0]
    assert first == ("Document saved: a/doc.docx (1 operations applied: 1 a; 1 failed and may be partly applied; "
                     "1 failed and changed nothing)")
    refused = OpError(Code.BAD_VALUE, "refused")
    refused.unchanged = True
    back = pickle.loads(pickle.dumps(refused))
    assert back.unchanged is True and OpResult.from_error(back).unchanged is True


def test_counts_and_save_rules():
    assert _outcome(Status.APPLIED, Status.NOOP).should_commit
    assert not _outcome(Status.APPLIED, Status.FAILED).should_commit
    assert _outcome(Status.APPLIED, Status.FAILED, best_effort=True).should_commit
    assert not _outcome(Status.FAILED, best_effort=True).should_commit
    assert not _outcome(Status.NOOP).should_commit
    assert _outcome(create_new=True).should_commit and _outcome(create_new=True).should_save_main
    side = _outcome(Status.APPLIED)
    side.results[0].side_only = True
    assert side.should_commit and not side.should_save_main and not side.main_changed


def test_the_context_names_side_temps_in_the_work_folder(tmp_path):
    ctx = OpCtx(2, "split", work_dir=str(tmp_path), main_path="/agents/a/doc.pdf")
    first = ctx.side_temp(ResolvedPath("/agents/a/out/part.pdf"))
    second = ctx.side_temp(ResolvedPath("/agents/a/out/part.pdf"), ".tmp")
    assert os.path.dirname(first) == str(tmp_path) and first != second
    assert first.endswith("-part.pdf") and second.endswith("-part.pdf.tmp")
    with pytest.raises(OpError, match="work folder"):
        OpCtx(0, "split").side_temp("/x")


def test_defer_write_refuses_the_main_file_twins_and_unresolved_paths():
    earlier = [Deferred("/agents/a/out/one.png", "/tmp/1", 0)]
    ctx = OpCtx(1, "extract_images", main_path="/agents/a/doc.pdf", earlier=earlier)
    with pytest.raises(OpError) as caught:
        ctx.defer_write(ResolvedPath("/agents/a/doc.pdf"), "/tmp/t")
    assert caught.value.code is Code.BAD_PATH
    with pytest.raises(OpError, match="two outputs"):
        ctx.defer_write(ResolvedPath("/agents/a/out/one.png"), "/tmp/t")
    with pytest.raises(OpError, match="not resolved"):
        ctx.defer_write("/agents/a/out/raw.png", "/tmp/t")
    ctx.defer_write(ResolvedPath("/agents/a/out/two.png"), "/tmp/2")
    with pytest.raises(OpError, match="two outputs"):
        ctx.defer_write(ResolvedPath("/agents/a/out/two.png"), "/tmp/3")


def test_side_dest_keeps_a_plain_name_inside_its_folder():
    ctx = OpCtx(0, "extract_images")
    folder = ResolvedPath("/agents/a/out")
    assert ctx.side_dest(folder, "img_1.png") == "/agents/a/out/img_1.png"
    assert isinstance(ctx.side_dest(folder, "img_1.png"), ResolvedPath)
    for bad in ("sub/img.png", os.pardir, os.curdir, ""):
        with pytest.raises(OpError):
            ctx.side_dest(folder, bad)
    with pytest.raises(OpError, match="not resolved"):
        ctx.side_dest("/agents/a/out", "img.png")


@pytest.mark.parametrize("statuses, kwargs, saved, first", [
    ((Status.APPLIED, Status.APPLIED), {}, True, "Document saved: a/doc.docx (2 operations applied: 1 op0, 1 op1)"),
    ((), {"create_new": True}, True, "Document saved: a/doc.docx (0 operations applied)"),
    ((Status.APPLIED, Status.NOOP), {}, True,
     "Document saved: a/doc.docx (1 operations applied: 1 op0; 1 changed nothing)"),
    ((Status.APPLIED, Status.FAILED), {"best_effort": True}, True,
     "Document saved: a/doc.docx (1 operations applied: 1 op0; 1 failed and may be partly applied)"),
    ((Status.APPLIED, Status.FAILED), {}, False,
     "Nothing saved: a/doc.docx is unchanged. 1 of 2 operations failed. Fix them and send the whole batch "
     "again, or pass best_effort: true to keep the operations that worked."),
    ((Status.NOOP,), {}, False, "Nothing saved: a/doc.docx is unchanged. No operation changed anything."),
    ((), {}, False, "Nothing saved: a/doc.docx is unchanged. No operations were given."),
    ((Status.FAILED,), {"best_effort": True}, False,
     "Nothing saved: a/doc.docx is unchanged. 1 of 1 operations failed and none changed the file."),
])
def test_the_verdict_line(statuses, kwargs, saved, first):
    text = render_outcome(_outcome(*statuses, **kwargs), kind="Document", path="/agents/a/doc.docx", saved=saved)
    assert text.splitlines()[0] == first


def _named(*ops, statuses=None):
    statuses = statuses or [Status.APPLIED] * len(ops)
    return BatchOutcome(results=[OpResult(i, op, s) for i, (op, s) in enumerate(zip(ops, statuses, strict=True))], total=len(ops))


def test_the_verdict_names_the_operations_that_ran_by_kind():
    """The agent sent its batch; the verdict says what ran, most first."""
    ops = ["add_paragraph", "set_cell", "add_paragraph", "style_table", "add_paragraph", "set_cell",
           "add_paragraph", "add_paragraph", "set_cell", "add_paragraph"]
    first = render_outcome(_named(*ops), kind="Document", path="/agents/a/d.docx", saved=True).splitlines()[0]
    assert first == "Document saved: a/d.docx (10 operations applied: 6 add_paragraph, 3 set_cell, 1 style_table)"


def test_the_tally_counts_only_what_ran_and_is_capped():
    statuses = [Status.APPLIED, Status.FAILED, Status.NOOP, Status.APPLIED]
    first = render_outcome(_named("a", "b", "c", "a", statuses=statuses), kind="Workbook", path="/agents/w.xlsx",
                           saved=True).splitlines()[0]
    assert first == "Workbook saved: w.xlsx (2 operations applied: 2 a; 1 changed nothing; 1 failed and may be partly applied)"
    many = [f"op_{n}" for n in range(9)] + ["op_8"]
    first = render_outcome(_named(*many), kind="Deck", path="/agents/p.pptx", saved=True).splitlines()[0]
    assert first == ("Deck saved: p.pptx (10 operations applied: 2 op_8, 1 op_0, 1 op_1, 1 op_2, 1 op_3, 1 op_4 "
                     "and 3 more)")


def test_a_new_file_that_was_not_saved_was_not_created():
    text = render_outcome(_outcome(Status.FAILED), kind="Workbook", path="/agents/a/new.xlsx", saved=False,
                          existed=False)
    assert text.startswith("Nothing saved: a/new.xlsx was not created.")


def test_side_files_alone_leave_the_main_file_unchanged():
    text = render_outcome(_outcome(Status.APPLIED), kind="PDF", path="/agents/a/doc.pdf", saved=False,
                          side_written=["/agents/a/out/p1.pdf", "/agents/a/out/p2.pdf"])
    assert text.startswith("PDF unchanged: a/doc.pdf. Side files written: a/out/p1.pdf, a/out/p2.pdf")


def test_the_result_keeps_one_order():
    outcome = BatchOutcome(results=[
        OpResult(0, "add_heading", Status.APPLIED, notes=["'comment' ignored"], warnings=["very large"]),
        OpResult(1, "add_table", Status.FAILED, "unknown key 'colour'", Code.UNKNOWN_KEY,
                 hint="Accepted keys: headers."),
        OpResult(3, "search_and_replace", Status.NOOP, "'foo' was not found"),
        OpResult(4, "delete_paragraph", Status.FAILED, "paragraph 40 does not exist", Code.NOT_FOUND,
                 location="paragraph index 40", after=1),
    ], total=5, best_effort=True, warnings=["the last op is a page break"])
    text = render_outcome(outcome, kind="Document", path="/agents/a/d.docx", saved=True,
                          readback="Readback — grid", hints="add_table: headers, rows")
    order = [text.index(s) for s in ("Document saved", "Failed:", "Changed nothing:", "Warnings:", "Notes:",
                                     "Readback", "add_table: headers")]
    assert order == sorted(order)
    assert ('  - op #1 add_table: unknown key \'colour\'. Accepted keys: headers. '
            '({"op": "help", "name": "add_table"} shows the full shape.)') in text
    assert ("  - op #4 delete_paragraph: paragraph index 40: paragraph 40 does not exist. "
            "This may follow from op #1.") in text
    assert "  - op #3 search_and_replace: 'foo' was not found." in text
    assert "  - op #0 add_heading: very large." in text and "  - the last op is a page break." in text


def test_an_unsaved_result_omits_the_readback():
    text = render_outcome(_outcome(Status.FAILED), kind="Workbook", path="/agents/a/b.xlsx", saved=False,
                          readback="Readback — grid")
    assert "Readback" not in text


def test_long_lists_are_capped(monkeypatch):
    outcome = _outcome(*([Status.FAILED] * 120))
    text = render_outcome(outcome, kind="Document", path="/agents/a/d.docx", saved=False)
    assert text.count("\n  - op #") == results.LIST_CAP
    assert "...and 70 more" in text
    monkeypatch.setattr(results, "TEXT_CAP", 100)
    assert results.cap_text("x" * 500).endswith("(The result was cut at 100 characters.)")


def test_an_unknown_op_points_at_help():
    line = results.failure_line(OpResult(2, "add_photo", Status.FAILED, "unknown operation 'add_photo'",
                                         Code.UNKNOWN_OP, hint="Did you mean 'add_image'?"))
    assert line.endswith('Did you mean \'add_image\'? ({"op": "help"} lists every operation.)')


def test_a_rejected_batch_lists_every_invalid_operation():
    problems = [OpError(Code.UNKNOWN_KEY, "unknown key 'x'", index=4, op="add_table"),
                OpError(Code.BAD_ITEM, "this item is not an operation object", index=1)]
    text = render_rejected(problems, kind="Document", path="/agents/a/d.docx", total=6, hints="help text")
    lines = text.splitlines()
    assert lines[0] == ("Nothing saved: a/d.docx was not touched. 2 of 6 operations are not valid, so none ran. "
                        "Fix them and send the whole batch again.")
    assert lines[2].startswith("  - op #1: this item") and lines[3].startswith("  - op #4 add_table:")
    assert text.endswith("help text")


@pytest.mark.parametrize("existed, state", [(None, "was not touched"), (True, "is unchanged"),
                                            (False, "was not created")])
def test_a_rejected_batch_says_what_it_knows_of_the_file(existed, state):
    text = render_rejected([OpError(Code.BAD_PATH, "outside", index=0, op="x")], kind="Notes",
                           path="/agents/a/n.json", total=1, existed=existed)
    assert text.startswith(f"Nothing saved: a/n.json {state}. 1 of 1 operations are not valid")


@pytest.mark.parametrize("message, hint, expected", [
    ("line 9 does not exist (the notes have 1)", "Read the notes.",
     "line 9 does not exist (the notes have 1). Read the notes."),
    ("see the help (it lists every key.)", "", "see the help (it lists every key.)"),
    ("no slide 4", "Use 0-3", "no slide 4. Use 0-3."),
])
def test_a_message_ending_in_a_parenthesis_gets_its_full_stop(message, hint, expected):
    line = results.failure_line(OpResult(1, "x", Status.FAILED, message, Code.NOT_FOUND, hint=hint))
    assert line == f"  - op #1 x: {expected}"
