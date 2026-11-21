"""Every root handed to the safe file helpers: one of a few named kinds, or a judged row.

A call of the safe helpers (``services/infra/safe_fs.py``, and the
satellite's twin ``satellite/host/safe_fs.py``) is only as sound as its root:
the helpers open the root by its real place, then refuse a link at any part
below it. So a root must be a folder no session, person, tool or paired
computer can replace. The surface pass (``test_agent_tree_surface.py``) reads
every call of a helper that takes a root, and this test names each root:

- ``agents``: the agents folder (``config.AGENTS_DIR``);
- ``agent``: an agent's one root (``core.agent_folder.agent_root`` and
  ``beneath_agent``), its folder resolved once; on the satellite, its
  agents folder and an agent's folder as its config names them;
- ``sessions``: the sessions folder (``config.SESSIONS_DIR``);
- ``tools``: the tool folders (``config.MCPS_DIR``);
- ``own``: one of the server's own data folders that no session reaches;
- ``private``: a temporary folder the server made for itself;
- ``handle``: a held handle (a descriptor from ``open_root``, an int);

or a row of ``agent_tree_sites.py`` (``PROXY_ROOTS``, ``SATELLITE_ROOTS``)
that says why that root is sound. A root is followed through the tree's own
code: a name to what it was bound to, a parameter to what every caller in the
tree hands it (and its default), a call of a tree function to what it
returns. A folder below a named root is not named: only the root itself is
known to be a folder no other process replaces.

The list is exact both ways: a new root the pass cannot name fails until it
is named or listed, and a listed root that is gone or now named fails until
its row leaves. What stays invisible: a caller the pass cannot see (a method
called on an object whose class it cannot tell, a function handed on as a
value) is counted as unnamed, never as named; a name rebound by code run by
name, and a root built inside the helpers' owners, which their own tests
judge.
"""
from __future__ import annotations

import ast
import collections
import functools
import json
import sys
import textwrap
from typing import NamedTuple

import pytest

from tests import _import_names as names
from tests.core.agent_tree_sites import (
    AGENTS,
    OWN,
    PROXY_ROOTS,
    SATELLITE_ROOTS,
    TO_JUDGE,
    Site,
)
from tests.core.test_agent_tree_surface import (
    _REASON_NAMES,
    KEYWORD_ONLY,
    _differences,
    _Facts,
    _facts,
    _facts_of,
    _Function,
    _line,
    _parts,
    _unexplained,
    root_helpers,
)

#: The kinds a root may be named as, each with the words the failure prints.
KINDS = {"agents": "the agents folder", "agent": "an agent's one root", "sessions": "the sessions folder",
         "tools": "the tool folders", "own": "one of the server's own folders", "private": "a temporary folder of "
         "its own", "handle": "a held handle"}
UNNAMED = "?"


class _Root(NamedTuple):
    """One root of one call, as the list keys it: file, function and the
    root's spelling; and the kinds the pass names it as."""

    rel: str
    function: str
    spelling: str
    kinds: frozenset[str]


class _Resolver:
    """The kinds of a root's term, followed through one tree's functions.
    A term met again while it is followed adds nothing on that path, and a
    result reached through such a loop is not kept, so a later reading
    starts afresh."""

    def __init__(self, facts: dict[str, _Facts]) -> None:
        self._functions: dict[str, _Function] = {}
        self._callers: dict[str, list[tuple[tuple, dict[str, tuple], int]]] = collections.defaultdict(list)
        self._referenced: set[str] = set()
        self._loose: set[str] = set()
        for module in facts.values():
            self._functions.update(module.functions)
            for callee, positional, keywords, starred in module.handed:
                self._callers[callee].append((positional, keywords, starred))
            self._referenced |= module.referenced
            self._loose |= module.loose
        self._known: dict[tuple, frozenset[str]] = {}
        self._open: set[tuple] = set()

    def kinds(self, term: tuple) -> frozenset[str]:
        found, _whole = self._kinds(term)
        return found or frozenset({UNNAMED})

    def _kinds(self, term: tuple) -> tuple[frozenset[str], bool]:
        if term in self._known:
            return self._known[term], True
        if term in self._open:
            return frozenset(), False
        self._open.add(term)
        try:
            found, whole = self._read(term)
        finally:
            self._open.discard(term)
        if whole:
            self._known[term] = found
        return found, whole

    def _union(self, terms) -> tuple[frozenset[str], bool]:
        found: set[str] = set()
        whole = True
        for term in terms:
            kinds, complete = self._kinds(term)
            found |= kinds
            whole = whole and complete
        return frozenset(found), whole

    def _read(self, term: tuple) -> tuple[frozenset[str], bool]:
        kind = term[0]
        if kind == "k":
            return frozenset({term[1]}), True
        if kind == "or":
            return self._union(term[1])
        if kind == "r":
            return self._returned(term[1], term[2])
        if kind == "p":
            return self._handed(term[1], term[2])
        return frozenset({UNNAMED}), True

    def _returned(self, callee: str, index: int | None) -> tuple[frozenset[str], bool]:
        """What a tree function returns (one element of a returned tuple)."""
        function = self._functions.get(callee)
        if function is None or not function.returns:
            return frozenset({UNNAMED}), True
        terms = []
        for returned in function.returns:
            if returned[0] == "tuple":
                terms.append(returned[1][index] if index is not None and index < len(returned[1]) else ("u",))
            else:
                terms.append(returned[1] if index is None else returned[2 + index] if index < 2 else ("u",))
        return self._union(terms)

    def _handed(self, callee: str, name: str) -> tuple[frozenset[str], bool]:
        """What every caller in the tree hands a parameter, or its default
        where a caller leaves it out. A function read as a value, a method
        whose name is also called on an object, and a function no one in the
        tree calls have callers out of sight: the parameter is unnamed."""
        function = self._functions.get(callee)
        callers = self._callers.get(callee, [])
        if (function is None or not callers or callee in self._referenced
                or (function.method and callee.rpartition(".")[2] in self._loose)):
            return frozenset({UNNAMED}), True
        place = function.positional.index(name) if name in function.positional else None
        terms = []
        for positional, keywords, starred in callers:
            if name in keywords:
                terms.append(keywords[name])
            elif place is not None and place < starred and place < len(positional):
                terms.append(positional[place])
            elif (place is not None and starred < len(positional)) or "**" in keywords:
                terms.append(("u",))
            elif name in function.defaults:
                terms.append(function.defaults[name])
            else:
                terms.append(("u",))
        return self._union(terms)


@functools.cache
def _roots(tree: str) -> tuple[_Root, ...]:
    """Every root of ``tree`` with the kinds it is named as."""
    facts = _facts(tree)
    resolver = _Resolver(facts)
    return tuple(_Root(rel, function, spelling, resolver.kinds(term))
                 for rel, module in facts.items() for function, term, spelling in module.roots)


def _rows(tree: str) -> dict[tuple[str, str], Site]:
    return PROXY_ROOTS if tree == "proxy" else SATELLITE_ROOTS


def _unnamed(roots) -> dict[tuple[str, str], list[str]]:
    """The roots no kind names: ``(file, function)`` -> their spellings."""
    found: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for root in roots:
        if UNNAMED in root.kinds:
            found[(root.rel, root.function)].append(root.spelling)
    return {key: sorted(spellings) for key, spellings in found.items()}


def _instruction(tree: str, new: list[str], grown: list[str]) -> str:
    """What to do, for a contributor who never saw this test."""
    lists = "PROXY_ROOTS" if tree == "proxy" else "SATELLITE_ROOTS"
    kinds = ", ".join(f"{words} ({kind})" for kind, words in KINDS.items())
    hand_named = (
        "A safe helper opens its root by its real place and refuses a link only below it, so the root must be a "
        "folder no session, person, tool or paired computer can replace. Hand the helper one of the roots the "
        f"test names, with the rest of the path as the rel: {kinds}. For a path in an agent's folder, "
        "core.agent_folder.beneath_agent gives both. Then the root leaves the list.")
    reasons = ", ".join(list(_REASON_NAMES.values())[:-1]) + " or " + list(_REASON_NAMES.values())[-1]
    parts = []
    if new:
        rows = [f'    ("{rel}", "{function}"): Site(<reason>, {json.dumps(" ".join(sorted(spellings)))}, '
                f'"<what the root is and why no other process replaces it>"),'
                for rel, function, spellings in map(_parts, new)]
        parts.append(
            f"A root handed to the safe file helpers is none the test can name, and not on the list in "
            f"proxy/tests/core/agent_tree_sites.py ({lists}):\n  " + "\n  ".join(new) + "\n\n" + hand_named
            + " If the root is sound as it is, add the row below with a reason from "
            "proxy/tests/core/agent_tree_sites.py (" + reasons + ", each defined there) and a why in place of "
            "<...>:\n" + "\n".join(rows))
    if grown:
        rows = []
        for rel, function, spellings in map(_parts, grown):
            site = _rows(tree)[(rel, function)]
            merged = " ".join(sorted([*site.calls.split(), *spellings]))
            rows.append(f'    ("{rel}", "{function}"): Site({_REASON_NAMES[site.reason]}, {json.dumps(merged)}, "{site.why}"),')
        parts.append(
            f"A function that already has a row in proxy/tests/core/agent_tree_sites.py ({lists}) hands the safe "
            "helpers a new root the test cannot name:\n  " + "\n  ".join(grown) + "\n\nThe row's why must hold "
            "for the new root too. " + hand_named + " If the why holds, change the row's roots to these, never add "
            "a second row:\n" + "\n".join(rows))
    return "\n\n".join(parts)


def _assert_exact(tree: str, found: dict[tuple[str, str], list[str]], files: set[str] | None = None) -> None:
    diff = _differences(tree, found, files, _rows(tree))
    problems = []
    if diff.new or diff.grown:
        problems.append(_instruction(tree, diff.new, diff.grown))
    if diff.moved:
        problems.append("A function or a file was renamed or moved; change the key of its rows, the judgement stays:"
                        "\n  " + "\n  ".join(diff.moved))
    if diff.gone:
        problems.append("Listed in proxy/tests/core/agent_tree_sites.py and no longer an unnamed root in the code "
                        "(it left, or the test names it now). Take these roots out of their rows (a row left with "
                        "none goes):\n  " + "\n  ".join(diff.gone))
    assert not problems, "\n\n".join(problems)


@pytest.mark.parametrize("tree", ["proxy", "satellite"])
def test_every_root_handed_to_the_safe_helpers_is_named_or_listed(tree):
    _assert_exact(tree, _unnamed(_roots(tree)))


def _unexplained_roots(tree: str) -> list[str]:
    return _unexplained(tree, _rows(tree))


@pytest.mark.parametrize("tree", ["proxy", "satellite"])
def test_every_root_row_says_why(tree):
    unexplained = _unexplained_roots(tree)
    assert not unexplained, "In proxy/tests/core/agent_tree_sites.py:\n  " + "\n  ".join(unexplained)


def test_no_root_row_waits_to_be_judged():
    waiting = [f"{tree}: {key}" for tree in ("proxy", "satellite") for key, site in _rows(tree).items()
               if site.why.startswith(TO_JUDGE)]
    assert not waiting, f"root rows still to judge: {waiting}"


@pytest.mark.parametrize("tree", ["proxy", "satellite"])
def test_the_helpers_read_are_the_owners_own(tree):
    """Read from the owner: every helper that opens beneath a root, each
    root at its place, and nothing that only reads a path's text."""
    helpers = root_helpers(tree)
    assert helpers["open_root"] == ((0, "root"),) and helpers["rename_beneath"][0] == (0, "root")
    assert "rel_under" not in helpers
    if tree == "proxy":
        assert helpers["copy_file_beneath"] == ((0, "src_root"), (2, "dst_root"))
        assert helpers["move_beneath"] == ((0, "root"), (KEYWORD_ONLY, "dst_root"))


def _module_roots(source: str, rel: str = "services/zz_new.py", tree: str = "proxy") -> list[tuple[str, str, str]]:
    """``(function, spelling, kinds)`` of each root one module's text hands
    the safe helpers, its names read through its own imports."""
    module = ast.parse(textwrap.dedent(source))
    bound = names.bindings(module, rel)
    facts = {rel: _facts_of(tree, rel, module, bound, {names.module_of(rel): bound})}
    resolver = _Resolver(facts)
    return [(function, spelling, "+".join(sorted(resolver.kinds(term)))) for function, term, spelling in facts[rel].roots]


_PROXY_HEAD = """
import asyncio
import tempfile
import config
from config import AGENTS_DIR
from core import agent_folder
from core.agent_folder import beneath_agent
from services.infra import safe_fs
"""

_SPELLINGS = {
    "the agents folder": ("def f(rel):\n    safe_fs.unlink_beneath(AGENTS_DIR, rel)", "agents"),
    "the agents folder through its module": ("def f(rel):\n    safe_fs.lstat_beneath(config.AGENTS_DIR, rel)",
                                             "agents"),
    "an agent's one root": ("def f(a, rel):\n    safe_fs.read_bytes_beneath(agent_folder.agent_root(a), rel)",
                            "agent"),
    "beneath_agent starred": ("def f(p):\n    safe_fs.unlink_beneath(*beneath_agent(p))", "agent"),
    "beneath_agent unpacked": ("def f(p):\n    root, rel = beneath_agent(p)\n    safe_fs.unlink_beneath(root, rel)",
                               "agent"),
    "the sessions folder": ("def f(b):\n    safe_fs.atomic_write_beneath(config.SESSIONS_DIR, 'x', b)", "sessions"),
    "the tool folders": ("def f(rel):\n    safe_fs.walk_beneath(config.MCPS_DIR, rel)", "tools"),
    "one of the server's own": ("def f(rel):\n    safe_fs.mkdirs_beneath(config.RECOVER_BIN_DIR, rel)", "own"),
    "a temporary folder of its own": ("def f():\n    tmp = tempfile.mkdtemp()\n    safe_fs.rmtree_beneath(tmp, 'x')",
                                      "private"),
    "a held handle": ("def f(rel):\n    with safe_fs.open_root(AGENTS_DIR) as fd:\n"
                      "        safe_fs.lstat_beneath(fd, rel)", "agents\nhandle"),
    "an int parameter": ("def f(fd: int, rel):\n    safe_fs.unlink_beneath(fd, rel)", "handle"),
    "a parameter takes its callers' kinds": (
        "def _g(root, rel):\n    safe_fs.unlink_beneath(root, rel)\n"
        "def f():\n    _g(AGENTS_DIR, 'a')\n    _g(rel='b', root=config.SESSIONS_DIR)", "agents+sessions"),
    "a parameter through to_thread": (
        "def _g(root, rel):\n    safe_fs.unlink_beneath(root, rel)\n"
        "async def f():\n    await asyncio.to_thread(_g, config.SESSIONS_DIR, 'a')", "sessions"),
    "a parameter left out takes its default": (
        "def _g(rel, root=AGENTS_DIR):\n    safe_fs.unlink_beneath(root, rel)\ndef f():\n    _g('a')", "agents"),
    "a call takes the kind its function returns": (
        "def _r(a):\n    return agent_folder.agent_root(a)\ndef f(a):\n    safe_fs.lstat_beneath(_r(a), 'x')", "agent"),
    "a returned tuple": (
        "def _r(a):\n    return config.AGENTS_DIR, a\ndef f(a):\n    root, rel = _r(a)\n"
        "    safe_fs.lstat_beneath(root, rel)", "agents"),
    "the second element of a returned call": (
        "def _q(a):\n    return a, config.AGENTS_DIR\ndef _r(a):\n    return _q(a)\ndef f(a):\n"
        "    rel, root = _r(a)\n    safe_fs.lstat_beneath(root, rel)", "agents"),
    "a function returning beneath_agent": (
        "def _r(p):\n    return beneath_agent(p)\ndef f(p):\n    safe_fs.lstat_beneath(*_r(p))", "agent"),
    "a function's return through to_thread": (
        "def _r(p):\n    return beneath_agent(p)\nasync def f(p):\n"
        "    root, rel = await asyncio.to_thread(_r, p)\n    safe_fs.lstat_beneath(root, rel)", "agent"),
    "both roots of a copy, one by keyword": (
        "def f(a, b):\n    safe_fs.copy_file_beneath(AGENTS_DIR, a, dst_rel=b, dst_root=config.SESSIONS_DIR)",
        "agents\nsessions"),
    "the roots a loop takes": (
        "def f():\n    for root in (AGENTS_DIR, config.SESSIONS_DIR):\n        safe_fs.walk_beneath(root, '')",
        "agents+sessions"),
    "a spelling keeps its root": ("def f(rel):\n    safe_fs.unlink_beneath(str(AGENTS_DIR.resolve()), rel)", "agents"),
    "a folder below the agents folder": ("def f(a, rel):\n    safe_fs.unlink_beneath(AGENTS_DIR / a, rel)", "?"),
    "a file's parent": ("def f(path):\n    safe_fs.unlink_beneath(path.parent, path.name)", "?"),
    "the parent of a named root": ("def f(rel):\n    safe_fs.unlink_beneath(config.SESSIONS_DIR.parent, rel)", "?"),
    "a parameter no one in the tree calls": ("def f(root, rel):\n    safe_fs.unlink_beneath(root, rel)", "?"),
    "a local that hides the import": (
        "def f(rel):\n    AGENTS_DIR = rel\n    safe_fs.unlink_beneath(AGENTS_DIR, rel)", "?"),
    "a function read as a value": (
        "def _g(root):\n    safe_fs.unlink_beneath(root, 'x')\nVERBS = {'g': _g}\ndef f():\n    _g(AGENTS_DIR)", "?"),
    "a method also called on an object": (
        "class A:\n    def g(self, root):\n        safe_fs.unlink_beneath(root, 'x')\n"
        "    def f(self):\n        self.g(AGENTS_DIR)\ndef h(a, p):\n    a.g(p)", "?"),
    "a method called on itself alone": (
        "class A:\n    def g(self, root):\n        safe_fs.unlink_beneath(root, 'x')\n"
        "    def f(self):\n        self.g(AGENTS_DIR)", "agents"),
    "a partial's later arguments are out of sight": (
        "import functools\ndef _g(rel, root=AGENTS_DIR):\n    safe_fs.unlink_beneath(root, rel)\n"
        "def f():\n    return functools.partial(_g, 'x')", "?"),
    "a function that calls itself": (
        "def _g(root, n):\n    safe_fs.unlink_beneath(root, 'x')\n    _g(root, n - 1)\ndef f():\n    _g(AGENTS_DIR, 3)",
        "agents"),
    "a loop of two functions keeps what one caller out of sight adds": (
        "def _b(root):\n    safe_fs.lstat_beneath(root, 'y')\n    _a(root)\n"
        "def _a(root):\n    safe_fs.unlink_beneath(root, 'x')\n    _b(root)\n"
        "def f():\n    _a(AGENTS_DIR)\ndef h(p):\n    _b(p)", "?+agents\n?+agents"),
    "a root handed on by a star": (
        "def _g(root=AGENTS_DIR, rel='x'):\n    safe_fs.unlink_beneath(root, rel)\ndef f(args):\n    _g(*args)", "?"),
}


@pytest.mark.parametrize(("source", "kinds"), list(_SPELLINGS.values()), ids=list(_SPELLINGS))
def test_the_pass_names_the_spellings_of_a_root(source, kinds):
    found = _module_roots(_PROXY_HEAD + source)
    assert "\n".join(sorted(k for _f, _s, k in found)) == kinds, found


def test_the_satellites_agents_folders_and_its_helpers_are_read():
    found = _module_roots("""
from host import safe_fs
class Sync:
    def f(self, rel, b):
        safe_fs.atomic_write_beneath(self.agents_dir, rel, b)
        safe_fs.unlink_beneath(self.agent_dir, rel)
        safe_fs.unlink_beneath(self.home, rel)
""", "transport/zz_new.py", "satellite")
    assert [k for _f, _s, k in found] == ["agents", "agent", "?"], found


def test_a_new_root_the_test_cannot_name_fails_and_says_what_to_do():
    rel = "services/zz_new.py"
    found = _unnamed(_Root(rel, function, spelling, frozenset({kinds}))
                     for function, spelling, kinds in _module_roots(_PROXY_HEAD + _SPELLINGS["a file's parent"][0], rel))
    with pytest.raises(AssertionError) as failed:
        _assert_exact("proxy", found, {rel})
    text = str(failed.value)
    assert "services/zz_new.py, function f: path.parent" in text
    assert '("services/zz_new.py", "f"): Site(<reason>, "path.parent", "<what the root is' in text
    assert "beneath_agent" in text and "the agents folder (agents)" in text and "OWN" in text


def test_a_row_whose_root_is_named_now_or_gone_fails():
    key = next(iter(PROXY_ROOTS))
    found = {k: v for k, v in _unnamed(_roots("proxy")).items() if k != key}
    with pytest.raises(AssertionError) as failed:
        _assert_exact("proxy", found, {key[0]})
    assert "no longer an unnamed root" in str(failed.value) and _line(key, sorted(PROXY_ROOTS[key].calls.split())) in (
        str(failed.value))


def test_a_second_unnamed_root_in_a_listed_function_shows_its_row_merged():
    key, site = next(iter(PROXY_ROOTS.items()))
    found = dict(_unnamed(_roots("proxy")))
    found[key] = sorted([*found[key], "other_root"])
    with pytest.raises(AssertionError) as failed:
        _assert_exact("proxy", found, {key[0]})
    merged = " ".join(sorted([*site.calls.split(), "other_root"]))
    assert f"Site({_REASON_NAMES[site.reason]}, {json.dumps(merged)}, " in str(failed.value)


def test_a_root_row_with_no_why_or_a_pasted_one_fails(monkeypatch):
    rows = {("services/zz_new.py", "f"): Site(OWN, "path.parent", ""),
            ("services/zz_new.py", "g"): Site(OWN, "root", "<what the root is and why no other process replaces it>"),
            ("services/zz_new.py", "h"): Site(AGENTS, "root", "a folder the server made")}
    monkeypatch.setattr(sys.modules[__name__], "PROXY_ROOTS", rows)
    unexplained = _unexplained_roots("proxy")
    assert [line.split(":")[0] for line in unexplained] == [f"services/zz_new.py, function {f}" for f in "fgh"]
