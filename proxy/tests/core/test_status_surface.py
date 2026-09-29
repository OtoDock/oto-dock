"""The status surface — the acceptance test of core-seams phase 8, in the
shape of ``tests/core/test_placement_surface.py``.

Eleven status machines, each named once in the store that owns it (the
task run and the delegate result in ``storage/automation/run_status.py``,
the chat phase in ``ws/chat_phase.py`` beside the lane words in
``services/delegation/lane_status.py``, the meeting in
``storage/chat/meeting_status.py``, the rest as constants at the top of
their store or service) and mirrored under ``dashboard/src/lib/status/``.
Generic code asks the sets or compares the constants; a member spelled as a
compare operand anywhere else is listed here WITH its reason, and this test
keeps the list exact in both directions.

A machine's DISTINCTIVE words (``limit_exceeded``, ``streaming``,
``concluding``, ``install_failed``, ``renew_failed``, ``quota_full``,
``inflight``, ``pending approval``, ``not_checked``, ``never_connected`` …)
are scanned tree-wide; its GENERIC words (``pending``, ``running``,
``completed``, ``failed``, ``cancelled``, ``active``, ``idle``, ``ready``,
``expired``, ``disabled``, ``stopped``, ``starting``, ``up``, ``done``,
``ok``, ``error``, ``unknown``, ``paused``, ``rejected``, ``offline``) only
inside the machine's DOMAIN — the files that read the machine — because
elsewhere the same word is another vocabulary (a tool block, a transfer,
a licence, a check verdict). A keyed map (``STATUS_STYLES[status]``) is a
lookup and is never scored; the release gate's vocabulary check does not
score it either.

Python: ``Compare`` operands (tuple / list / set members included),
``startswith`` / ``endswith`` arguments and ``match`` cases over the tracked
``.py`` files under ``proxy/``, ``satellite/`` and ``mcps/custom/`` (tests,
docs, venvs, vendored copies and skills excluded); the three leaves are
used module-qualified. TypeScript: ``=== '…'`` / ``!== '…'``, ``case '…':``
and ``['…'].includes(`` over ``dashboard/src`` (tests excluded).
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


MACHINES: tuple[Machine, ...] = (
    Machine("run",
            frozenset({"limit_exceeded", "user_interrupted"}),
            frozenset({"pending", "running", "completed", "failed", "cancelled"}),
            ("proxy/storage/automation/run_status.py",),
            ("proxy/services/scheduler/", "proxy/storage/automation/db_tasks.py", "proxy/api/tasks/",
             "proxy/ws/", "proxy/core/events/stream_pump.py", "proxy/core/events/task_producer.py",
             "proxy/core/session/session_state.py", "proxy/services/checks/kinds/judge_kind.py",
             "proxy/services/apps/app_handlers.py", "proxy/services/apps/app_steps.py",
             "proxy/api/apps/catalog.py", "proxy/services/delegation/"),
            ("dashboard/src/lib/status/run.ts",),
            ("dashboard/src/api/runs.ts", "dashboard/src/lib/runs.ts", "dashboard/src/components/StatusBadge.tsx",
             "dashboard/src/components/chat/TaskMetadata.tsx", "dashboard/src/components/chat/DelegateTaskInfo.tsx",
             "dashboard/src/components/chat/ChatHistory.tsx", "dashboard/src/pages/History.tsx",
             "dashboard/src/lib/messageBlocks.ts", "dashboard/src/hooks/useChatStream.ts",
             "dashboard/src/components/GroupedRunsTable.tsx")),
    Machine("chat_phase",
            frozenset({"streaming", "warming", "finished", "generating", "awaiting_user"}),
            frozenset({"ready", "idle"}),
            ("proxy/ws/chat_phase.py", "proxy/services/delegation/lane_status.py"),
            ("proxy/ws/", "proxy/core/events/stream_pump.py", "proxy/core/session/interactive_session.py",
             "proxy/api/agents/chats.py", "proxy/services/notifications/", "proxy/api/apps/catalog.py",
             "proxy/core/session/sibling_awareness.py"),
            ("dashboard/src/lib/status/chat.ts",),
            ("dashboard/src/store/chatStore.ts", "dashboard/src/hooks/useActiveChats.ts",
             "dashboard/src/hooks/useDashboardWs.ts", "dashboard/src/components/chat/ActiveChatsPanel.tsx",
             "dashboard/src/components/chat/ChatHistory.tsx", "dashboard/src/pages/agent/AgentChat.tsx",
             "dashboard/src/components/projects/ProjectsOverlay.tsx", "dashboard/src/api/chats.ts")),
    Machine("meeting",
            frozenset({"concluding", "concluded"}),
            frozenset({"pending", "active", "paused", "failed"}),
            ("proxy/storage/chat/meeting_status.py",),
            ("proxy/services/meetings/", "proxy/api/meetings/", "proxy/storage/chat/db_meetings.py"),
            ("dashboard/src/lib/status/meeting.ts",),
            ("dashboard/src/api/meetings.ts", "dashboard/src/pages/admin/MeetingsPage.tsx",
             "dashboard/src/pages/agent/AgentMeetings.tsx", "dashboard/src/components/StatusBadge.tsx")),
    Machine("mcp_request",
            frozenset({"approved", "installing", "install_failed", "installed"}),
            frozenset({"pending", "rejected", "cancelled"}),
            ("proxy/storage/mcp/mcp_request_store.py",),
            ("proxy/services/community/community_installer.py",
             "proxy/services/community/community_agent_installer.py", "proxy/api/mcp/community.py"),
            ("dashboard/src/lib/status/mcpRequest.ts",),
            ("dashboard/src/api/community.ts", "dashboard/src/pages/admin/McpRequestsPage.tsx")),
    Machine("webhook_subscription",
            frozenset({"creating", "renew_failed"}),
            frozenset({"active", "failed", "expired", "disabled"}),
            ("proxy/storage/automation/webhook_subscription_store.py",),
            ("proxy/services/webhooks/", "proxy/api/events/"),
            ("dashboard/src/lib/status/webhookSubscription.ts",),
            ("dashboard/src/api/subscriptions.ts", "dashboard/src/components/accounts/SubscriptionRow.tsx")),
    Machine("engine_subscription",
            frozenset(),
            frozenset({"active", "disabled", "expired"}),
            ("proxy/storage/billing/subscription_status.py",),
            ("proxy/services/engines/", "proxy/services/infra/subscription_health.py",
             "proxy/services/infra/subscription_window_alerts.py", "proxy/api/admin/execution_layers.py",
             "proxy/api/auth/claude_oauth.py", "proxy/api/auth/openai_oauth.py", "proxy/api/agents/discovery.py"),
            ("dashboard/src/lib/status/engineSubscription.ts",),
            ("dashboard/src/api/executionLayers.ts", "dashboard/src/pages/UserSettings.aiEngines.tsx",
             "dashboard/src/pages/admin/ExecutionLayersTab")),
    Machine("app_server",
            frozenset({"backoff", "quota_full", "unapproved", "static", "secrets", "up"}),
            frozenset({"stopped", "starting"}),
            ("proxy/services/apps/app_supervisor.py",),
            ("proxy/services/apps/", "proxy/api/apps/", "proxy/api/hooks/app_deploy.py",
             "proxy/services/checks/kinds/handler_kind.py"),
            ("dashboard/src/lib/status/appServer.ts",),
            ("dashboard/src/api/apps.ts", "dashboard/src/components/apps/AppFrame.tsx",
             "dashboard/src/components/apps/AppLogsPanel.tsx")),
    Machine("app_delivery",
            frozenset({"inflight", "dead"}),
            frozenset({"pending", "done"}),
            ("proxy/storage/db_app_deliveries.py",),
            ("proxy/services/apps/", "proxy/api/apps/", "proxy/services/scheduler/trigger_manager.py"),
            (), ()),
    Machine("deploy_answer",
            frozenset({"refused", "pending approval"}),
            frozenset({"ok", "rejected"}),
            ("proxy/services/apps/app_deploy.py",),
            ("proxy/services/apps/", "proxy/api/hooks/", "proxy/services/community/"),
            (), ()),
    Machine("deploy_state",
            frozenset(),
            frozenset({"idle", "pending"}),
            ("proxy/storage/db_apps.py",),
            ("proxy/services/apps/app_deploy.py", "proxy/api/apps/apps.py"),
            ("dashboard/src/lib/status/appDeploy.ts",),
            ("dashboard/src/api/apps.ts", "dashboard/src/components/apps/AppApprovalCard.tsx",
             "dashboard/src/components/apps/AppsOverlay.tsx", "dashboard/src/pages/apps/AppPage.tsx")),
    Machine("docker",
            frozenset({"unhealthy", "not_found", "not_checked"}),
            frozenset({"running", "starting", "stopped", "error", "unknown"}),
            ("proxy/services/mcp/docker_manager.py",),
            ("proxy/api/mcp/mcps.py", "proxy/services/community/community_installer.py", "proxy/services/mcp/"),
            ("dashboard/src/lib/status/docker.ts",),
            ("dashboard/src/api/mcps.ts", "dashboard/src/pages/admin/McpServersPage.row.tsx")),
    Machine("machine",
            frozenset({"stale", "never_connected", "disconnected", "online"}),
            frozenset({"paused", "offline"}),
            ("proxy/services/remote/remote_status.py", "proxy/storage/remote_store.py"),
            ("proxy/core/remote/", "proxy/api/remote/", "proxy/services/remote/"),
            ("dashboard/src/lib/status/machine.ts",),
            ("dashboard/src/api/remoteMachines.ts", "dashboard/src/components/RemoteBadge.tsx",
             "dashboard/src/pages/admin/RemoteMachinesPage.tsx", "dashboard/src/pages/agent/AgentConfig.tsx",
             "dashboard/src/components/AgentCard.tsx", "dashboard/src/pages/UserSettings.machines.tsx")),
)

# {path: (reason, {"machine:word": count})} — sites outside a machine's
# home that compare one of its words, each ANOTHER vocabulary sharing the
# word, or a string read over the API.
KNOWN_SITES: dict[str, tuple[str, dict[str, int]]] = {
    "mcps/custom/computer-mcp/executor.py": (
        "the computer MCP's key state (up | down), not a server", {"app_server:up": 1}),
    "mcps/custom/display-mcp/app_tools.py": (
        "the display MCP reads the deploy answer as strings over the API (a Python constant never reaches it)",
        {"deploy_answer:pending approval": 2, "deploy_answer:refused": 3}),
    "proxy/api/apps/apps.py": (
        "a substring test on the reorder route's error message, not a machine state", {"machine:stale": 1}),
    "proxy/api/hooks/app_deploy.py": (
        "the check hook's own words: the render report's verdict (ok | soft | hard | unavailable) and a "
        "manifest-dict key test", {"deploy_answer:ok": 3}),
    "proxy/api/notifications/notifications.py": (
        "a notification's source (static), not a server state", {"app_server:static": 1}),
    "proxy/core/events/stream_pump.py": (
        "the compaction phase (context_compact: started | completed | failed | usage), not a run",
        {"run:completed": 1}),
    "proxy/core/remote/satellite_connection.py": (
        "a persist LANE name (status | caps | paused), not the machine's live state", {"machine:paused": 1}),
    "proxy/services/mcp/docker_manager.py": (
        "Docker's own container State word (exited | dead | created), not a delivery", {"app_delivery:dead": 1}),
    "proxy/services/mcp/mcp_sync.py": (
        "the satellite MCP sync ack's status, not a container", {"docker:not_found": 1}),
}

# {path: (reason, {"machine:word": count})} — dashboard files that keep a
# quoted word, each another vocabulary sharing it.
KNOWN_TS_SITES: dict[str, tuple[str, dict[str, int]]] = {
    "dashboard/src/components/CommunityAgentsBrowser.tsx": (
        "the catalog browser's filter: an entry installed on the platform, not a request",
        {"mcp_request:installed": 1}),
    "dashboard/src/components/CommunityMcpsBrowser.tsx": (
        "the catalog browser's installed flag, not a request", {"mcp_request:installed": 1}),
    "dashboard/src/components/CommunitySkillsBrowser.tsx": (
        "the catalog browser's installed flag, not a request", {"mcp_request:installed": 1}),
    "dashboard/src/components/apps/AppApprovalCard.tsx": (
        "a manifest block key (secrets), not a server state", {"app_server:secrets": 1}),
    "dashboard/src/components/chat/InstallProgressBar.tsx": (
        "the dashboard's install job (installing | verifying | done | failed), not a request",
        {"mcp_request:installing": 3}),
    "dashboard/src/components/ui/IconDropdown.tsx": (
        "a placement direction (up | down), not a server state", {"app_server:up": 1}),
    "dashboard/src/hooks/useChatStream.ts": (
        "the compaction phase (completed | failed), the plan status (pending) and the tool block's status "
        "(running) — three dashboard-side vocabularies sharing the run's words",
        {"run:completed": 1, "run:failed": 1, "run:pending": 2, "run:running": 2}),
    "dashboard/src/store/installStore.ts": (
        "the dashboard's install job, not a request", {"mcp_request:installing": 2}),
}

_LEAVES = ("storage.automation.run_status", "ws.chat_phase", "storage.chat.meeting_status")


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
            if isinstance(n, ast.ImportFrom) and n.module in _LEAVES for a in n.names]


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


_HINT = ("compare the machine's constants or ask its sets (run_status.is_terminal, chat_phase.WIRE_PHASES, "
         "meeting_status.ENDABLE, mcp_request_store.APPROVABLE, deliveries.ACTIVE, app_supervisor.SERVING, "
         "docker_manager.PRESENT, remote_status.REACHABLE …); a site that is another vocabulary sharing the "
         "word is listed in KNOWN_SITES with its reason")


def _diff(found: dict[str, dict[str, int]], known: dict[str, tuple[str, dict[str, int]]]) -> str:
    expected = {p: c for p, (_r, c) in known.items()}
    lines = []
    for p in sorted(set(found) | set(expected)):
        if found.get(p) != expected.get(p):
            lines.append(f"{p}: found {found.get(p)} expected {expected.get(p)}")
    return "\n".join(lines)


def test_every_status_compare_outside_the_authorities_is_a_known_site():
    found, _ = _scan_python()
    diff = _diff(found, KNOWN_SITES)
    assert not diff, f"\n{diff}\n— {_HINT}"


def test_the_leaves_are_used_module_qualified():
    _, imports = _scan_python()
    assert not imports, (imports, "use `from storage.automation import run_status` / `from ws import chat_phase` "
                                  "/ `from storage.chat import meeting_status` and spell module.NAME")


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


def _scan_ts() -> dict[str, dict[str, int]]:
    src = REPO_ROOT / "dashboard" / "src"
    sites: dict[str, dict[str, int]] = {}
    for p in sorted(list(src.rglob("*.ts")) + list(src.rglob("*.tsx"))):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel:
            continue
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
    assert not diff, (f"\n{diff}\n— compare the lib/status/ mirrors' constants (RUN_STATUS.*, CHAT_PHASE.*, "
                      "MEETING_STATUS.*, MCP_REQUEST_STATUS.*, ENGINE_SUBSCRIPTION_STATUS.*, APP_SERVER_STATE.*, "
                      "DEPLOY_STATE.*, DOCKER_STATUS.*, MACHINE_STATE.*) or ask their predicates; another "
                      "vocabulary sharing the word is listed in KNOWN_TS_SITES with its reason")
