"""Static invariants over the GitHub Actions workflows: every action
is pinned to a full commit SHA, and no checkout persists a token into the
job's git configuration. The public cut ships a subset of the files; the
checks run over whatever workflow files exist next to this checkout.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
_SHA_REF = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _workflow_files() -> list[Path]:
    return sorted(p for p in _WORKFLOWS.glob("*.y*ml") if p.is_file())


def _uses(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                yield value
            else:
                yield from _uses(value)
    elif isinstance(node, list):
        for value in node:
            yield from _uses(value)


def _steps(workflow: dict):
    for name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield name, step


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path):
    refs = list(_uses(yaml.safe_load(path.read_text())))
    assert refs, f"{path.name} declares no actions"
    unpinned = [r for r in refs if not r.startswith(("./", "docker://")) and not _SHA_REF.match(r)]
    assert not unpinned, f"{path.name}: not pinned to a full commit SHA: {unpinned}"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_no_checkout_persists_credentials(path):
    for job, step in _steps(yaml.safe_load(path.read_text())):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert (step.get("with") or {}).get("persist-credentials") is False, (
                f"{path.name}: job {job} checks out with a persisted token"
            )


def test_the_workflow_folder_is_present():
    assert _workflow_files(), "no workflow files found"
