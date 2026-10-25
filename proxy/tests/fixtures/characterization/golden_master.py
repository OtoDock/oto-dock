"""The golden-master compare the characterization suites share: a value is
dumped as sorted, indented JSON and compared with ``golden/<name>.json`` byte
for byte; a mismatch fails with the unified diff. ``CHARACTERIZATION_WRITE=1``
writes the goldens instead (done once from the tree before a refactor, never
by the refactor: a drift is a behaviour change to explain)."""
import difflib
import json
import os
import pathlib

import pytest

FIX = pathlib.Path(__file__).parent
GOLD = FIX / "golden"
WRITE = os.environ.get("CHARACTERIZATION_WRITE") == "1"


def dump(value) -> str:
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False, default=_default) + "\n"


def _default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return repr(value)


def check_golden(name: str, got) -> None:
    path = GOLD / f"{name}.json"
    text = dump(got)
    if WRITE:
        path.write_text(text)
        pytest.fail(f"golden {name} written; run again without CHARACTERIZATION_WRITE")
    assert path.exists(), f"no golden for {name}: write it with CHARACTERIZATION_WRITE=1"
    want = path.read_text()
    if want != text:
        diff = "".join(difflib.unified_diff(
            want.splitlines(True), text.splitlines(True), "golden", "live", n=4))
        pytest.fail(f"{name} drifted from its golden:\n{diff[:30000]}")


def load_lines(name: str) -> list[str]:
    """A JSONL recording as its lines (blank and unparsable ones kept: the
    tailers meet them)."""
    return (FIX / name).read_text().split("\n")


def load_jsonl(name: str) -> list[dict]:
    return [json.loads(ln) for ln in load_lines(name) if ln.strip()]


def load_json(name: str):
    return json.loads((FIX / name).read_text())
