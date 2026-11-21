"""The run's results: ``report.json``, ``report.md`` and the verdict."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .scenario import (
    CARRIED,
    FAIL,
    FIXED,
    INCONCLUSIVE,
    KNOWN,
    NOT_SUPPORTED,
    PASS,
    SKIPPED,
)

ORDER = (PASS, FAIL, KNOWN, FIXED, INCONCLUSIVE, NOT_SUPPORTED, CARRIED, SKIPPED)


def verdict(results: list[dict]) -> str:
    """``fail`` when anything failed, or was skipped because something it needs
    did not pass; else ``pass``. A known failure, an inconclusive behaviour and
    what an engine does not support do not fail a run, and the report shows
    them apart."""
    if not results:
        return FAIL
    return FAIL if any(r["result"] in (FAIL, SKIPPED) for r in results) else PASS


def release_verdict(results: list[dict]) -> str:
    """What a release reads: a known product failure stops it too, and so does a
    row carried from an earlier run, since a release runs everything on a fresh
    install. A behaviour a real model did not do (``inconclusive``) does not stop
    it here: the count is bounded where the release reads it."""
    if verdict(results) != PASS:
        return FAIL
    return FAIL if any(r["result"] in (KNOWN, CARRIED) for r in results) else PASS


def counts(results: list[dict]) -> dict[str, int]:
    return {word: sum(1 for r in results if r["result"] == word) for word in ORDER if any(r["result"] == word for r in results)}


def write_report(folder: Path, *, layer: str, results: list[dict], started: float, context: dict,
                 lost_at: str = "") -> dict:
    """``lost_at``: the row after which the install was lost and the run ended; its
    first line says so, since the rows after it say nothing."""
    summary = {
        "layer": layer,
        "verdict": FAIL if lost_at else verdict(results),
        "lost_at": lost_at,
        "release": release_verdict(results),
        "counts": counts(results),
        "seconds": round(time.time() - started),
        "context": context,
        "results": results,
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"report-{layer}.json").write_text(json.dumps(summary, indent=1))
    lines = [
        f"# Rig report: {layer} layer",
        "",
        f"Verdict: **{summary['verdict']}** in {summary['seconds']} s"
        + (f": the install was lost at {lost_at}, and the "
           f"{sum(1 for r in results if str(r.get('detail', '')).startswith('not run: the install was lost'))} "
           "rows after it did not run. " if lost_at else ". ")
        + ", ".join(f"{n} {word}" for word, n in summary["counts"].items()) + ".",
        "",
        *[f"- {key}: {value}" for key, value in context.items()],
        "",
        "| Scenario | Result | Seconds | Detail |",
        "|---|---|---|---|",
    ]
    for r in results:
        detail = str(r.get("detail", "")).replace("|", "/").replace("\n", " ")[:300]
        lines.append(f"| {r['name']} | {r['result']} | {r.get('seconds', 0)} | {detail} |")
    (folder / f"report-{layer}.md").write_text("\n".join(lines) + "\n")
    return summary
