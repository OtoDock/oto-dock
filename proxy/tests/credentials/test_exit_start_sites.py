"""Every place that starts a tool from a session's build gives a placeholder
the exit that fills it.

The one build (``build_session_mcp_config``,
its ``_exit_placeholders``) swaps a declared key for a placeholder wherever
the one machine rule admits the place. The exit itself is attached later, by
whoever starts the session's tools: ``credential_exit.attach_sandbox`` (the
platform's sandbox or a compute machine), the build's own
``credential_exit.machine_env`` (a paired computer), or the descriptor's
placeholders with the record made again (a session taken back after a
restart). A starter that forgets it leaves a tool holding a placeholder
nothing fills, which it then sends to its vendor.

One pass over every tracked ``.py`` under ``proxy/`` (tests, docs and the
venv excluded) finds the three kinds of site a new starter has to touch:
the one build, the one door that hands a program its bundle
(``mcp_broker.provision``, matched by its module), and the tool manager
Direct LLM and the app buttons share (``AgentMCPManager``). Every reference
counts, called or handed on (to a thread or an executor in any argument,
to ``functools.partial``), under any name an import gave it. An annotation
names the manager without starting anything and does not count.
``SITES`` lists them per file with how the exit is attached for the tools
they start. The counts are exact in both directions: a new site fails until
it is listed with its reason, and a listed site that left fails too. The
headless starter's and the re-adoption's behaviour are held by
``tests/apps/test_headless_exec.py`` and ``tests/remote/test_exit_push.py``.
"""

from __future__ import annotations

import ast
import subprocess
from collections import Counter
from pathlib import Path

from tests._paths import PROXY_DIR, REPO_ROOT

BUILD = "build_session_mcp_config"
PROVISION = "provision"
MANAGER = "AgentMCPManager"
_BROKER = "core.credentials.mcp_broker"

_LAYERS = ("its session starts in a layer that attaches the exit: the CLI and Codex layers' _prepare "
           "(attach_sandbox, the placement the build used), Direct LLM's start_session, or the remote "
           "start, whose build gave a paired computer's bundles machine_env and whose payload carries "
           "machine_block")

# {(path, kind): (count, how the exit is attached for the tools it starts)}
SITES: dict[tuple[str, str], tuple[int, str]] = {
    # The one build's callers.
    ("core/config/config_builder.py", BUILD): (1, _LAYERS),
    ("core/config/task_config_builder.py", BUILD): (1, _LAYERS),
    ("core/config/phone_config_builder.py", BUILD): (2, _LAYERS),
    ("services/meetings/meeting_context.py", BUILD): (1, _LAYERS),
    ("services/scheduler/wake_session.py", BUILD): (1, _LAYERS),
    ("services/apps/headless_exec.py", BUILD): (
        1, "open_manager attaches the compute machine's exit (attach_sandbox with the placement the build "
           "used) before its manager starts"),
    # The one door that hands a program its bundle.
    ("core/layers/cli/layer.py", PROVISION): (1, "_prepare attaches the exit first (attach_sandbox)"),
    ("core/layers/codex/spawn.py", PROVISION): (1, "_prepare attaches the exit first (attach_sandbox)"),
    ("core/layers/direct/mcp.py", PROVISION): (
        1, "the manager's start: its starter attached the exit before (Direct LLM's start_session, the "
           "headless open_manager); the door rides each tool_open and the values are pushed first"),
    ("core/remote/remote_session_start.py", PROVISION): (
        1, "a paired computer's bundles carry machine_env from the build, the payload carries "
           "machine_block; a compute session's exit is attached in its layer"),
    ("core/remote/mcp_gateway_push.py", PROVISION): (
        1, "reprovision_adopted: the descriptor's placeholders, a paired computer's record and its "
           "variables made again first (_readopt_machine_exit), a compute sandbox kept its own"),
    # The tool manager Direct LLM and the app buttons share.
    ("core/layers/direct/mcp.py", MANAGER): (1, "MCPPool, for a Direct LLM session its layer attached"),
    ("services/apps/headless_exec.py", MANAGER): (1, "open_manager, which attaches before it starts"),
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


def _bound(tree: ast.Module) -> tuple[set[str], set[str], set[str], set[str]]:
    """The names a module binds to the build, to the broker's module, to its
    ``provision`` and to the manager (an alias included)."""
    build, broker, provision, manager = {BUILD}, set(), set(), {MANAGER}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                as_name = alias.asname or alias.name
                if f"{node.module}.{alias.name}" == _BROKER:
                    broker.add(as_name)
                elif node.module == _BROKER and alias.name == PROVISION:
                    provision.add(as_name)
                elif alias.name == BUILD:
                    build.add(as_name)
                elif alias.name == MANAGER:
                    manager.add(as_name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _BROKER and alias.asname:
                    broker.add(alias.asname)
    return build, broker, provision, manager


def _annotations(tree: ast.Module) -> set[int]:
    """The nodes inside an annotation (a parameter's, a return's, a variable's):
    a type names the manager and starts nothing."""
    parts: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            parts.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            parts.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            parts.append(node.annotation)
    return {id(n) for part in parts for n in ast.walk(part)}


def _references(tree: ast.Module) -> Counter:
    """Each reference in one module to the build, the broker's ``provision``
    and the manager, called or handed on, by any of their names."""
    build, broker, provision, manager = _bound(tree)
    skip = _annotations(tree)
    counts: Counter = Counter()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        kind = ""
        if isinstance(node, ast.Name):
            kind = BUILD if node.id in build else MANAGER if node.id in manager else (
                PROVISION if node.id in provision else "")
        elif isinstance(node, ast.Attribute):
            if node.attr in (BUILD, MANAGER):
                kind = node.attr
            elif node.attr == PROVISION and (
                    (isinstance(node.value, ast.Name) and node.value.id in broker)
                    or ast.unparse(node.value) == _BROKER):
                kind = PROVISION
        if kind:
            counts[kind] += 1
    return counts


def _sites() -> Counter:
    counts: Counter = Counter()
    for path in _tracked_py():
        rel = str(path.relative_to(PROXY_DIR))
        for kind, n in _references(ast.parse(path.read_text(encoding="utf-8"))).items():
            counts[(rel, kind)] += n
    return counts


def test_every_starter_of_a_sessions_tools_is_listed_with_its_exit():
    found = _sites()
    listed = {key: count for key, (count, _why) in SITES.items()}
    new = {key: n for key, n in found.items() if key not in listed}
    gone = {key: n for key, n in listed.items() if key not in found}
    moved = {key: (listed[key], found[key]) for key in listed.keys() & found.keys() if listed[key] != found[key]}
    assert not new, f"a new place builds, provisions or starts a session's tools: list it with how its exit is attached: {new}"
    assert not gone, f"a listed site left the code: remove it from SITES: {gone}"
    assert not moved, f"a listed site's count changed (listed, found): {moved}"


def test_the_pass_sees_each_spelling():
    """The matcher itself: a call, a function handed to a wrapper in its first
    or its second argument and to ``functools.partial``, the build and the
    manager under another name, the broker's module by its full name and by
    two aliases, its ``provision`` imported under another name, a
    ``provision`` of another module that is not the broker's, and an
    annotation, which counts for nothing."""
    source = (
        "import asyncio\n"
        "import functools\n"
        "import core.credentials.mcp_broker\n"
        "import core.credentials.mcp_broker as cb\n"
        "from core.credentials.mcp_broker import provision as give\n"
        "from core.credentials import mcp_broker as mb\n"
        "from core.layers.direct.mcp import AgentMCPManager as M\n"
        "from services.mcp import mcp_registry, other\n"
        "from services.mcp.mcp_registry import build_session_mcp_config as build\n"
        "async def f(loop, held: M | None = None) -> M:\n"
        "    mcp_registry.build_session_mcp_config('a', None)\n"
        "    await asyncio.to_thread(mcp_registry.build_session_mcp_config, 'a', None)\n"
        "    await loop.run_in_executor(None, mcp_registry.build_session_mcp_config, 'a', None)\n"
        "    later = functools.partial(build, 'a')\n"
        "    mb.provision('s', {})\n"
        "    cb.provision('s', {})\n"
        "    core.credentials.mcp_broker.provision('s', {})\n"
        "    give('s', {})\n"
        "    other.provision('s', {})\n"
        "    kept: dict[str, M] = {}\n"
        "    return M('a')\n"
    )
    assert _references(ast.parse(source)) == Counter({BUILD: 4, PROVISION: 4, MANAGER: 1})
