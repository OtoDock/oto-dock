"""The layout and host-OS surface — the acceptance test of core-seams phase
10, in the shape of ``tests/core/test_kind_surface.py``.

Two machines, each named once. The agent tree (``core/layout.py``, vendored
into the satellite, mirrored in ``lib/layout/tree.ts``): the folder names,
the per-user tree, the sandbox-virtual roots. The host OS
(``core/host_os.py`` and its twin ``satellite/config.py``, mirrored in
``lib/hostOs/os.ts``): the three rows and the facts generic code asks.
A word composed or compared anywhere else is listed here WITH its reason,
and this test keeps the list exact in both directions.

Pass A (layout, Python): a ``/``-join chain, a ``join_under`` /
``resolve_under`` / ``os.path.join`` / ``Path(…)`` / ``_verified_*`` call,
a tuple / list / set literal or an f-string that names an ANCHOR (``users``,
``workspace``, ``knowledge``) scores every layout word it carries — the
anchors and the generic words (``config``, ``context``, ``.credentials``)
alike: a generic word is scored only beside an anchor (``MCPS_DIR /
"config"`` is not the tree); a ``Compare`` operand, a ``startswith`` /
``endswith`` argument or a tuple member that is an anchor, a virtual root
(``/users``, ``/workspace``, ``/knowledge``, ``/config``) or starts with
``users/`` scores. What this pass cannot see, and routes by hand where the
words compose: a regex or glob STRING (``step_runner._REL_RE``,
``path_roles._AGENT_CONFIG_CMD_RE``, the two globs in ``ws_client`` and
``session_files``) and prose (the prompt rows, error messages).

Pass B (host OS, Python): the seven OS words as compare operands / prefix
arguments / tuple members, and every READ of ``sys.platform``,
``platform.system()`` and ``os.name`` outside the two tables.

Pass C: the leaves are used module-qualified (``from core.layout import`` /
``from core.host_os import`` refused; the satellite reads
``config.HOST`` at call time — ``from ..config import HOST`` and a
module-level ``HOST = config.HOST`` refused); the dashboard compares the
folder words in ``lib/layout/`` only and spells the OS unions in
``lib/hostOs/`` only.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from tests._paths import REPO_ROOT

_SCAN_ROOTS = ("proxy", "satellite", "mcps/custom")
_EXCLUDED_PARTS = {"tests", "docs", "venv", ".venv", "_vendored", "skills", "node_modules", "__pycache__"}
_PREFIX_METHODS = ("startswith", "endswith")

LAYOUT_HOMES = ("proxy/core/layout.py",)
OS_HOMES = ("proxy/core/host_os.py", "satellite/config.py")

ANCHORS = frozenset({"users", "workspace", "knowledge"})
GENERIC = frozenset({"config", "context", ".credentials"})
LAYOUT_WORDS = ANCHORS | GENERIC
VIRTUAL_ROOTS = frozenset({"/users", "/workspace", "/knowledge", "/config"})
OS_WORDS = frozenset({"win32", "windows", "darwin", "linux", "macos", "nt", "cygwin"})
_JOIN_CALLS = frozenset({"join_under", "resolve_under", "_verified_literal_path", "_verified_session_dir", "Path", "join"})

# {path: (reason, {word: count})} — every site outside the homes, with why.
KNOWN_LAYOUT_SITES: dict[str, tuple[str, dict[str, int]]] = {
    "proxy/services/path_roles.py": ("the path_env ROLE names (`workspace`, `config`, …) the resolver dispatches on — a manifest vocabulary, not the folder", {"config": 1, "workspace": 2}),
    "mcps/custom/agent-config-mcp/server.py": ("an MCP process reading the tree the API serves (config/context, the knowledge mirrors); it cannot import the leaf", {"context": 1, "knowledge": 3}),
    "mcps/custom/display-mcp/app_tools.py": ("an MCP process recovering the agent root from OTO_WORKSPACE_DIR by the layout's rule (the strip; put to the operator as OTO_AGENT_DIR)", {"users": 1, "workspace": 2}),
    "mcps/custom/delegation-mcp/server.py": ("an MCP process composing the path HINT it writes into a task prompt — not a resolved path; it cannot import the leaf", {"users": 1, "workspace": 2}),
}
KNOWN_OS_SITES: dict[str, tuple[str, dict[str, int]]] = {
    "proxy/services/infra/safe_fs.py": ("vendored into file-tools with no proxy import: probes its own kernel for openat2", {"linux": 1, "sys.platform": 1}),
    "mcps/custom/file-tools-mcp/safe_fs.py": ("the byte copy of services/infra/safe_fs.py the file-tools image runs (the copy test pins it)", {"linux": 1, "sys.platform": 1}),
    "proxy/services/mcp/mcp_installer.py": ("vendored byte-for-byte into the satellite and self-contained by its docstring: its sys.platform reads and the package families (a manifest vocabulary) stay", {"win32": 3, "windows": 3, "darwin": 1, "linux": 1, "sys.platform": 3, "platform.system": 1}),
    "mcps/custom/computer-mcp/environment.py": ("an MCP process probing its own host (os_kind / display_backend)", {"darwin": 4, "linux": 2, "windows": 1, "sys.platform": 1}),
    "mcps/custom/computer-mcp/executor.py": ("an MCP process probing its own host", {"darwin": 2, "windows": 1}),
    "mcps/custom/computer-mcp/screen.py": ("an MCP process probing its own host", {"windows": 1}),
    "mcps/custom/computer-mcp/server.py": ("an MCP process probing its own host", {"darwin": 1, "windows": 1}),
    "mcps/custom/ssh-hosts/server.py": ("an MCP process reading its own host for the ControlMaster question", {"platform.system": 1}),
    "satellite/terminal/otodock_cli.py": ("the standalone otodock terminal client: its fallback import branch has no package, so it cannot read config.HOST", {"win32": 2, "sys.platform": 2}),
}


def _tracked_files(suffixes: tuple[str, ...], roots: tuple[str, ...]) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard", *roots], capture_output=True, check=True)
        rel = [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        rel = [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for root in roots for s in suffixes for p in (REPO_ROOT / root).rglob(f"*{s}")]
    return sorted({p for p in rel if p.endswith(suffixes) and not (set(Path(p).parts) & _EXCLUDED_PARTS)
                   and (REPO_ROOT / p).is_file()})


def _py_files() -> list[str]:
    return _tracked_files((".py",), _SCAN_ROOTS)


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _div_chain(node: ast.BinOp) -> list[str]:
    """The string operands of a left-recursive ``/`` chain."""
    out: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        s = _str(cur.right)
        if s is not None:
            out.append(s)
        cur = cur.left
    s = _str(cur)
    if s is not None:
        out.append(s)
    return out


def _is_layout_word_in_text(text: str) -> set[str]:
    """The layout words a composed string carries as path segments."""
    found = set()
    for seg in re.split(r"[/\\]", text):
        if seg in LAYOUT_WORDS:
            found.add(seg)
    return found


def _scan_layout(path: str) -> dict[str, int]:
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"), filename=path)
    counts: dict[str, int] = defaultdict(int)

    def score_group(words: list[str]) -> None:
        if any(w in ANCHORS for w in words):
            for w in words:
                if w in LAYOUT_WORDS:
                    counts[w] += 1

    seen_binops: set[int] = set()
    for node in ast.walk(tree):
        # (a) a ``/`` join chain — score the OUTERMOST chain once
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in seen_binops:
            chain = _div_chain(node)
            cur: ast.AST = node
            while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
                seen_binops.add(id(cur))
                cur = cur.left
            score_group(chain)
        # (b) a join call
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in _JOIN_CALLS:
                words = [s for s in (_str(a) for a in node.args) if s is not None]
                score_group(words)
            elif name in _PREFIX_METHODS:
                for a in node.args:
                    s = _str(a)
                    if s is None and isinstance(a, ast.Tuple):
                        for e in a.elts:
                            es = _str(e)
                            if es is not None and _scores_as_operand(es):
                                counts[_operand_word(es)] += 1
                    elif s is not None and _scores_as_operand(s):
                        counts[_operand_word(s)] += 1
        # (c) an f-string
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            words = _is_layout_word_in_text(text)
            if words & ANCHORS:
                for w in words:
                    counts[w] += 1
        # (d) a compare operand
        elif isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                if isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
                    members = [s for s in (_str(e) for e in operand.elts) if s is not None]
                    if any(w in ANCHORS or w in VIRTUAL_ROOTS for w in members):
                        for w in members:
                            if w in LAYOUT_WORDS or w in VIRTUAL_ROOTS:
                                counts[w.lstrip("/") if w in VIRTUAL_ROOTS else w] += 1
                else:
                    s = _str(operand)
                    if s is not None and _scores_as_operand(s):
                        counts[_operand_word(s)] += 1
        # (e) a module-level tuple / set / list literal (a head set)
        elif isinstance(node, ast.Assign) and isinstance(node.value, (ast.Tuple, ast.Set, ast.List)):
            members = [s for s in (_str(e) for e in node.value.elts) if s is not None]
            if any(w in ANCHORS for w in members):
                for w in members:
                    if w in LAYOUT_WORDS:
                        counts[w] += 1
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) and node.value.func.id == "frozenset" \
                and node.value.args and isinstance(node.value.args[0], (ast.Set, ast.Tuple, ast.List)):
            members = [s for s in (_str(e) for e in node.value.args[0].elts) if s is not None]
            if any(w in ANCHORS for w in members):
                for w in members:
                    if w in LAYOUT_WORDS:
                        counts[w] += 1
    return dict(counts)


def _scores_as_operand(s: str) -> bool:
    return s in ANCHORS or s in VIRTUAL_ROOTS or s.startswith("users/") or s.startswith("/users/")


def _operand_word(s: str) -> str:
    if s in VIRTUAL_ROOTS:
        return s.lstrip("/")
    if s.startswith("users/") or s.startswith("/users/"):
        return "users"
    return s


def _scan_os(path: str) -> dict[str, int]:
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"), filename=path)
    counts: dict[str, int] = defaultdict(int)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                elts = operand.elts if isinstance(operand, (ast.Tuple, ast.List, ast.Set)) else [operand]
                for e in elts:
                    s = _str(e)
                    if s is not None and s in OS_WORDS:
                        counts[s] += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _PREFIX_METHODS:
            for a in node.args:
                s = _str(a)
                if s is not None and s in OS_WORDS:
                    counts[s] += 1
        elif isinstance(node, ast.match_case) and isinstance(node.pattern, ast.MatchValue):
            s = _str(node.pattern.value)
            if s is not None and s in OS_WORDS:
                counts[s] += 1
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "sys" and node.attr == "platform":
                counts["sys.platform"] += 1
            elif node.value.id == "os" and node.attr == "name":
                counts["os.name"] += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "platform" \
                and node.func.attr == "system":
            counts["platform.system"] += 1
    return dict(counts)


def _diff(found: dict[str, dict[str, int]], known: dict[str, tuple[str, dict[str, int]]]) -> list[str]:
    problems = []
    for path, counts in sorted(found.items()):
        if path not in known:
            problems.append(f"UNLISTED {path}: {dict(sorted(counts.items()))}")
        elif known[path][1] != counts:
            problems.append(f"CHANGED {path}: found {dict(sorted(counts.items()))}, listed {dict(sorted(known[path][1].items()))}")
    for path in known:
        if path not in found:
            problems.append(f"STALE {path}: listed but no site remains — retire the row")
    return problems


# ---------------------------------------------------------------------------
# pass A — the agent tree
# ---------------------------------------------------------------------------

def test_pass_a_every_layout_word_outside_the_leaf_is_a_known_site():
    found = {}
    for path in _py_files():
        if path in LAYOUT_HOMES:
            continue
        counts = _scan_layout(path)
        if counts:
            found[path] = counts
    problems = _diff(found, KNOWN_LAYOUT_SITES)
    assert not problems, "\n" + "\n".join(problems)


def test_the_leaf_spells_every_layout_word():
    src = (REPO_ROOT / LAYOUT_HOMES[0]).read_text(encoding="utf-8")
    for w in ("users", "workspace", "knowledge", "config", "context", ".credentials"):
        assert f'"{w}"' in src, w


# ---------------------------------------------------------------------------
# pass B — the host OS
# ---------------------------------------------------------------------------

def test_pass_b_every_os_word_and_probe_outside_the_tables_is_a_known_site():
    found = {}
    for path in _py_files():
        if path in OS_HOMES:
            continue
        counts = _scan_os(path)
        if counts:
            found[path] = counts
    problems = _diff(found, KNOWN_OS_SITES)
    assert not problems, "\n" + "\n".join(problems)


def test_the_two_tables_read_the_interpreter_and_nothing_else_does():
    for home in OS_HOMES:
        c = _scan_os(home)
        assert c.get("sys.platform") == 1 and c.get("platform.system") == 1, (home, c)


# ---------------------------------------------------------------------------
# pass C — module-qualified use, the satellite's one global, the dashboard
# ---------------------------------------------------------------------------

def test_pass_c_the_leaves_are_used_module_qualified():
    bad = []
    for path in _py_files():
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        if re.search(r"^from core\.(layout|host_os) import", text, re.M):
            bad.append(path)
        if path.startswith("satellite/") and path != "satellite/config.py":
            if re.search(r"^from \.{1,2}config import [^\n]*\bHOST\b", text, re.M):
                bad.append(f"{path} (binds HOST at import)")
            if re.search(r"^HOST = config\.HOST", text, re.M):
                bad.append(f"{path} (rebinds HOST)")
            if re.search(r"^from \.{1,2}_vendored\.layout import", text, re.M):
                bad.append(f"{path} (the vendored leaf is used module-qualified)")
            bad.extend(f"{path}:{ln} (a local `config` shadows the module at a HOST read)"
                       for ln in _shadowed_host_reads(text))
    assert not bad, bad


def _shadowed_host_reads(text: str) -> list[int]:
    """The lines where ``config.HOST`` is read inside a function that binds
    its own ``config`` (a parameter or a local — ``_main`` binds the loaded
    ``SatelliteConfig`` to that name): the read hits the object, not the
    module, and only a boot shows it (T1 did, twice). Such a module imports
    the module under another name."""
    tree = ast.parse(text)
    out: list[int] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        if "config" not in names:
            continue
        out.extend(n.lineno for n in ast.walk(fn)
                   if isinstance(n, ast.Attribute) and n.attr == "HOST"
                   and isinstance(n.value, ast.Name) and n.value.id == "config")
    return out


_TS_LAYOUT_MIRROR = "dashboard/src/lib/layout/"
_TS_OS_MIRROR = "dashboard/src/lib/hostOs/"
_TS_LAYOUT_COMPARE = re.compile(r"(?:===|!==|case)\s*'(?:users|workspace|knowledge|config|context)'|startsWith\('users/'\)")
_TS_OS_UNION = re.compile(r"'linux'\s*\|\s*'(?:macos|darwin)'|'x11'\s*\|\s*'wayland'|'darwin'\s*\|")


def test_pass_c_the_dashboard_compares_the_words_in_the_mirrors_only():
    ts_files = _tracked_files((".ts", ".tsx"), ("dashboard/src",))
    ts_files = [p for p in ts_files if not p.endswith((".test.ts", ".test.tsx", ".d.ts"))]
    bad = []
    for path in ts_files:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        if not path.startswith(_TS_LAYOUT_MIRROR) and _TS_LAYOUT_COMPARE.search(text):
            bad.append(path)
        if not path.startswith(_TS_OS_MIRROR) and _TS_OS_UNION.search(text):
            bad.append(f"{path} (an OS union)")
    assert not bad, bad
