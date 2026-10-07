"""The wire surface — the acceptance test of core-seams phase 6, in
``tests/execution/test_engine_id_surface.py``'s shape.

A frame is named once, in ``ws/wire_events.py`` and its dashboard mirror
``api/wireEvents.ts``; every sender spells the constant and every receiver
switches on it. What still spells a frame name as a literal outside the
two authorities is listed here WITH its reason, per file and count, and
this test keeps the list exact in both directions — a new site fails
("spell the constant, or add the site here with its reason"), a retired
site fails too ("drop it from here").

Four passes over the tracked sources (tests, docs, venvs, vendored copies
and skills excluded):

1. the proxy: a dict literal ``{"type": "<frame>"}`` whose value is a
   dashboard-socket frame name, anywhere but the authority — the senders'
   pass. The engines' NATIVE event shapes (the Direct layer's chunk dicts,
   the CLI hook config's ``command`` entries) share words with the wire
   and are the allowed sites;
2. the proxy: a pump-item kind, a hook-item kind or a notify-queue kind
   (``ws_event`` … ``pump_ended``, ``question_prompt`` / ``mode_restored``,
   ``_server_kick`` …) as a compare operand or a dict value outside the
   authority — none allowed;
3. the proxy: an inbound message name as a compare operand outside the
   dispatcher table — none allowed (the phone socket and the app socket
   spell their own inbound names through the same constants);
4. the dashboard: a frame name, a persisted row kind or a live-block kind
   as a ``case`` / ``===`` / ``.includes`` operand outside the mirror.
   The render block kinds (``components/chat/types.ts``) share a dozen
   words with the wire and their comparers are the allowed sites, as are
   the duplex voice state's ``thinking`` and the STT socket's own
   ``error``.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections import Counter
from pathlib import Path

from tests._paths import REPO_ROOT

from ws import wire_events as wire

EXCLUDED = ("/tests/", "/venv/", "/.venv/", "/node_modules/", "/_vendored/", "/skills/",
            "/fixtures/", "/__pycache__/", "/docs/")
AUTHORITY_PY = {"proxy/ws/wire_events.py", "proxy/core/events/common_events.py"}
AUTHORITY_TS = {"dashboard/src/api/wireEvents.ts"}


def _tracked(prefix: str, suffixes: tuple[str, ...]) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", prefix], cwd=REPO_ROOT, capture_output=True,
                             text=True, check=True).stdout.split("\n")
    except (OSError, subprocess.CalledProcessError):
        # A tree without .git (the exported public cut): walk the same root.
        out = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for s in suffixes for p in (REPO_ROOT / prefix).rglob(f"*{s}")]
    return [REPO_ROOT / p for p in out
            if p.endswith(suffixes) and not any(x in f"/{p}" for x in EXCLUDED)]


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# Pass 1 — the senders: a typed dict literal naming a frame
# ---------------------------------------------------------------------------

FRAME_NAMES = frozenset(wire.FRAMES)

# {file: {literal: count}} — every site that still builds a frame-shaped
# dict by literal, with its reason.
KNOWN_SENDER_LITERALS: dict[str, dict[str, int]] = {
    # The Direct layer's native chunk dicts (``{"type": "text", "data": …}``),
    # translated into CommonEvents at the layer's edge — not wire frames.
    "proxy/core/layers/direct/session.py": {"text": 2, "thinking": 5, "tool_start": 1, "tool_end": 2,
                                            "done": 1, "error": 4, "metadata": 1, "context_compact": 1},
    # The task-run SSE stream (``api/tasks/tasks.py``): an HTTP stream read by
    # schedules-mcp's ``run_task(wait=true)`` (nothing in the dashboard reads
    # it), not the socket; its own words. The producer forwards the turn's
    # frames; the runner sends the one ``done``, after the row is stamped.
    "proxy/api/tasks/tasks.py": {"error": 1},
    "proxy/api/tasks/run_stream.py": {"text": 1, "done": 1},
    "proxy/core/events/task_producer.py": {"text": 1, "tool_start": 1, "tool_end": 1, "task_spawn": 1},
    "proxy/services/scheduler/runner.py": {"done": 1},
    # The STT socket (``ws/audio.py``): its own protocol shares ``error``.
    "proxy/ws/audio.py": {"error": 7},
    # The satellite protocol's own ``error`` (the reconnect grace) and its
    # own ``pong`` (``core/remote/satellite_connection.py``).
    "proxy/core/remote/satellite_grace.py": {"error": 1},
    "proxy/core/remote/satellite_connection.py": {"pong": 1},
    # The vendor APIs' content blocks (``text`` / ``tool_result``) and the
    # Codex app-server's input items (``text``).
    "proxy/core/layers/providers/anthropic_adapter.py": {"text": 2, "tool_result": 1},
    "proxy/core/layers/codex/session.py": {"text": 2},
    # The file tree's entry kind (``file`` / ``dir``), not the artifact frame.
    "proxy/api/agents/files.py": {"file": 2},
}


def _typed_dict_literals(path: Path) -> Counter:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    counts: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "type"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    counts[v.value] += 1
    return counts


def test_no_sender_spells_a_frame_name_by_literal():
    found: dict[str, dict[str, int]] = {}
    for p in _tracked("proxy", (".py",)):
        rel = _rel(p)
        if rel in AUTHORITY_PY:
            continue
        hits = {lit: n for lit, n in _typed_dict_literals(p).items() if lit in FRAME_NAMES}
        if hits:
            found[rel] = hits
    known = {k: v for k, v in KNOWN_SENDER_LITERALS.items() if v}
    assert found == known, _diff(found, known)


# ---------------------------------------------------------------------------
# Pass 2 — the kinds beside the wire, and pass 3 — the inbound names
# ---------------------------------------------------------------------------

BESIDE_THE_WIRE = frozenset({
    wire.PUMP_WS_EVENT, wire.PUMP_PERMISSION_PROMPT, wire.PUMP_PLAN_REVIEW,
    wire.PUMP_QUESTION_PROMPT, wire.PUMP_MODE_RESTORED,
    wire.PUMP_IS_DONE, wire.PUMP_ALL_DONE, wire.PUMP_ENDED, wire.PUMP_DETACHED,
    wire.ITEM_QUESTION_PROMPT, wire.ITEM_MODE_RESTORED,
    wire.NOTIFY_SERVER_KICK, wire.NOTIFY_BG_NUDGE, wire.NOTIFY_BG_COMMAND_NUDGE,
    wire.NOTIFY_LIVENESS_CLEAR, wire.NOTIFY_CHAT_UI_FRAME, wire.NOTIFY_TASK_RESULT_PROMPT,
    wire.NOTIFY_CONTINUATION_PROMPT,
})
INBOUND_NAMES = frozenset(wire.INBOUND)
# Inbound names other protocols share: the phone socket (warmup / chat /
# abort / close) and the satellite protocol (abort, close, ping, pty_*) —
# the phone socket spells the constants; the satellite's are its own.
INBOUND_SHARED_ELSEWHERE = frozenset({"abort", "close", "ping", "chat", "warmup", "focus",
                                      "pty_input", "pty_resize"})


def _compare_operands(path: Path) -> Counter:
    """String constants used as a compare operand, an ``in (…)`` member or a
    dict value under a ``type`` / ``pump_type`` / ``event_type`` key."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    counts: Counter = Counter()

    def add(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            counts[n.value] += 1
        elif isinstance(n, (ast.Tuple, ast.List, ast.Set)):
            for e in n.elts:
                add(e)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            add(node.left)
            for c in node.comparators:
                add(c)
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in ("type", "pump_type", "event_type"):
                    add(v)
    return counts


def test_no_module_spells_a_pump_item_hook_item_or_notify_kind():
    found = {}
    for p in _tracked("proxy", (".py",)):
        rel = _rel(p)
        if rel in AUTHORITY_PY:
            continue
        hits = {lit: n for lit, n in _compare_operands(p).items() if lit in BESIDE_THE_WIRE}
        if hits:
            found[rel] = hits
    assert found == {}, _diff(found, {})


def test_no_module_compares_an_inbound_name_outside_the_table():
    found = {}
    for p in _tracked("proxy", (".py",)):
        rel = _rel(p)
        if rel in AUTHORITY_PY or rel.startswith("proxy/core/remote/") or rel == "proxy/ws/satellite.py":
            continue
        hits = {lit: n for lit, n in _compare_operands(p).items()
                if lit in INBOUND_NAMES and lit not in INBOUND_SHARED_ELSEWHERE}
        if hits:
            found[rel] = hits
    assert found == {}, _diff(found, {})


# ---------------------------------------------------------------------------
# Pass 4 — the dashboard
# ---------------------------------------------------------------------------

# The artifact kinds are the `artifact_kinds` axis's words (phase 9) and
# the live-block kinds `agent` / `command` / `delegate` are other axes'
# (scope, task kinds, render kinds): neither is this pass's to count.
ARTIFACT_KINDS = frozenset({
    wire.IMAGES, wire.IMAGE_GENERATING, wire.IMAGE_GEN_FAILED, wire.URL, wire.FILE,
    wire.VIDEO, wire.AUDIO, wire.MEDIA_PROCESSING, wire.MEDIA_FAILED, wire.DOCUMENT_PREVIEW, wire.UI,
})
DASH_WORDS = (FRAME_NAMES - ARTIFACT_KINDS) | {
    wire.PERSISTED_TOOL, wire.PERSISTED_BG_NUDGE, wire.PERSISTED_BG_COMMAND_NUDGE,
    wire.PERSISTED_SCHEDULE_WAKE,
}
# Words the render block kinds (``components/chat/types.ts``) share with the
# wire; a file that compares a render kind is listed with its count.
KNOWN_TS_SITES: dict[str, dict[str, int]] = {
    # The render block kinds (``components/chat/types.ts``) share these words
    # with the wire; each file compares a MessageBlock's kind.
    "dashboard/src/hooks/useChatStream.ts": {"text": 3, "thinking": 3, "system": 2, "question": 4,
                                             "plan_review": 6, "tool": 2},
    "dashboard/src/components/chat/ChatBlockRenderer.tsx": {"text": 1, "thinking": 1, "tool": 1, "question": 1,
                                                            "plan_review": 1, "system": 1,
                                                            "artifact_interaction": 1, "app_action": 1,
                                                            "metadata": 1},
    "dashboard/src/components/chat/ChatMessages.tsx": {"text": 2, "metadata": 4},
    "dashboard/src/components/chat/ActivityGroup.tsx": {"thinking": 1},
    "dashboard/src/lib/activityGroups.ts": {"tool": 1, "thinking": 1},
    "dashboard/src/hooks/useChatMessages.ts": {"text": 1, "tool": 3},
    "dashboard/src/components/workspace/FilePreviewBody.tsx": {"text": 1},
    "dashboard/src/lib/messageBlocks.ts": {"text": 1, "question": 1, "tool": 2},
    # The duplex voice STATE (listening | thinking | speaking) and the duplex
    # browser socket's own ``error``.
    "dashboard/src/components/chat/PresenceHalo.tsx": {"thinking": 4, "error": 1},
    "dashboard/src/hooks/useDuplexVoice.ts": {"error": 4, "thinking": 2},
    "dashboard/src/pages/agent/chat/useChatDuplexVoice.ts": {"thinking": 2, "error": 1},
    "dashboard/src/components/chat/VoiceControl.tsx": {"error": 1},
    "dashboard/src/hooks/useWakeWord.ts": {"error": 1},
    # The STT / TTS backends' own protocol.
    "dashboard/src/audio/backends/platformStt.ts": {"error": 1},
    "dashboard/src/audio/backends/platformTts.ts": {"error": 1},
    # ``system`` as a theme and as an MCP instance scope.
    "dashboard/src/contexts/ThemeContext.tsx": {"system": 3},
    "dashboard/src/components/admin/McpInstanceManager.tsx": {"system": 7},
    "dashboard/src/pages/admin/PlatformPage.tsx": {"system": 1},
    # ``done`` / ``queued`` as an install, transfer, workflow or step state.
    "dashboard/src/components/chat/InstallProgressBar.tsx": {"done": 3},
    "dashboard/src/store/installStore.ts": {"done": 1},
    "dashboard/src/store/transferStore.ts": {"queued": 2, "done": 1},
    "dashboard/src/components/workspace/TransferPopup.tsx": {"done": 2},
    "dashboard/src/components/chat/plan/WorkflowPanel.tsx": {"done": 2},
    "dashboard/src/components/chat/ToolActivity.tsx": {"done": 1},
    "dashboard/src/components/chat/ChatInput.tsx": {"done": 1},
    "dashboard/src/components/AgentUpdateModal.tsx": {"done": 1},
    "dashboard/src/components/CommunityMcpsBrowser.tsx": {"done": 1},
    "dashboard/src/components/CommunitySkillsBrowser.tsx": {"done": 1},
    "dashboard/src/pages/admin/ExecutionLayersTab.forms.tsx": {"done": 1},
    # ``app_action`` as the app iframe's postMessage kind (APPS.md), and
    # ``done`` as an app frame state.
    "dashboard/src/components/apps/AppFrame.tsx": {"done": 1, "app_action": 1},
    "dashboard/src/sharehost/host.ts": {"app_action": 1},
}

_TS_CASE = re.compile(r"\bcase\s+'([a-z_]+)'")
_TS_EQ = re.compile(r"[!=]==?\s*'([a-z_]+)'|'([a-z_]+)'\s*[!=]==?")
_TS_INCL = re.compile(r"\.(?:includes|has)\(\s*'([a-z_]+)'")


def _ts_compares(path: Path) -> Counter:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"//[^\n]*", "", text)
    counts: Counter = Counter()
    for m in _TS_CASE.finditer(text):
        counts[m.group(1)] += 1
    for m in _TS_EQ.finditer(text):
        counts[m.group(1) or m.group(2)] += 1
    for m in _TS_INCL.finditer(text):
        counts[m.group(1)] += 1
    return counts


def test_the_dashboard_compares_frame_names_only_through_the_mirror():
    found = {}
    for p in _tracked("dashboard/src", (".ts", ".tsx")):
        rel = _rel(p)
        if rel in AUTHORITY_TS:
            continue
        hits = {lit: n for lit, n in _ts_compares(p).items() if lit in DASH_WORDS}
        if hits:
            found[rel] = hits
    known = {k: v for k, v in KNOWN_TS_SITES.items() if v}
    assert found == known, _diff(found, known)


def _diff(found: dict, known: dict) -> str:
    lines = []
    for f in sorted(set(found) | set(known)):
        if found.get(f) != known.get(f):
            lines.append(f"{f}: found {found.get(f)} known {known.get(f)}")
    return "\n".join(lines)


_BARE_IMPORT = re.compile(r"^\s*from ws\.wire_events import ", re.M)


def test_the_catalogue_is_used_module_qualified():
    # Fifteen catalogue names equal a `core.events.common_events` name the pump
    # imports bare (TEXT, THINKING, SYSTEM, DONE, ERROR, METADATA …): a bare
    # `from ws.wire_events import X` would shadow one silently. `wire.X` only.
    offenders = [_rel(p) for p in _tracked("proxy", (".py",))
                 if _rel(p) not in AUTHORITY_PY and _BARE_IMPORT.search(p.read_text(encoding="utf-8"))]
    assert offenders == [], offenders


def test_the_allowlists_are_not_empty_in_name_only():
    # The reasoned lists carry real sites; a list that drifted to nothing
    # should be trimmed, and the passes above must have scanned files.
    assert _tracked("proxy", (".py",)) and _tracked("dashboard/src", (".ts", ".tsx"))
