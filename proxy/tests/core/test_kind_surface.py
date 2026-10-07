"""The kind surface — the acceptance test of core-seams phase 9, in the
shape of ``tests/core/test_status_surface.py``.

Four kind vocabularies, each named once and mirrored once: the artifact
kinds (``core/events/artifact_events.py`` → ``lib/kinds/artifact.ts``), the
task / run / trigger kinds (``services/scheduler/task_kinds.py`` →
``lib/kinds/task.ts``), the app kind (``storage/db_apps.py`` →
``lib/kinds/app.ts``) and the MCP runtime
(``services/mcp/mcp_manifest_types.py`` → ``lib/kinds/mcpRuntime.ts``).
Generic code asks the facts or compares the constants; a member spelled as
a compare operand anywhere else is listed here WITH its reason, and this
test keeps the list exact in both directions.

A machine's DISTINCTIVE words (``image_generating``, ``document_preview``,
``one-time``, ``app_handler``, ``folder``, ``docker`` …) are scanned
tree-wide; its GENERIC words (``url``, ``file``, ``ui``, ``scheduled``,
``check``, ``app``, ``python``, ``none`` …) only inside the machine's
DOMAIN — the files that read the machine — because elsewhere the same word
is another vocabulary (a file kind, a notification mode, a share target, an
interpreter). A keyed map (``LABELS[kind]``) is a lookup and is never
scored; the release gate's vocabulary check does not score it either. A hand-written TypeScript union of an axis's words is a site the
scan cannot see, so the last pass greps for them.

Python: ``Compare`` operands (tuple / list / set members included),
``startswith`` / ``endswith`` arguments and ``match`` cases over the tracked
``.py`` files under ``proxy/``, ``satellite/`` and ``mcps/custom/`` (tests,
docs, venvs, vendored copies and skills excluded); the new leaf is used
module-qualified. TypeScript: ``=== '…'`` / ``!== '…'``, ``case '…':`` and
``['…'].includes(`` over ``dashboard/src`` (tests excluded).
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tests._paths import REPO_ROOT

_SCAN_ROOTS = ("proxy", "satellite", "mcps/custom")
_EXCLUDED_PARTS = {"tests", "docs", "venv", ".venv", "_vendored", "skills", "node_modules", "__pycache__"}
_PREFIX_METHODS = ("startswith", "endswith")


@dataclass(frozen=True)
class Machine:
    name: str
    distinctive: frozenset[str]
    generic: frozenset[str]
    homes: tuple[str, ...]          # exact paths: never scored
    domain: tuple[str, ...]         # path prefixes where the generic words are this machine's
    ts_homes: tuple[str, ...]
    ts_domain: tuple[str, ...]


_TS = "dashboard/src/"

MACHINES: tuple[Machine, ...] = (
    Machine("artifact",
            frozenset({"images", "image_generating", "image_gen_failed", "media_processing", "media_failed",
                       "document_preview"}),
            frozenset({"url", "file", "video", "audio", "ui"}),
            ("proxy/core/events/artifact_events.py",),
            ("proxy/core/events/", "proxy/ws/dashboard_pty.py", "proxy/services/sharing/chat_snapshot.py",
             "proxy/core/session/interactive_session.py"),
            (_TS + "lib/kinds/artifact.ts",),
            (_TS + "hooks/useChatStream.ts", _TS + "hooks/useArtifactWindows.ts", _TS + "hooks/useChatMessages.ts",
             _TS + "hooks/useDashboardWs.ts", _TS + "lib/displayReplay.ts", _TS + "lib/messageBlocks.ts",
             _TS + "api/wireEvents.ts", _TS + "components/chat/artifacts/ArtifactWindows.tsx")),
    Machine("task",
            frozenset({"one-time", "one_time", "continuation", "app_handler", "app_action", "triggered"}),
            frozenset({"scheduled", "trigger", "manual", "check", "app", "delegate"}),
            ("proxy/services/scheduler/task_kinds.py",),
            ("proxy/services/scheduler/", "proxy/api/tasks/", "proxy/storage/automation/",
             "proxy/storage/billing/db_usage.py", "proxy/storage/checks/db_checks.py",
             "proxy/services/checks/evaluator.py", "proxy/core/config/task_config_builder.py",
             "proxy/api/apps/manifest.py", "proxy/services/apps/app_handlers.py"),
            (_TS + "lib/kinds/task.ts",),
            (_TS + "lib/runs.ts", _TS + "lib/format.ts", _TS + "pages/History.tsx",
             _TS + "components/GroupedRunsTable.tsx", _TS + "components/chat/TaskMetadata.tsx",
             _TS + "pages/agent/AgentTriggers.modals.tsx", _TS + "api/tasks.ts", _TS + "api/runs.ts")),
    Machine("app_kind",
            frozenset({"folder"}),
            frozenset({"file"}),
            ("proxy/storage/db_apps.py",),
            ("proxy/api/apps/", "proxy/api/hooks/", "proxy/api/sharing/", "proxy/services/apps/",
             "proxy/services/checks/kinds/handler_kind.py", "proxy/services/scheduler/trigger_manager.py"),
            (_TS + "lib/kinds/app.ts",),
            (_TS + "api/apps.ts", _TS + "components/apps/", _TS + "pages/apps/",
             _TS + "pages/agent/AgentTriggers.modals.tsx")),
    Machine("mcp_runtime",
            frozenset({"docker"}),
            frozenset({"python", "node", "none"}),
            ("proxy/services/mcp/mcp_manifest_types.py",),
            ("proxy/services/mcp/mcp_sync.py", "proxy/services/mcp/mcp_venv_bootstrap.py",
             "proxy/services/mcp/mcp_installer.py", "proxy/services/community/community_catalog.py",
             "proxy/services/community/community_installer.py", "proxy/services/community/skills_installer.py",
             "proxy/services/oauth/credential_resolver.py"),
            (_TS + "lib/kinds/mcpRuntime.ts",),
            (_TS + "api/mcps.ts", _TS + "api/community.ts", _TS + "pages/admin/McpServersPage.row.tsx",
             _TS + "pages/admin/McpServersPage.tsx", _TS + "pages/admin/McpServersPage.installModal.tsx")),
)

# {path: (reason, {"machine:word": count})} — sites outside a machine's
# home that compare one of its words, each ANOTHER vocabulary sharing the
# word, a string read over the API, or a copy the satellite runs.
KNOWN_SITES: dict[str, tuple[str, dict[str, int]]] = {
    "mcps/custom/display-mcp/app_tools.py": (
        "the display MCP reads the app kind as a string over the API", {"app_kind:folder": 1}),
    "mcps/custom/schedules-mcp/server.py": (
        "the schedules MCP reads the task kinds as strings over the API (its trigger / app compares are "
        "generic words outside the task domain and score nowhere)", {"task:one_time": 1}),
    "proxy/api/notifications/notifications.py": (
        "the notification kind (one_time | recurring), not a task", {"task:one_time": 2}),
    "proxy/services/notifications/notification_manager.py": (
        "the notification kind (one_time | recurring), not a task", {"task:one_time": 2}),
    "proxy/core/config/task_config_builder.py": (
        "the notification mode (auto | manual | none), not a run's origin", {"task:manual": 1}),
    "proxy/services/community/skills_installer.py": (
        "the transport word none (a context-only MCP has no transport), not a runtime", {"mcp_runtime:none": 1}),
    "proxy/services/oauth/credential_resolver.py": (
        "the credential type none (no credentials to inject), not a runtime", {"mcp_runtime:none": 1}),
    "proxy/services/sharing/chat_snapshot.py": (
        "a media block's src_kind (url | token), not an artifact kind", {"artifact:url": 1}),
    "proxy/services/mcp/mcp_installer.py": (
        "vendored byte-for-byte into the satellite (SHARED_MCP_INSTALLER_HASH): the installer's dispatch "
        "keeps its words — a change is a satellite release", {"mcp_runtime:docker": 1, "mcp_runtime:node": 1,
                                                                 "mcp_runtime:python": 2}),
    "proxy/services/mcp/mcp_venv_bootstrap.py": (
        "the bootstrap's dispatch by runtime compares the leaf's constants — this row is the twin marker "
        "string 'python' the marker file keys on", {}),
    "satellite/sessions/mcp_install_support.py": (
        "the satellite's own runtime reconcile — no authority to import on that side", {}),
}

# {path: (reason, {"machine:word": count})} — dashboard files that keep a
# quoted word, each another vocabulary sharing it or a renderer per kind.
KNOWN_TS_SITES: dict[str, tuple[str, dict[str, int]]] = {
    "dashboard/src/components/chat/ChatBlockRenderer.tsx": (
        "the renderer per kind (a gallery needs a gallery component) and the app_action block kind",
        {"artifact:images": 1, "artifact:image_generating": 1, "artifact:media_processing": 1,
         "artifact:document_preview": 1, "task:app_action": 1}),
    "dashboard/src/components/chat/artifacts/ArtifactView.tsx": (
        "the PiP renderer per kind", {"artifact:images": 1, "artifact:image_generating": 1,
                                       "artifact:media_processing": 1}),
    "dashboard/src/components/chat/artifacts/ArtifactDock.tsx": (
        "the PiP dock's icon per kind", {"artifact:images": 1, "artifact:image_generating": 1,
                                          "artifact:media_processing": 1}),
    "dashboard/src/hooks/useArtifactWindows.ts": (
        "the window title per kind (titleFor) and a skeleton's media kind (audio | video); the eviction "
        "and identity rules ask the mirror",
        {"artifact:images": 1, "artifact:image_generating": 1, "artifact:media_processing": 1,
         "artifact:video": 1, "artifact:audio": 2, "artifact:url": 1, "artifact:file": 1, "artifact:ui": 1}),
    "dashboard/src/hooks/useChatStream.ts": (
        "a transcode skeleton's media kind (audio | video), not an artifact kind", {"artifact:audio": 1}),
    "dashboard/src/lib/messageBlocks.ts": (
        "ONE kind each: a preview's last push per file in a message, a ui page's supersede-to-chip by "
        "path, and a skeleton's media kind",
        {"artifact:document_preview": 2, "artifact:ui": 2, "artifact:audio": 1}),
    "dashboard/src/hooks/useChatMessages.ts": (
        "ONE kind's card per file in the streaming message (a preview's push replaces its card)",
        {"artifact:document_preview": 1}),
    "dashboard/src/components/chat/artifacts/ArtifactWindows.tsx": (
        "a window size for the iframe-hosting kind (a ui page)", {"artifact:ui": 1}),
    "dashboard/src/components/apps/AppFrame.tsx": (
        "the app_action WIRE frame (phase 6's axis), not a run's origin", {"task:app_action": 1}),
    "dashboard/src/sharehost/host.ts": (
        "the app_action block kind on the share host", {"task:app_action": 1}),
    "dashboard/src/components/workspace/workspaceIcons.tsx": (
        "an icon name (folder), not an app kind", {"app_kind:folder": 1}),
}


def _tracked_files(suffixes: tuple[str, ...], roots: tuple[str, ...]) -> list[str]:
    """The tracked files plus the untracked ones git does not ignore — a
    module written this turn is seen before it is committed."""
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard", *roots],
                             capture_output=True, check=True)
        rel = [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        rel = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for root in roots for s in suffixes for p in (REPO_ROOT / root).rglob(f"*{s}")]
    files = [p for p in rel if p.endswith(suffixes) and not (set(Path(p).parts) & _EXCLUDED_PARTS)
             and (REPO_ROOT / p).is_file()]
    assert len(files) > 50, "the scan found almost nothing — a checkout problem, not a clean tree"
    return files


def _compare_operands(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in (node.left, *node.comparators):
                elts = op.elts if isinstance(op, (ast.Tuple, ast.List, ast.Set)) else [op]
                yield from elts
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _PREFIX_METHODS:
            for a in node.args:
                elts = a.elts if isinstance(a, (ast.Tuple, ast.List)) else [a]
                yield from elts
        elif isinstance(node, ast.Match):
            for case in node.cases:
                for n in ast.walk(case.pattern):
                    if isinstance(n, ast.MatchValue):
                        yield n.value


def _literal_counts(tree: ast.AST) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for e in _compare_operands(tree):
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            counts[e.value] += 1
    return counts


def _in(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p) for p in prefixes)


def _sites_of(path: str, counts: dict[str, int], m: Machine, homes: tuple[str, ...],
              domain: tuple[str, ...]) -> dict[str, int]:
    if _in(path, homes):
        return {}
    found: dict[str, int] = {}
    for word, n in counts.items():
        if word in m.distinctive or (word in m.generic and _in(path, domain)):
            found[f"{m.name}:{word}"] = n
    return found


def _named_leaf_imports(tree: ast.AST) -> list[str]:
    return [f"{n.module}: {a.name}" for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "services.scheduler.task_kinds" for a in n.names]


def _scan_python():
    sites: dict[str, dict[str, int]] = {}
    imports: dict[str, list[str]] = {}
    for rel in _tracked_files((".py",), _SCAN_ROOTS):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8", errors="surrogateescape"))
        counts = _literal_counts(tree)
        found: dict[str, int] = {}
        for m in MACHINES:
            found.update(_sites_of(rel, counts, m, m.homes, m.domain))
        if found:
            sites[rel] = dict(sorted(found.items()))
        if names := _named_leaf_imports(tree):
            imports[rel] = names
    return sites, imports


_HINT = ("ask the kind's facts (artifact_events.kind_of(...).evicts / .saved / .shareable, "
         "task_kinds.of(task).fires / .judge / .worker, task_kinds.run_kind_of, task_kinds.self_removes, "
         "db_apps.app_kind_of(row).may_serve / .serves_tree / .keeps_data, mcp_manifest_types.is_container / "
         "installs_on_host / has_process) or compare the constants; a site that is another vocabulary sharing "
         "the word is listed in KNOWN_SITES with its reason")


def _diff(found: dict[str, dict[str, int]], known: dict[str, tuple[str, dict[str, int]]]) -> str:
    expected = {p: c for p, (_r, c) in known.items() if c}
    lines = []
    for p in sorted(set(found) | set(expected)):
        if found.get(p) != expected.get(p):
            lines.append(f"{p}: found {found.get(p)} expected {expected.get(p)}")
    return "\n".join(lines)


def test_every_kind_compare_outside_the_authorities_is_a_known_site():
    found, _ = _scan_python()
    diff = _diff(found, KNOWN_SITES)
    assert not diff, f"\n{diff}\n— {_HINT}"


def test_the_task_kinds_leaf_is_used_module_qualified():
    _, imports = _scan_python()
    assert not imports, (imports, "use `from services.scheduler import task_kinds` and spell task_kinds.NAME")


# ---------------------------------------------------------------------------
# the dashboard
# ---------------------------------------------------------------------------

_ALL_WORDS = sorted({w for m in MACHINES for w in m.distinctive | m.generic}, key=len, reverse=True)
_WORD_RE = "|".join(re.escape(w) for w in _ALL_WORDS)
_TS_COMPARE = re.compile(rf"""[!=]==\s*['"]({_WORD_RE})['"]|['"]({_WORD_RE})['"]\s*[!=]==""")
_TS_CASE = re.compile(rf"""case\s+['"]({_WORD_RE})['"]\s*:""")
_TS_INCLUDES = re.compile(r"""\[([^\]]*)\]\s*\.includes\(""")


def _ts_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for m in _TS_COMPARE.finditer(text):
        counts[m.group(1) or m.group(2)] += 1
    for m in _TS_CASE.finditer(text):
        counts[m.group(1)] += 1
    for m in _TS_INCLUDES.finditer(text):
        for w in re.findall(r"""['"]([^'"]*)['"]""", m.group(1)):
            if w in _ALL_WORDS:
                counts[w] += 1
    return counts


def _ts_files() -> list[Path]:
    src = REPO_ROOT / "dashboard" / "src"
    return [p for p in sorted(list(src.rglob("*.ts")) + list(src.rglob("*.tsx")))
            if "/tests/" not in p.relative_to(REPO_ROOT).as_posix()]


def _scan_ts() -> dict[str, dict[str, int]]:
    sites: dict[str, dict[str, int]] = {}
    for p in _ts_files():
        rel = p.relative_to(REPO_ROOT).as_posix()
        counts = _ts_counts(p.read_text(encoding="utf-8", errors="surrogateescape"))
        if not counts:
            continue
        found: dict[str, int] = {}
        for m in MACHINES:
            found.update(_sites_of(rel, counts, m, m.ts_homes, m.ts_domain))
        if found:
            sites[rel] = dict(sorted(found.items()))
    return sites


def test_the_dashboard_compares_the_mirrors():
    found = _scan_ts()
    diff = _diff(found, KNOWN_TS_SITES)
    assert not diff, (f"\n{diff}\n— compare the lib/kinds/ mirrors' constants (WIRE.*, TASK_KIND.*, RUN_KIND.*, "
                      "TRIGGER_KIND.*, APP_KIND.*, MCP_RUNTIME.*) or ask their predicates (evictedBy, identityOf, "
                      "isArtifactBlock, appKind(app).<fact>, isContainerRuntime); a renderer per kind or another "
                      "vocabulary sharing the word is listed in KNOWN_TS_SITES with its reason")


# A hand-written union of an axis's words is a site the compare scan cannot
# see (the surface scan scores compares, not types): the unions live in the
# mirrors only.
_TS_UNION = re.compile(r"""'(?:docker|python|node)'\s*\|\s*'(?:docker|python|node)'|'folder'\s*\|\s*'file'|"""
                       r"""'file'\s*\|\s*'folder'|'one_time'\s*\|\s*'|'one-time'\s*\|\s*'|'\s*\|\s*'one_time'|"""
                       r"""'\s*\|\s*'triggered'""")


def test_the_dashboard_types_the_kinds_from_the_mirrors():
    unions: dict[str, int] = {}
    for p in _ts_files():
        rel = p.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("dashboard/src/lib/kinds/"):
            continue
        if n := len(_TS_UNION.findall(p.read_text(encoding="utf-8", errors="surrogateescape"))):
            unions[rel] = n
    assert not unions, (unions, "type the field with McpRuntime / AppKindName / TaskKind / RunKind / "
                                "TriggerKind from lib/kinds/")
