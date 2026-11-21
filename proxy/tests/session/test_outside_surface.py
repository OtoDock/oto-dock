"""Every place a session gets something reads the one rule's answer.

An outside session gets no tool, route, mount or built-in tool a manager did
not pick. The one rule is ``mcp_session_filter.session_exclusion_reason``,
read with the picks; what it attached is recorded on ``AgentConfig.
excluded_mcps``, and the sandbox's mounts and routes are built from the
attached set. A new reader of the rule, a new builder of a session's MCP
config, a new taker of the agent's manifests for a mount or a route, is
listed here with the way it reads the picks, or this test fails: a site
that forgets them is the silent hole this lane closes.

Two passes over every tracked ``.py`` under ``proxy/`` (tests excluded):

1. every call of the functions of the one rule (``session_exclusion_reason``,
   ``filter_manifests_for_session``, ``preview_session_mcps``), of the
   session config builder (``build_session_mcp_config``) and of the skills
   set a config dir materializes (``get_on_demand_skills_for_materialization``):
   the site passes the picks (``picks=`` / ``outside_picks=``) or is listed
   as a reader that never serves an outside session;
2. every call that hands a session its mounts or its routes
   (``mcp_sandbox_mounts``, ``resolve_sandbox_config``,
   ``resolve_sandbox_egress``): the site passes the attached names, or is
   listed with the reason it may not.

The counts are exact in both directions. A taker of the agent's manifests
for a mount or a route is read through those two: the layers build the
attached list from ``AgentConfig.excluded_mcps`` and hand it on.
"""

from __future__ import annotations

import ast
import subprocess
from collections import Counter
from pathlib import Path

from tests._paths import PROXY_DIR, REPO_ROOT

RULE = ("session_exclusion_reason", "filter_manifests_for_session", "preview_session_mcps",
        "build_session_mcp_config", "get_on_demand_skills_for_materialization")
HANDS = ("mcp_sandbox_mounts", "resolve_sandbox_config", "resolve_sandbox_egress")
# A session kind with its own rule (the wrap-up after a conversation, the
# agent's own line) passes that rule where an outside session passes picks.
PICK_KEYS = ("picks", "outside_picks", "kind_rule")
ATTACHED_KEYS = ("attached", "attached_mcps")

# {(path, function name): (count, how it reads the picks or the attached set)}.
# A site with "never outside" serves no outside session by construction; a
# site with "defined" is the function's own definition or its facade.
KNOWN: dict[tuple[str, str], tuple[int, str]] = {
    # The rule and its facade.
    ("services/mcp/mcp_session_filter.py", "session_exclusion_reason"): (
        2, "filter_manifests_for_session and preview_session_mcps pass picks= through"),
    ("services/mcp/mcp_session_config.py", "session_exclusion_reason"): (
        1, "_SessionMcpBuild._excluded passes picks=self.outside_picks"),
    ("services/mcp/mcp_agent_skills.py", "session_exclusion_reason"): (
        2, "the prompt catalog and the one skill filter (behind the inline skills, the catalog and the "
           "materialized set) pass picks=outside_picks, which build_agent_prompt and the config dir "
           "builders hand them"),
    ("services/mcp/outside_picks.py", "session_exclusion_reason"): (
        1, "a_call_can_get asks the rule for one tool picked whole, with a caller's contexts: what the "
           "External identities card offers, never a session's build"),
    ("services/mcp/mcp_sync.py", "session_exclusion_reason"): (
        1, "never outside: lifts tools for a remote session, and an outside session never resolves remote "
           "(remote_status.resolve_execution_target keeps a session with no person local)"),
    ("api/phone/phone.py", "preview_session_mcps"): (
        1, "the route form's preview passes picks=outside_picks.load(agent) for an external identity"),
    ("core/config/phone_config_builder.py", "build_session_mcp_config"): (
        2, "the outside session's build passes outside_picks=picks; the wrap-up after a conversation "
           "is no outside session and passes its own rule (kind_rule=wrapup_rule)"),
    ("core/config/config_builder.py", "build_session_mcp_config"): (
        1, "never outside: a person's own chat, or a Shared-only chat with a person"),
    ("core/config/task_config_builder.py", "build_session_mcp_config"): (
        1, "never outside: a task run or a delivery"),
    ("services/meetings/meeting_context.py", "build_session_mcp_config"): (
        1, "never outside: a meeting participant"),
    ("services/apps/headless_exec.py", "build_session_mcp_config"): (
        1, "never outside: an app button's run and the tool listing's one-off manager, neither an outside session"),
    ("services/scheduler/wake_session.py", "build_session_mcp_config"): (
        1, "never outside: a delivery's own build of a task's or a chat's session"),
    ("core/sandbox/skills_materializer.py", "get_on_demand_skills_for_materialization"): (
        1, "the materializer passes outside_picks=, which the config dir builders hand it for an outside home"),
    # The mounts and the routes.
    ("core/layers/cli/layer.py", "mcp_sandbox_mounts"): (1, "the attached manifests"),
    ("core/layers/cli/layer.py", "resolve_sandbox_config"): (1, "attached_mcps= and the outside facts"),
    ("core/layers/codex/spawn.py", "mcp_sandbox_mounts"): (1, "the attached manifests"),
    ("core/layers/codex/spawn.py", "resolve_sandbox_config"): (1, "attached_mcps= and the outside facts"),
    ("core/layers/direct/layer.py", "mcp_sandbox_mounts"): (1, "the attached manifests"),
    ("core/layers/direct/layer.py", "resolve_sandbox_config"): (1, "attached_mcps= and the outside facts"),
    ("core/sandbox/sandbox.py", "resolve_sandbox_egress"): (
        1, "resolve_sandbox_config passes attached=attached_mcps"),
    ("services/apps/headless_exec.py", "resolve_sandbox_config"): (
        1, "never outside: an app button's run, with its own included set"),
    ("services/scripts/runner.py", "resolve_sandbox_config"): (
        1, "never outside: a check's script runs for a judged session (a chat's, a task's), never a call's"),
}

_SKIP_PARTS = {"tests", "venv", ".venv", "__pycache__", "docs"}


def _tracked_py() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "proxy"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    files = []
    for line in out.stdout.splitlines():
        p = REPO_ROOT / line
        if p.suffix == ".py" and not (set(p.relative_to(REPO_ROOT).parts) & _SKIP_PARTS):
            files.append(p)
    return files


# A call made through a thread or executor wrapper names its function as
# the wrapper's first argument and passes the keywords to the wrapper.
_WRAPPERS = ("to_thread", "run_db", "run_db_fast", "run_in_executor")


def _name_of(expr) -> str:
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return ""


def _called(node: ast.Call) -> str:
    name = _name_of(node.func)
    if name in _WRAPPERS and node.args:
        return _name_of(node.args[0])
    return name


def _sites() -> tuple[Counter, dict[tuple[str, str], bool]]:
    """``(counts, reads)``: how many calls of each watched name per file and
    whether every call there passes the picks or the attached names."""
    counts: Counter = Counter()
    reads: dict[tuple[str, str], bool] = {}
    for path in _tracked_py():
        rel = str(path.relative_to(PROXY_DIR))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called(node)
            if name not in RULE + HANDS:
                continue
            key = (rel, name)
            counts[key] += 1
            kws = {k.arg for k in node.keywords if k.arg}
            wanted = PICK_KEYS if name in RULE else ATTACHED_KEYS
            passes = bool(kws & set(wanted))
            if name == "mcp_sandbox_mounts":
                # Its one argument is the attached list, by that name.
                passes = bool(node.args) and _name_of(node.args[0]) == "attached"
            reads[key] = reads.get(key, True) and passes
    return counts, reads


def test_every_site_is_known_and_reads_the_picks_or_says_why():
    counts, reads = _sites()
    found = {k: c for k, c in counts.items()}
    missing = {k: v for k, v in KNOWN.items() if k not in found}
    assert not missing, f"listed sites that no longer exist: {missing}"
    unknown = {k: c for k, c in found.items() if k not in KNOWN}
    assert not unknown, (
        f"new readers of the rule or new takers of the mounts and routes: {unknown}. "
        "Pass the picks (an outside session) or the attached names, and list the site here.")
    drift = {k: (c, KNOWN[k][0]) for k, c in found.items() if c != KNOWN[k][0]}
    assert not drift, f"a site's count moved (found, listed): {drift}"
    for key, (_count, how) in KNOWN.items():
        if how.startswith("never outside") or how.startswith("defined"):
            continue
        assert reads[key], f"{key} does not pass the picks or the attached names at every call ({how})"


# Every builder of a session's security context passes the agent's tool
# limits for the session's kind (``tool_limits``, the pairs the hook reads),
# or is listed here with why it carries none.
KNOWN_CONTEXTS: dict[tuple[str, int], str] = {
    ("core/session/session_state.py", 1): "the index's reload of a persisted context, every field as kept",
    ("core/config/phone_config_builder.py", 1): "an outside session (the first construction in the file): its picks are its grants, a limit never applies",
}


def _context_sites() -> dict[tuple[str, int], bool]:
    """``{(path, ordinal): passes tool_limits=}`` for every construction of
    a SecurityContext outside the tests and the class's own module."""
    out: dict[tuple[str, int], bool] = {}
    for path in _tracked_py():
        rel = str(path.relative_to(PROXY_DIR))
        if rel == "auth/path_policy.py":
            continue
        calls = sorted(
            (node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
             if isinstance(node, ast.Call) and _name_of(node.func) == "SecurityContext"),
            key=lambda node: node.lineno)
        for n, node in enumerate(calls, 1):
            out[(rel, n)] = "tool_limits" in {k.arg for k in node.keywords if k.arg}
    return out


def test_every_security_context_carries_the_tool_limits_or_says_why():
    sites = _context_sites()
    missing = {k: v for k, v in KNOWN_CONTEXTS.items() if k not in sites}
    assert not missing, f"listed sites that no longer exist: {missing}"
    bare = [k for k, passes in sites.items() if not passes]
    unknown = [k for k in bare if k not in KNOWN_CONTEXTS]
    assert not unknown, (f"a session's security context built without the agent's tool limits: {unknown}. "
                         "Pass tool_limits= (services/mcp/tool_limits.grants), or list the site here.")
    assert len([k for k in sites if k[0] == "core/config/phone_config_builder.py"]) == 2


def test_the_definitions_themselves_are_not_counted():
    """A definition is no call: the rule's own module defines it once."""
    counts, _reads = _sites()
    assert ("services/mcp/mcp_session_filter.py", "filter_manifests_for_session") not in counts
    assert ("services/mcp/mcp_egress.py", "resolve_sandbox_egress") not in counts
