"""Static invariants over the GitHub Actions workflows: every action
is pinned to a full commit SHA, and no checkout persists a token into the
job's git configuration. The public cut ships a subset of the files; the
checks run over whatever workflow files exist next to this checkout.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
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


# ── the proxy suite's parts and the two checks branch protection requires ───

_GUARD = "${{ always() && github.repository == 'OtoDock/oto-dock' }}"


@pytest.mark.parametrize(("required", "parts"), [("proxy", "proxy-shard"), ("proxy-arm64", "proxy-arm64-shard")])
def test_a_required_proxy_check_stands_for_every_part_of_the_suite(required, parts):
    """The suite runs in parts side by side; the job branch protection names
    waits for them all, runs even when one failed or was cancelled (a check
    that was skipped would let a merge through) and only in this repository
    (elsewhere the parts never start, and a bare ``always()`` would fail)."""
    jobs = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())["jobs"]
    check, part = jobs[required], jobs[parts]
    assert check["needs"] == parts
    assert check["if"] == _GUARD
    assert not any("uses" in step for step in check["steps"])
    result = [step for step in check["steps"] if "RESULT" in (step.get("env") or {})]
    assert len(result) == 1
    assert result[0]["env"]["RESULT"] == "${{ needs." + parts + ".result }}"
    assert result[0]["run"] == 'test "$RESULT" = success'
    # Each part runs to its end, and together they are the whole suite.
    shards = part["strategy"]["matrix"]["shard"]
    assert part["strategy"]["fail-fast"] is False
    assert shards == list(range(1, len(shards) + 1)) and len(shards) > 1
    runs = [step["run"] for step in part["steps"] if "--shard" in str(step.get("run", ""))]
    assert len(runs) == 1 and f'--shard "$SHARD/{len(shards)}"' in runs[0]
    suite = next(step for step in part["steps"] if "--shard" in str(step.get("run", "")))
    assert suite["env"]["SHARD"] == "${{ matrix.shard }}" and "if" not in suite


# ── the contributor agreement's check ───────────────────────────────────────

_REPO = _WORKFLOWS.parents[1]
_OWN_BRANCHES_SKIP = "github.event.pull_request.head.repo.full_name != github.repository"


def _cla_job() -> dict:
    return yaml.safe_load((_WORKFLOWS / "cla.yml").read_text())["jobs"]["cla"]


def _cla_verdict(body: str, kind: str = "User") -> int:
    """The exit code of the workflow's own script for one pull request."""
    run = next(step["run"] for step in _cla_job()["steps"] if "run" in step)
    script = textwrap.dedent(re.search(r"<<'PY'\n(.*)\nPY", run, re.S).group(1))
    env = {**os.environ, "BODY": body, "AUTHOR": "someone", "KIND": kind}
    return subprocess.run([sys.executable, "-c", script], cwd=_REPO, env=env,
                          capture_output=True, check=False).returncode


def test_the_cla_check_judges_forks_and_never_runs_their_code():
    """It runs from the base branch with the pull request's words only: a
    checkout of the head would run a stranger's code with this repository's
    token. A pull request from a branch of the repository itself is skipped,
    which branch protection counts as passed."""
    workflow = yaml.safe_load((_WORKFLOWS / "cla.yml").read_text())
    assert set(workflow[True]) == {"pull_request_target"}      # YAML reads the key `on` as True
    assert workflow["permissions"] == {"contents": "read"}
    job = _cla_job()
    assert job["if"] == _OWN_BRANCHES_SKIP
    checkouts = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert [step["with"] for step in checkouts] == [{"persist-credentials": False}]
    script = next(step for step in job["steps"] if "run" in step)
    assert "${{" not in script["run"]                           # the body arrives by environment, never by template


def test_a_ticked_line_of_the_template_is_what_the_cla_check_accepts():
    template = (_REPO / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text()
    boxes = [line for line in template.splitlines() if "CLA.md" in line and line.startswith("- [ ] ")]
    assert len(boxes) == 2, "the template carries the personal line and the company line"
    assert _cla_verdict(template) == 1
    for box in boxes:
        ticked = box.replace("- [ ] ", "- [x] ", 1)
        assert _cla_verdict(template.replace(box, ticked)) == 0
        assert _cla_verdict(ticked.replace("[x]", "[X]") + "\r\n") == 0
        assert _cla_verdict(re.sub(r"version \d+\.\d+", "version 0.1", ticked)) == 1
    assert _cla_verdict("") == 1
    assert _cla_verdict("", kind="Bot") == 0
