"""A test format built on the batch API end to end: notes saved as JSON.

It proves the catalogue, ``run_batch``, the commit and the parent handler
without a document library, and it is the shape a format module takes once
it has moved onto them: a ``CATALOG``, handlers ``(job, ctx, op)``, a core
over a ``WriteCall``, and the key-coverage contract's ``contract_job``.
"""

import asyncio
import json
import os

from batch import Rendered, WriteCall, handle_write_call
from catalog import Catalog, KeySpec, OpSpec
from results import Code, NoOp, OpError, Staged, WriteReport

KIND = "Notes"
FMT = "toy"


class ToyJob:
    def __init__(self, lines: list[str] | None = None, title: str = "") -> None:
        self.lines = list(lines or [])
        self.title = title


def _add_line(job: ToyJob, ctx, op: dict) -> None:
    text = op["text"]
    if op.get("bold"):
        text = f"**{text}**"
    if "color" in op:
        ctx.note(f"coloured {op['color']}")
    job.lines.append(text)


def _delete_line(job: ToyJob, ctx, op: dict) -> None:
    index = op["index"]
    if not 0 <= index < len(job.lines):
        raise OpError(Code.NOT_FOUND, f"line {index} does not exist (the notes have {len(job.lines)})",
                      location=f"index {index}", hint="Read the notes for the current indexes.")
    del job.lines[index]


def _replace(job: ToyJob, ctx, op: dict) -> None:
    hits = [i for i, line in enumerate(job.lines) if op["find"] in line]
    if not hits:
        raise NoOp(f"'{op['find']}' was not found")
    for i in hits:
        job.lines[i] = job.lines[i].replace(op["find"], op["replace"])


def _set_title(job: ToyJob, ctx, op: dict) -> None:
    if len(op["title"]) > 40:
        ctx.warn("the title is longer than 40 characters")
    job.title = op["title"]


def _insert_file(job: ToyJob, ctx, op: dict) -> None:
    with open(op["path"], encoding="utf-8") as fh:
        job.lines.extend(fh.read().splitlines())


def _export(job: ToyJob, ctx, op: dict) -> None:
    temp = ctx.side_temp(op["out_path"])
    with open(temp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(job.lines))
    ctx.defer_write(op["out_path"], temp)


def _explode(job: ToyJob, ctx, op: dict) -> None:
    job.lines.append("half")
    raise RuntimeError("boom")


CATALOG = Catalog("write_notes", [
    OpSpec("add_line", _add_line, (
        KeySpec("text", ("str",), required=True),
        KeySpec("bold", ("bool",), default=False),
        KeySpec("color", ("color",)),
    ), "LINES", aliases=("add",), note="appends a line",
        examples=({"op": "add_line", "text": "hi", "bold": True, "color": "red"},)),
    OpSpec("delete_line", _delete_line, (KeySpec("index", ("int",), required=True),), "LINES",
           setup=({"op": "add_line", "text": "x"},), examples=({"op": "delete_line", "index": 0},)),
    OpSpec("replace", _replace, (
        KeySpec("find", ("str",), required=True), KeySpec("replace", ("str",), required=True),
    ), "EDITING", setup=({"op": "add_line", "text": "a cat"},),
        examples=({"op": "replace", "find": "cat", "replace": "dog"},)),
    OpSpec("set_title", _set_title, (KeySpec("title", ("str",), required=True),), "LINES",
           examples=({"op": "set_title", "title": "Minutes"},)),
    OpSpec("insert_file", _insert_file, (KeySpec("path", ("path",), required=True),), "FILES",
           examples=({"op": "insert_file", "path": "lines.txt"},)),
    OpSpec("export", _export, (KeySpec("out_path", ("out_path",), required=True),), "FILES", side_only=True,
           examples=({"op": "export", "out_path": "export.txt"},)),
], notes="NOTES: lines are plain text.")

EXPLODE = OpSpec("explode", _explode, (), "TEST")
TEST_CATALOG = Catalog("write_notes", [*CATALOG.ops.values(), EXPLODE])


def _finalize(job: ToyJob, outcome) -> list[str]:
    return ["the notes have no title"] if job.lines and not job.title else []


def load(path: str) -> ToyJob:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return ToyJob(data["lines"], data.get("title", ""))


def toy_core(call: WriteCall, catalog: Catalog = TEST_CATALOG) -> WriteReport:
    job = load(call.path) if call.edit_existing else ToyJob()
    if call.extra.get("prefix"):
        job.title = call.extra["prefix"] + job.title
    if call.extra.get("mirror_path"):
        job.lines.append(f"mirror={call.extra['mirror_path']}")
    outcome = call.run(catalog, job, finalize=_finalize)

    def save(fh) -> None:
        fh.write(json.dumps({"title": job.title, "lines": job.lines}).encode())

    return call.commit(outcome, kind=KIND, fmt=FMT, save=save,
                       readback=lambda: "Readback — " + " | ".join(job.lines))


async def handle_write_notes(args: dict) -> str:
    return await handle_write_call(args, TEST_CATALOG, toy_core, kind=KIND, carry=("prefix",),
                                   carry_paths={"mirror_path": True})


CARRIED = "carried.json"


def toy_stage(call: WriteCall) -> Staged | WriteReport:
    """Worker A of the render-phase variant: the batch applied and staged,
    or committed at once when it added no line (nothing to count). What it
    hands on: ``facts`` for the render phase and worker B, and a file in the
    work folder (the convention for data too large to pickle)."""
    job = load(call.path) if call.edit_existing else ToyJob()
    before = list(job.lines)
    outcome = call.run(TEST_CATALOG, job, finalize=_finalize)

    def save(fh) -> None:
        fh.write(json.dumps({"title": job.title, "lines": job.lines}).encode())

    added = job.lines[len(before):] if job.lines[:len(before)] == before else list(job.lines)
    if not added:
        return call.commit(outcome, kind=KIND, fmt=FMT, save=save,
                           readback=lambda: "Readback — " + " | ".join(job.lines))
    with open(os.path.join(call.work_dir, CARRIED), "w", encoding="utf-8") as fh:
        json.dump({"added": added}, fh)
    return call.stage(outcome, save=save, readback=lambda: "Readback — " + " | ".join(job.lines),
                      facts={"links_out": any(line.startswith("http") for line in job.lines)})


def _count_lines(staged_path: str, work_dir: str) -> str:
    with open(staged_path, encoding="utf-8") as fh:
        count = len(json.load(fh)["lines"])
    computed = os.path.join(work_dir, "computed.json")
    with open(computed, "w", encoding="utf-8") as fh:
        json.dump({"count": count}, fh)
    return computed


async def toy_render(staged: Staged, call: WriteCall, budget_s: float) -> Rendered:
    """The parent's render phase: what a recalculation would compute (here,
    the line count), written beside the staged file. It runs on the server's
    event loop, so its file work goes to a thread. Worker A's facts can
    refuse it, as a workbook that links out refuses a recalculation."""
    if staged.facts.get("links_out"):
        return Rendered(None, "nothing was computed: a line links out")
    computed = await asyncio.to_thread(_count_lines, staged.staged_path, call.work_dir)
    return Rendered(payload=computed)


def toy_finish(staged: Staged, call: WriteCall, rendered: Rendered) -> WriteReport:
    """Worker B: the computed value spliced into the staged file, the
    readback built from the spliced file (so it shows the count) with what
    worker A carried in the work folder, then the commit."""
    def splice(path: str) -> None:
        with open(rendered.payload, encoding="utf-8") as fh:
            count = json.load(fh)["count"]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["count"] = count
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def readback(path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        with open(os.path.join(call.work_dir, CARRIED), encoding="utf-8") as fh:
            added = json.load(fh)["added"]
        count = f"count {data['count']}" if "count" in data else "not counted"
        return f"Readback — {' | '.join(data['lines'])} ({count}, {len(added)} added)"

    return call.commit_staged(staged, kind=KIND, fmt=FMT, transform=splice if rendered.payload else None,
                              warnings=[rendered.note] if rendered.note else None, readback=readback)


async def handle_write_counted(args: dict, render=toy_render) -> str:
    return await handle_write_call(args, TEST_CATALOG, toy_stage, kind=KIND, render=render, finish=toy_finish)


def contract_job(tmp_dir: str) -> ToyJob:
    return ToyJob()


def contract_fixtures(tmp_dir: str) -> dict[str, str]:
    lines = os.path.join(tmp_dir, "lines.txt")
    with open(lines, "w", encoding="utf-8") as fh:
        fh.write("one\ntwo\n")
    return {"lines.txt": lines, "export.txt": os.path.join(tmp_dir, "export.txt")}
