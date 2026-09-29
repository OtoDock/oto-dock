"""The schema kind (CHECKS.md): the turn's result must carry a fenced JSON
block that satisfies the check's JSON schema. The findings are the
validator's messages with their JSON paths. No session, no script."""

from __future__ import annotations

import json
import re
import time

from services.checks import kinds
from services.checks.render import Verdict

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def last_json_block(text: str):
    """The last fenced JSON block of a text, parsed; else the last top-level
    object the text ends with; else None."""
    if not text:
        return None
    blocks = _FENCE_RE.findall(text)
    for raw in reversed(blocks):
        try:
            return json.loads(raw)
        except ValueError:
            continue
    stripped = text.rstrip()
    end = stripped.rfind("}")
    if end == -1:
        return None
    depth = 0
    for i in range(end, -1, -1):
        ch = stripped[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stripped[i:end + 1])
                except ValueError:
                    return None
    return None


async def run(check, target, changed: dict, *, round_no: int) -> Verdict:
    started = time.monotonic()
    schema = check.doc.get("schema") or {}
    obj = last_json_block(changed.get("result") or "")
    ms = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
    if obj is None:
        return Verdict(section="schema", status="fail", findings=[{
            "location": "result", "severity": "error",
            "text": "the answer carries no JSON block; end it with a ```json block that "
                    "matches the schema"}],
            summary="no JSON block in the answer", duration_ms=ms())
    from services.checks import patterns
    errors = sorted(patterns.validator_class()(schema).iter_errors(obj),
                    key=lambda e: list(e.absolute_path))
    if not errors:
        return Verdict(section="schema", status="pass", passed=True,
                       summary="the answer matches the schema", duration_ms=ms())
    findings = []
    for e in errors[:20]:
        loc = "/".join(str(p) for p in e.absolute_path) or "(root)"
        findings.append({"location": loc, "severity": "error", "text": e.message[:400]})
    return Verdict(section="schema", status="fail", findings=findings,
                   summary=f"{len(errors)} schema violation{'s' if len(errors) != 1 else ''}",
                   duration_ms=ms())


kinds.register("schema", run)
