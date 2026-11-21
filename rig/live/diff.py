#!/usr/bin/env python3
"""What a CLI bump moved: two runs' raw output compared by shape.

    python3 rig/live/diff.py <run folder> <run folder>

Shape means the frame types in order (a repeat folded), each type's keys and the
transcript's event types, so a changed word in a model's answer shows nothing and
a new frame, a missing key or a new transcript event does.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def shape(folder: Path) -> dict:
    """The shape of one behaviour's raw folder."""
    order: list[str] = []
    keys: dict[str, set[str]] = {}
    frames = folder / "frames.jsonl"
    if frames.exists():
        for line in frames.read_text().splitlines():
            frame = json.loads(line)
            kind = frame.get("type", "?")
            if not order or order[-1] != kind:
                order.append(kind)
            keys.setdefault(kind, set()).update(frame)
    events: set[str] = set()
    transcript = folder / "transcript.jsonl"
    if transcript.exists():
        for line in transcript.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                events.add(str(row.get("type", "?")))
    return {"order": order, "keys": {k: sorted(v) for k, v in keys.items()}, "events": sorted(events)}


def behaviours(run: Path) -> dict[str, Path]:
    root = run / "raw"
    return {str(p.parent.relative_to(root)): p.parent for p in root.rglob("frames.jsonl")} if root.exists() else {}


def compare(old: Path, new: Path) -> list[str]:
    lines: list[str] = []
    before, after = behaviours(old), behaviours(new)
    for name in sorted(set(before) | set(after)):
        if name not in before:
            lines.append(f"+ {name}: new in this run")
            continue
        if name not in after:
            lines.append(f"- {name}: gone from this run")
            continue
        a, b = shape(before[name]), shape(after[name])
        if a["order"] != b["order"]:
            lines.append(f"~ {name}: frames {a['order']} -> {b['order']}")
        for kind in sorted(set(a["keys"]) & set(b["keys"])):
            if a["keys"][kind] != b["keys"][kind]:
                gone = sorted(set(a["keys"][kind]) - set(b["keys"][kind]))
                came = sorted(set(b["keys"][kind]) - set(a["keys"][kind]))
                lines.append(f"~ {name}: {kind} keys, gone {gone}, new {came}")
        if a["events"] != b["events"]:
            lines.append(f"~ {name}: transcript events {a['events']} -> {b['events']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    lines = compare(Path(args[0]), Path(args[1]))
    print("\n".join(lines) if lines else "the two runs have one shape")
    return 0


if __name__ == "__main__":
    sys.exit(main())
