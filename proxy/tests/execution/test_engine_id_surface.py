"""The engine-id surface — the acceptance test of the engine lane (phase 6),
the executable twin of ``docs/execution-layers/ADDING-AN-ENGINE.md``
section 6.

A fourth engine is a descriptor, a layer, a satellite row and its classes:
shared code reads the descriptor and calls the layer, never compares an
engine id. What still names an engine outside the engine's own package is
listed here WITH its reason, and this test keeps the list exact in both
directions — a new site fails ("read the descriptor, or add the site to
ADDING-AN-ENGINE.md and here with its reason"), a retired site fails too
("drop it from both").

Two passes over every tracked ``.py`` under ``proxy/``, ``satellite/`` and
``mcps/custom/`` (tests, docs, venvs, vendored copies and skills excluded;
the registered engines' own packages and the satellite's engine files
exempt — an engine names itself):

1. the three ids as substrings of any string constant that is not a
   docstring (f-string parts, implicit concatenation, dict keys and tuples
   all surface as constants; what hides is a runtime-built id such as
   ``"codex" + "-cli"`` — stated, not defended), counted per file and
   literal; ``config.py``'s must all sit inside ``MODEL_REGISTRY``;
2. the OTHER spellings of an identity as a BRANCH operand — a short name
   (``claude`` / ``codex`` / ``direct``) or a config dir (``.claude`` /
   ``.codex``) on either side of a compare (``==``, ``!=``, ``in``,
   ``not in``) or as a ``startswith`` / ``endswith`` argument — the pass
   that finds a ``transcript_kind == "claude"`` or a ``".codex" in parts``
   an id scan cannot see. Data tuples of config dirs (the sandbox's mount
   lists) are not branches and are not counted; the doc's table names them.

3. (core-seams phase 2) the platform's canonical TOOL NAMES
   (``core/events/tool_roles.TOOL_ROLES``) as a string constant EQUAL to a
   name, outside the authority — ``tool_roles.py``, the engine packages,
   the two tailers and the hook script's twin — with an allowlist of the
   sites that must name one and why. Generic code asks a role, never a
   name.

And three structural facts: every registered engine's package has a
``layer.py`` and a ``remote.py`` exactly when it runs remotely; every
satellite row's two class modules exist; no non-test dashboard source
quotes an engine id, branches on a short name, or branches on a tool name
outside ``dashboard/src/lib/tools/``.
"""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
from pathlib import Path

from tests._paths import PROXY_DIR, REPO_ROOT

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.events import tool_roles  # noqa: E402
from core.session.session_manager import get_all_layers  # noqa: E402
from satellite.engines import ENGINES  # noqa: E402

ENGINE_IDS = ("claude-code-cli", "codex-cli", "direct-llm")
SHORT_NAMES = ("claude", "codex", "direct")
CONFIG_DIRS = (".claude", ".codex")
_SCAN_ROOTS = ("proxy", "satellite", "mcps/custom")
_EXCLUDED_PARTS = {"tests", "docs", "venv", ".venv", "_vendored", "skills", "node_modules", "__pycache__"}

# --- pass 1: the ids ---------------------------------------------------------
# {path: {literal: count}} — every string constant naming an engine id outside
# the engine packages, with the reason it stays. ``config.py`` is the one
# file judged by WHERE its literals sit (all inside MODEL_REGISTRY), not by
# count: a new model row must not fail this test.
KNOWN_ENGINE_ID_SITES: dict[str, tuple[str, dict[str, int]]] = {
    "proxy/core/session/session_manager.py": (
        "the registry itself — one row per engine, by design",
        {"claude-code-cli": 1, "codex-cli": 1, "direct-llm": 1}),
    "proxy/core/execution_layer.py": (
        "DEFAULT_EXECUTION_PATH, the one constant every no-opinion caller reads",
        {"claude-code-cli": 1}),
    "proxy/storage/agents/schema.py": (
        "the agents column's DDL default — DDL cannot read a constant; a test pins it equal",
        {"claude-code-cli": 1}),
    "proxy/core/remote/satellite_connection.py": (
        "_LEGACY_SATELLITE_ENGINES — what every satellite before 0.5.124 ran, a wire fact",
        {"claude-code-cli": 1, "codex-cli": 1}),
    "proxy/api/auth/claude_oauth.py": (
        "the vendor route's `layer` default is its own engine; a same-vendor engine passes `layer`",
        {"claude-code-cli": 2}),
    "proxy/api/auth/openai_oauth.py": (
        "the vendor route's `layer` default; the device-code login is performed by the Codex CLI itself",
        {"codex-cli": 2}),
    "proxy/core/concurrency.py": (
        "the capacity evictor's source → engine map beside its private-pool scan (roadmap: an accessor)",
        {"claude-code-cli": 1, "codex-cli": 1, "direct-llm": 1}),
    "proxy/core/config/config_builder.py": (
        "Direct LLM's provider-switch scope rides its private _USER_SUB env entry (not a no-op move)",
        {"direct-llm": 2}),
    "proxy/core/config/phone_config_builder.py": (
        "the same _USER_SUB stamp on the phone path",
        {"direct-llm": 1}),
    "proxy/services/phone/phone_config.py": (
        "the phone turn classifier calls a provider API through the API engine's pool keys (and its relay path)",
        {"direct-llm": 3}),
    "proxy/services/title_generator.py": (
        "the title turn calls a provider API through the API engine's pool keys, its provider entries and its relay path",
        {"direct-llm": 4}),
    "proxy/core/layers/providers/anthropic_adapter.py": (
        "the shared provider package offers the vendor's server tools on the API engine's rows",
        {"direct-llm": 1}),
    "satellite/engines.py": (
        "the satellite's ENGINES table — one row per engine, by design",
        {"claude-code-cli": 2, "codex-cli": 2}),
    "satellite/terminal/otodock_cli.py": (
        "the local `otodock` client's subcommand → wire-id map; it runs standalone and cannot import the table",
        {"claude-code-cli": 1, "codex-cli": 1}),
    "mcps/custom/delegation-mcp/server.py": (
        "a tool description names the engines as examples (roadmap: read the catalog like agent-config-mcp)",
        {"claude-code-cli": 1, "codex-cli": 1}),
    "mcps/custom/schedules-mcp/server.py": (
        "a tool description names the engines as examples (roadmap: read the catalog like agent-config-mcp)",
        {"claude-code-cli": 1, "codex-cli": 1, "direct-llm": 1}),
}
_REGISTRY_ONLY = {"proxy/config.py"}   # MODEL_REGISTRY rows tag the engines a model runs on

# --- pass 2: the other spellings as branch operands --------------------------
KNOWN_SPELLING_BRANCHES: dict[str, tuple[str, dict[str, int]]] = {
    "proxy/api/hooks/paths.py": (
        "the /.claude/ and /.codex/ virtual-path prefixes the hooks resolve for a session",
        {".claude": 4, ".codex": 2}),
    "proxy/services/path_roles.py": (
        "the protected-file rules per config dir and the Claude task-output path shape",
        {".claude": 1, ".codex": 1, "claude": 2}),
    "proxy/core/sandbox/sandbox.py": (
        "a mount destination may never sit under a CLI dir",
        {".claude": 1, ".codex": 1}),
    "proxy/core/remote/file_sync.py": (
        "the host-local runtime-state helpers per config dir (one of the five file-sync blocks)",
        {".claude": 1, ".codex": 1}),
    "satellite/transport/file_sync.py": (
        "the satellite's twin of the file-sync helpers",
        {".claude": 1, ".codex": 1}),
    "proxy/config.py": (
        "a provider heuristic on MODEL ids (`model.startswith(\"claude\")`), not an engine",
        {"claude": 1}),
    "proxy/api/sessions/sessions.py": (
        "the legacy /v1/sessions `llm_mode` field's value, hard-wired to the Anthropic API — not an engine",
        {"direct": 1}),
}


# --- pass 3: the canonical tool names as string constants ------------------
# {path: (reason, {literal: count})} — a string constant EQUAL to a canonical
# tool name outside the authority. The authority: core/events/tool_roles.py,
# the engine packages, the two tailers, the hook script's twin.
TOOL_NAME_AUTHORITY = (
    "proxy/core/events/tool_roles.py",
    "proxy/core/session/transcript_tailer.py",
    "proxy/core/session/codex_rollout_tailer.py",
    "proxy/hooks/tool_result_forwarder.py",
)
KNOWN_TOOL_NAME_SITES: dict[str, tuple[str, dict[str, int]]] = {
    "proxy/auth/path_policy.py": (
        "the deny message's user-facing verbs (\"Write denied\" / \"Read denied\"), not tools",
        {"Write": 1, "Read": 1}),
    "proxy/api/hooks/preview.py": (
        "the WOPI token's display name for the agent, not a tool",
        {"Agent": 1}),
    "proxy/core/layers/providers/openai_adapter.py": (
        "the vendor's server-side web search tool, named by the provider adapter (the providers package is not an engine package)",
        {"web_search": 5}),
    "proxy/core/layers/providers/anthropic_adapter.py": (
        "the vendor's server-side web search tool, named by the provider adapter",
        {"web_search": 1}),
}


def _tracked_py_files() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z", *_SCAN_ROOTS],
                             capture_output=True, check=True)
        rel = [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        # A tree without .git (a deployed copy): walk it with the same roots.
        rel = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for root in _SCAN_ROOTS for p in (REPO_ROOT / root).rglob("*.py")]
    files = [p for p in rel if p.endswith(".py") and not (set(Path(p).parts) & _EXCLUDED_PARTS)]
    assert len(files) > 100, "the scan found almost nothing — a checkout problem, not a clean tree"
    return files


def _engine_package_dirs() -> set[Path]:
    return {Path(inspect.getfile(type(layer))).resolve().parent for layer in get_all_layers().values()}


def _satellite_engine_files() -> set[Path]:
    files = set()
    for row in ENGINES.values():
        for ref in (row.headless, row.pty):
            mod = ref.partition(":")[0].lstrip(".").replace(".", "/")
            files.add((REPO_ROOT / "satellite" / mod).with_suffix(".py").resolve())
    return files


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _spelling(value: str) -> str | None:
    """The identity spelling a branch operand carries, or None."""
    v = value.strip("/")
    if v in CONFIG_DIRS:
        return v
    v = value.rstrip("-_/")
    if v in SHORT_NAMES:
        return v
    return None


def _scan_tool_names(path: Path) -> dict[str, int]:
    """String constants equal to a canonical tool name (docstrings excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="surrogateescape"))
    docs = _docstring_ids(tree)
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs and node.value in tool_roles.TOOL_ROLES):
            out[node.value] = out.get(node.value, 0) + 1
    return out


def _scan(path: Path) -> tuple[dict[str, int], dict[str, int], int]:
    """(id literals by literal, spelling branches by operand, id literals
    outside MODEL_REGISTRY) for one file."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="surrogateescape"))
    docs = _docstring_ids(tree)
    registry_span = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "MODEL_REGISTRY" for t in targets):
                registry_span = (node.lineno, node.end_lineno)
    ids: dict[str, int] = {}
    spellings: dict[str, int] = {}
    outside_registry = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            for eid in ENGINE_IDS:
                if eid in node.value:
                    ids[eid] = ids.get(eid, 0) + 1
                    if not (registry_span and registry_span[0] <= node.lineno <= registry_span[1]):
                        outside_registry += 1
        if isinstance(node, ast.Compare):
            for op in (node.left, *node.comparators):
                if isinstance(op, ast.Constant) and isinstance(op.value, str) and (s := _spelling(op.value)):
                    spellings[s] = spellings.get(s, 0) + 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("startswith", "endswith") and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and (s := _spelling(a.value)):
                spellings[s] = spellings.get(s, 0) + 1
    return ids, spellings, outside_registry


def _scan_tree() -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]], dict[str, int]]:
    dirs = _engine_package_dirs()
    owned = _satellite_engine_files()
    found_ids: dict[str, dict[str, int]] = {}
    found_spellings: dict[str, dict[str, int]] = {}
    outside: dict[str, int] = {}
    for rel in _tracked_py_files():
        p = (REPO_ROOT / rel).resolve()
        if p in owned or any(d == p.parent or d in p.parents for d in dirs):
            continue
        ids, spellings, out = _scan(p)
        if ids:
            found_ids[rel] = ids
            outside[rel] = out
        if spellings:
            found_spellings[rel] = spellings
    assert found_ids, "the id scan found no site at all — the registry itself names three"
    return found_ids, found_spellings, outside


_HINT_NEW = ("read the descriptor or call the layer instead; if the site must stay, add it to "
             "ADDING-AN-ENGINE.md section 6 and to this test with its reason")
_HINT_GONE = "retired: drop it from ADDING-AN-ENGINE.md section 6 and from this test"


def test_every_engine_id_literal_outside_the_engine_packages_is_a_known_site():
    found, _, outside = _scan_tree()
    for rel in _REGISTRY_ONLY:
        assert rel in found, f"{rel}: MODEL_REGISTRY no longer tags any engine?"
        assert outside[rel] == 0, f"{rel}: an engine id outside MODEL_REGISTRY — {_HINT_NEW}"
    counted = {rel: ids for rel, ids in found.items() if rel not in _REGISTRY_ONLY}
    expected = {rel: counts for rel, (_reason, counts) in KNOWN_ENGINE_ID_SITES.items()}
    new = {rel: ids for rel, ids in counted.items() if rel not in expected}
    gone = sorted(rel for rel in expected if rel not in counted)
    assert not new, f"new engine-id site(s) {new} — {_HINT_NEW}"
    assert not gone, f"{gone} — {_HINT_GONE}"
    drift = {rel: (counted[rel], expected[rel]) for rel in expected if counted[rel] != expected[rel]}
    assert not drift, f"the literals in a known site changed (found, expected): {drift} — {_HINT_NEW}"


def test_every_short_name_or_config_dir_branch_is_a_known_site():
    _, found, _ = _scan_tree()
    expected = {rel: counts for rel, (_reason, counts) in KNOWN_SPELLING_BRANCHES.items()}
    new = {rel: sp for rel, sp in found.items() if rel not in expected}
    gone = sorted(rel for rel in expected if rel not in found)
    assert not new, f"new branch(es) on an engine's short name or config dir {new} — {_HINT_NEW}"
    assert not gone, f"{gone} — {_HINT_GONE}"
    drift = {rel: (found[rel], expected[rel]) for rel in expected if found[rel] != expected[rel]}
    assert not drift, f"the branches in a known site changed (found, expected): {drift} — {_HINT_NEW}"


def test_every_tool_name_constant_outside_the_authority_is_a_known_site():
    """Pass 3 (core-seams phase 2): generic code asks ``tool_roles`` a role;
    a canonical tool name written anywhere else is a site this test names
    with its reason — or a leak."""
    dirs = _engine_package_dirs()
    owned = _satellite_engine_files()
    found: dict[str, dict[str, int]] = {}
    for rel in _tracked_py_files():
        p = (REPO_ROOT / rel).resolve()
        if rel in TOOL_NAME_AUTHORITY or p in owned or any(d == p.parent or d in p.parents for d in dirs):
            continue
        hits = _scan_tool_names(p)
        if hits:
            found[rel] = hits
    expected = {rel: counts for rel, (_reason, counts) in KNOWN_TOOL_NAME_SITES.items()}
    new = {rel: h for rel, h in found.items() if rel not in expected}
    gone = sorted(rel for rel in expected if rel not in found)
    assert not new, f"a canonical tool name outside core/events/tool_roles.py: {new} — ask tool_roles a role instead, or add the site here with its reason"
    assert not gone, f"{gone} — retired: drop it from this test"
    drift = {rel: (found[rel], expected[rel]) for rel in expected if found[rel] != expected[rel]}
    assert not drift, f"the tool-name constants in a known site changed (found, expected): {drift}"


def test_every_engine_package_has_its_layer_and_its_remote_half_exactly_when_remote():
    for path, layer in get_all_layers().items():
        pkg = Path(inspect.getfile(type(layer))).resolve().parent
        assert (pkg / "layer.py").is_file(), path
        remote = (pkg / "remote.py").is_file()
        assert remote == layer.capabilities.runtime.supports_remote_execution, (path, remote)


def test_every_satellite_row_names_two_class_modules_that_exist():
    for f in _satellite_engine_files():
        assert f.is_file(), f


_TS_ID = re.compile(r"""['"`](claude-code-cli|codex-cli|direct-llm)['"`]""")
_TS_TOOL_NAMES = "|".join(re.escape(n) for n in tool_roles.TOOL_ROLES)
# A tool name as a branch operand in TypeScript: ===/!== either side, a case
# label, a member asked of .includes( / .has( — the forms the vocabulary gate
# counts too. The mirror (lib/tools/) is the one place that may.
_TS_TOOL = re.compile(
    r"""(?:[=!]==\s*['"](?:""" + _TS_TOOL_NAMES + r""")['"]"""
    r"""|['"](?:""" + _TS_TOOL_NAMES + r""")['"]\s*[=!]=="""
    r"""|case\s+['"](?:""" + _TS_TOOL_NAMES + r""")['"]\s*:"""
    r"""|\.(?:includes|has)\(\s*['"](?:""" + _TS_TOOL_NAMES + r""")['"]\s*\))""")
_TS_TOOL_ALLOWED = {
    "dashboard/src/components/workspace/useWorkspaceKeyboardShortcuts.ts": "e.key === 'Delete' is a keyboard key",
}
_TS_SHORT = re.compile(
    r"""(?:\.(?:startsWith|includes|endsWith)\(\s*['"](claude|codex|direct)['"]\s*\)"""
    r"""|[=!]==\s*['"](claude|codex|direct)['"]"""
    r"""|['"](claude|codex|direct)['"]\s*[=!]==)""")


def test_the_dashboard_neither_quotes_an_engine_id_nor_branches_on_a_short_name():
    src = REPO_ROOT / "dashboard" / "src"
    hits = []
    for p in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel:
            continue
        text = p.read_text(encoding="utf-8", errors="surrogateescape")
        for m in _TS_ID.finditer(text):
            hits.append((rel, m.group(0)))
        for m in _TS_SHORT.finditer(text):
            hits.append((rel, m.group(0)))
    assert not hits, f"the dashboard reads the descriptor (lib/engines), never an engine id: {hits}"


def test_the_dashboard_branches_on_a_tool_name_only_in_its_mirror():
    src = REPO_ROOT / "dashboard" / "src"
    hits = []
    for p in list(src.rglob("*.ts")) + list(src.rglob("*.tsx")):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or rel.startswith("dashboard/src/lib/tools/") or rel in _TS_TOOL_ALLOWED:
            continue
        text = p.read_text(encoding="utf-8", errors="surrogateescape")
        for m in _TS_TOOL.finditer(text):
            hits.append((rel, m.group(0)))
    assert not hits, f"the dashboard asks lib/tools/roles a role, never a tool name: {hits}"
