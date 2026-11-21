"""The file program and the general loop, as they run inside a sandbox.

Both texts are loaded by their path: this file imports nothing of the proxy,
so it also runs on the oldest Python a sandbox holds (3.10), with
``--noconftest``. The rows here are plain temporary folders: what the
kernel's mounts add (a mask, a read-only row, another person's folder that
is not there) is ``test_direct_files_sandboxed.py``'s.
"""

import ast
import contextlib
import errno
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

_DIRECT = Path(__file__).resolve().parents[2] / "core" / "layers" / "direct"
_RUNTIME = _DIRECT / "sandbox_program_runtime.py"
_PROGRAM = _DIRECT / "file_program.py"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_oto_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fp = _load(_PROGRAM)
rt = _load(_RUNTIME)


def _joined() -> str:
    """The one text a sandbox runs (``sandbox_program.program_text``'s rule)."""
    return f"{_RUNTIME.read_text()}\n{_PROGRAM.read_text()}\n_serve(OPERATIONS)\n"


@pytest.fixture
def tree(tmp_path):
    """Two rows (``ws`` and ``other``), a folder that is no row, a skills
    folder, and the facts the platform would send."""
    root = Path(os.path.realpath(tmp_path))
    ws, other, outside, skills = root / "ws", root / "other", root / "outside", root / "cfg" / "skills"
    for folder in (ws / "sub", other, outside, skills):
        folder.mkdir(parents=True)
    (ws / "a.md").write_text("alpha\nbeta\n")
    (ws / "sub" / "b.md").write_text("gamma\n")
    (other / "o.md").write_text("other row\n")
    (outside / "secret.md").write_text("not a row\n")
    facts = {
        "rows": [str(ws), str(other)], "refused": [".claude", ".git"], "glob_skip": [".git", "node_modules"],
        "read_max": 64, "glob_max": 5, "skills": str(skills), "skill_max": 32, "write_max": 64,
    }
    return ws, other, outside, skills, facts


def _op(operation, facts, **args):
    return fp.OPERATIONS[operation](facts, args)


# -- read ---------------------------------------------------------------------

def test_read_returns_the_text(tree):
    ws, _o, _x, _s, facts = tree
    assert _op("read", facts, path=str(ws / "a.md")) == {"ok": True, "text": "alpha\nbeta\n", "truncated": False}


def test_read_stops_at_the_cap(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "big.md").write_text("x" * 200)
    answer = _op("read", facts, path=str(ws / "big.md"))
    assert answer["ok"] is True and len(answer["text"]) == 64 and answer["truncated"] is True


@pytest.mark.parametrize("name, code", [("gone.md", "missing"), ("sub", "folder"), ("a.md/x", "missing")])
def test_read_refuses_what_is_no_file(tree, name, code):
    ws, _o, _x, _s, facts = tree
    assert _op("read", facts, path=f"{ws}/{name}")["code"] == code


def test_read_refuses_a_binary_file(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "b.bin").write_bytes(b"ab\x00cd")
    assert _op("read", facts, path=str(ws / "b.bin"))["code"] == "binary"


def test_a_pipe_is_refused_at_once(tree):
    ws, _o, _x, _s, facts = tree
    os.mkfifo(ws / "pipe")
    began = time.monotonic()
    assert _op("read", facts, path=str(ws / "pipe"))["code"] == "special"
    assert time.monotonic() - began < 2


@pytest.mark.parametrize("bad", ["relative.md", "{ws}/../ws/a.md", "{ws}//a.md", "{ws}/a.md\x00", 7, None])
def test_a_path_that_is_not_normal_is_refused(tree, bad):
    ws, _o, _x, _s, facts = tree
    path = bad.format(ws=ws) if isinstance(bad, str) else bad
    assert _op("read", facts, path=path)["code"] == "bad_path"


def test_a_path_in_no_row_is_refused(tree):
    _ws, _o, outside, _s, facts = tree
    assert _op("read", facts, path=str(outside / "secret.md"))["code"] == "not_a_session_folder"
    assert _op("read", facts, path="/etc/hostname")["code"] == "not_a_session_folder"


def test_a_name_that_only_starts_like_a_row_is_no_row(tree):
    ws, _o, _x, _s, facts = tree
    twin = Path(f"{ws}x")
    twin.mkdir()
    (twin / "t.md").write_text("twin\n")
    assert _op("read", facts, path=str(twin / "t.md"))["code"] == "not_a_session_folder"


def test_a_refused_name_is_refused_by_name(tree):
    ws, _o, _x, _s, facts = tree
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("x\n")
    assert _op("read", facts, path=str(ws / ".git" / "config")) == {
        "ok": False, "code": "refused_name", "name": ".git"}


def test_a_link_is_never_opened_and_its_landing_is_named(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "link.md").symlink_to(ws / "sub" / "b.md")
    answer = _op("read", facts, path=str(ws / "link.md"))
    assert answer == {"ok": False, "code": "lands", "landed": str(ws / "sub" / "b.md")}
    assert _op("read", facts, path=answer["landed"], strict=True)["text"] == "gamma\n"


def test_a_folder_link_on_the_way_lands_too(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "alias").symlink_to(ws / "sub")
    assert _op("read", facts, path=str(ws / "alias" / "b.md")) == {
        "ok": False, "code": "lands", "landed": str(ws / "sub" / "b.md")}


def test_a_link_out_of_its_row_is_refused(tree):
    ws, other, outside, _s, facts = tree
    (ws / "to-other").symlink_to(other / "o.md")
    (ws / "to-outside").symlink_to(outside / "secret.md")
    assert _op("read", facts, path=str(ws / "to-other"))["code"] == "leaves"
    assert _op("read", facts, path=str(ws / "to-outside"))["code"] == "leaves"


def test_a_link_is_followed_only_inside_the_row_it_starts_in(tree):
    ws, _o, _x, _s, facts = tree
    nested = dict(facts, rows=[*facts["rows"], str(ws / "sub")])
    (ws / "sub" / "up.md").symlink_to(ws / "a.md")
    (ws / "down.md").symlink_to(ws / "sub" / "b.md")
    assert _op("read", nested, path=str(ws / "sub" / "up.md"))["code"] == "leaves"
    assert _op("read", nested, path=str(ws / "down.md"))["code"] == "lands"


def test_a_link_onto_a_refused_name_is_refused(tree):
    ws, _o, _x, _s, facts = tree
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").write_text("{}\n")
    (ws / "s.json").symlink_to(ws / ".claude" / "settings.json")
    assert _op("read", facts, path=str(ws / "s.json"))["code"] == "elsewhere"


def test_a_dangling_link_reads_as_missing(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "dangling").symlink_to(ws / "nowhere.md")
    answer = _op("read", facts, path=str(ws / "dangling"))
    assert answer["code"] == "lands"
    assert _op("read", facts, path=answer["landed"], strict=True)["code"] == "missing"


@pytest.mark.parametrize("path", ["{ws}/link.md", "{ws}/alias/b.md"])
def test_a_strict_open_follows_no_link(tree, path):
    ws, _o, _x, _s, facts = tree
    (ws / "link.md").symlink_to(ws / "a.md")
    (ws / "alias").symlink_to(ws / "sub")
    assert _op("read", facts, path=path.format(ws=ws), strict=True)["code"] == "link"


def test_a_strict_open_refuses_a_link_out_of_the_row(tree):
    """What ``strict`` opens was judged by the platform. A link put there
    after the judgement is refused at the open, wherever it points."""
    ws, _o, outside, _s, facts = tree
    (ws / "swapped.md").symlink_to(outside / "secret.md")
    assert _op("read", facts, path=str(ws / "swapped.md"), strict=True)["code"] == "link"


# -- glob ---------------------------------------------------------------------

def test_glob_lists_matching_files_sorted(tree):
    ws, _o, _x, _s, facts = tree
    assert _op("glob", facts, path=str(ws), pattern="**/*.md") == {"ok": True, "rels": ["a.md", "sub/b.md"]}
    assert _op("glob", facts, path=str(ws), pattern="*.md")["rels"] == ["a.md"]
    assert _op("glob", facts, path=str(ws), pattern="sub/*")["rels"] == ["sub/b.md"]


def test_glob_enters_no_link_and_no_skipped_name(tree):
    ws, other, _x, _s, facts = tree
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "n.md").write_text("n\n")
    (ws / "alias").symlink_to(ws / "sub")
    (ws / "far").symlink_to(other)
    assert _op("glob", facts, path=str(ws), pattern="**/*.md")["rels"] == ["a.md", "sub/b.md"]


def test_glob_lists_a_link_only_where_it_lands_on_a_file_of_its_row(tree):
    ws, other, _x, _s, facts = tree
    (ws / "good.md").symlink_to(ws / "sub" / "b.md")
    (ws / "to-other.md").symlink_to(other / "o.md")
    (ws / "dangling.md").symlink_to(ws / "nowhere.md")
    (ws / "folder.md").symlink_to(ws / "sub")
    os.mkfifo(ws / "pipe.md")
    assert _op("glob", facts, path=str(ws), pattern="*.md")["rels"] == ["a.md", "good.md"]


def test_glob_stops_at_the_cap(tree):
    ws, _o, _x, _s, facts = tree
    for i in range(9):
        (ws / f"n{i}.txt").write_text("x")
    assert len(_op("glob", facts, path=str(ws), pattern="*.txt")["rels"]) == 5


@pytest.mark.parametrize("name", ["gone", "a.md"])
def test_glob_of_what_is_no_folder(tree, name):
    ws, _o, _x, _s, facts = tree
    assert _op("glob", facts, path=f"{ws}/{name}", pattern="*")["code"] == "not_a_folder"


@pytest.mark.parametrize("pattern", ["", "  ", "/abs/*.md", "../*.md", "a/../b", None])
def test_glob_refuses_a_pattern_that_is_not_relative(tree, pattern):
    ws, _o, _x, _s, facts = tree
    assert _op("glob", facts, path=str(ws), pattern=pattern)["code"] == "bad_pattern"


def test_glob_of_a_folder_link_lands_first(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "alias").symlink_to(ws / "sub")
    answer = _op("glob", facts, path=str(ws / "alias"), pattern="*")
    assert answer == {"ok": False, "code": "lands", "landed": str(ws / "sub")}
    assert _op("glob", facts, path=answer["landed"], pattern="*", strict=True)["rels"] == ["b.md"]


# -- the search's walk: one folder open a level, and never a silent gap ------------

_FD = "/proc/self/fd"


@contextlib.contextmanager
def _few_descriptors(headroom):
    """The process may open ``headroom`` more files at most while this
    holds. Yields how many it may open."""
    import resource
    numbers = [int(n) for n in os.listdir(_FD)]
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = max(numbers) + 1 + headroom
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
    try:
        yield limit - len(numbers)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def _search_facts(root):
    return {"rows": [str(root)], "refused": [], "glob_skip": [], "glob_max": 200}


@pytest.mark.skipif(not os.path.isdir(_FD), reason="reads the process's open files from /proc")
def test_a_search_holds_one_folder_a_level(tmp_path):
    """A folder with more subfolders than the process may open files: each
    is entered in its turn, and the answer is whole."""
    root = Path(os.path.realpath(tmp_path)) / "ws"
    with _few_descriptors(48) as free:
        wide = free + 150
    for i in range(wide):
        (root / f"d{i:04d}").mkdir(parents=True)
    (root / "d0000" / "target.md").write_text("x")
    (root / f"d{wide - 1:04d}" / "target.md").write_text("x")
    before = len(os.listdir(_FD))
    with _few_descriptors(48):
        answer = _op("glob", _search_facts(root), path=str(root), pattern="**/target.md")
    assert answer == {"ok": True, "rels": ["d0000/target.md", f"d{wide - 1:04d}/target.md"]}
    assert len(os.listdir(_FD)) == before                # every folder it opened is closed again


@pytest.mark.skipif(not os.path.isdir(_FD), reason="reads the process's open files from /proc")
def test_a_search_deeper_than_it_may_open_says_what_it_left_out(tmp_path):
    """One folder is held a level, so a nest deeper than the process may
    open files ends in a folder that cannot be opened. The answer keeps what
    was found and counts that folder: it never reads as whole."""
    root = Path(os.path.realpath(tmp_path)) / "ws"
    with _few_descriptors(40) as free:
        levels = free + 50
    deep = root
    for i in range(levels):
        deep = deep / f"n{i:03d}"
    deep.mkdir(parents=True)
    (root / "n000" / "near.md").write_text("x")
    (deep / "far.md").write_text("x")
    before = len(os.listdir(_FD))
    with _few_descriptors(40):
        answer = _op("glob", _search_facts(root), path=str(root), pattern="**/*.md")
    assert answer == {"ok": True, "rels": ["n000/near.md"], "unread": 1}
    assert len(os.listdir(_FD)) == before
    whole = _op("glob", _search_facts(root), path=str(root), pattern="**/*.md")
    far = "/".join(f"n{i:03d}" for i in range(levels)) + "/far.md"
    assert whole == {"ok": True, "rels": sorted([far, "n000/near.md"])}     # with room it is whole and says nothing


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every folder")
def test_a_folder_the_search_may_not_open_is_counted(tree):
    ws, _o, _x, _s, facts = tree
    for name in ("locked", "sub/locked"):
        (ws / name).mkdir()
        (ws / name / "in.md").write_text("x")
        (ws / name).chmod(0)
    try:
        answer = _op("glob", facts, path=str(ws), pattern="**/*.md")
    finally:
        for name in ("locked", "sub/locked"):
            (ws / name).chmod(0o700)
    assert answer == {"ok": True, "rels": ["a.md", "sub/b.md"], "unread": 2}


def test_a_folder_whose_listing_fails_is_counted_once(tree, monkeypatch):
    ws, _o, _x, _s, facts = tree
    (ws / "sub" / "deeper").mkdir()
    (ws / "sub" / "deeper" / "c.md").write_text("x")
    scandir, asked = os.scandir, []

    def failing(fd):
        asked.append(fd)
        if len(asked) == 2:                               # the second folder listed: sub
            raise OSError(errno.EIO, "the disk did not answer")
        return scandir(fd)

    monkeypatch.setattr(fp.os, "scandir", failing)
    answer = _op("glob", facts, path=str(ws), pattern="**/*.md")
    # What sub holds is not searched, nor what lies below it, and the answer says so once.
    assert answer == {"ok": True, "rels": ["a.md"], "unread": 1} and len(asked) == 2


@pytest.mark.parametrize("error, unread", [
    (FileNotFoundError(errno.ENOENT, "gone since the listing"), 0),    # nothing is there to search
    ("link", 0),                                                        # a link now: no link is entered
    (PermissionError(errno.EACCES, "not for this user"), 1),
    (OSError(errno.EMFILE, "too many open files"), 1),
    (NotADirectoryError(errno.ENOTDIR, "a file now"), 1),
])
def test_a_folder_that_cannot_be_entered_is_counted_unless_it_is_gone_or_a_link(tree, monkeypatch, error, unread):
    ws, _o, _x, _s, facts = tree
    step = fp._step

    def stepping(name, flags, dir_fd):
        if name == "sub":
            raise fp._No("link") if error == "link" else error
        return step(name, flags, dir_fd)

    monkeypatch.setattr(fp, "_step", stepping)
    answer = _op("glob", facts, path=str(ws), pattern="**/*.md")
    assert answer == ({"ok": True, "rels": ["a.md"], "unread": unread} if unread else {"ok": True, "rels": ["a.md"]})


def test_a_search_stopped_at_its_cap_closes_what_it_opened(tree):
    ws, _o, _x, _s, facts = tree
    for i in range(4):
        (ws / "sub" / f"deep{i}").mkdir()
        for j in range(3):
            (ws / "sub" / f"deep{i}" / f"f{j}.md").write_text("x")
    before = len(os.listdir(_FD)) if os.path.isdir(_FD) else None
    answer = _op("glob", facts, path=str(ws), pattern="**/*.md")
    assert len(answer["rels"]) == facts["glob_max"] and "unread" not in answer
    if before is not None:
        assert len(os.listdir(_FD)) == before


_PATTERNS = ["*", "**", "**/*", "*.md", "**/*.md", "a/*", "a/**", "a/**/c.md", "**/b/*", "?.md", "[ab].md",
             "[!a].md", "a/b/c.md", "*/*.md", ".*", "**/.*", "a*/c*", "[a-c]/**", "x[", "**/**/c.md"]
_NAMES = ["a.md", "b.md", ".hidden", "a/b.md", "a/b/c.md", "a/.x", "b/c/d.md", "c.md", "a/c.md", "ab/cd", "x["]


@pytest.mark.skipif(sys.version_info < (3, 13), reason="full_match is Python 3.13's")
@pytest.mark.parametrize("pattern", _PATTERNS)
def test_the_matcher_is_python_313s_full_match(pattern):
    import re
    matches = re.compile(fp._glob_regex(pattern)).match
    for name in _NAMES:
        assert bool(matches(name)) == PurePosixPath(name).full_match(pattern), (pattern, name)


# -- skills -------------------------------------------------------------------

def test_skills_lists_folders_that_hold_a_skill(tree):
    ws, _o, _x, skills, facts = tree
    for name in ("beta", "alpha", ".hidden", "empty"):
        (skills / name).mkdir()
    for name in ("beta", "alpha", ".hidden"):
        (skills / name / "SKILL.md").write_text("body\n")
    (skills / "linked").symlink_to(skills / "alpha")
    (skills / "linkfile").mkdir()
    (skills / "linkfile" / "SKILL.md").symlink_to(ws / "a.md")
    assert _op("skills", facts) == {"ok": True, "names": ["alpha", "beta"]}


def test_skills_of_a_session_with_no_folder(tree):
    _ws, _o, _x, _s, facts = tree
    assert _op("skills", dict(facts, skills="")) == {"ok": True, "names": []}
    assert _op("skills", dict(facts, skills="/nowhere/skills")) == {"ok": True, "names": []}


def test_skill_reads_one_skill(tree):
    _ws, _o, _x, skills, facts = tree
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text("the body\n")
    assert _op("skill", facts, name="alpha") == {"ok": True, "text": "the body\n"}


def test_skill_refuses_what_is_no_skill(tree):
    ws, _o, _x, skills, facts = tree
    (skills / "big").mkdir()
    (skills / "big" / "SKILL.md").write_text("x" * 100)
    (skills / "linked").mkdir()
    (skills / "linked" / "SKILL.md").symlink_to(ws / "a.md")
    assert _op("skill", facts, name="nope")["code"] == "missing"
    assert _op("skill", facts, name="big")["code"] == "too_large"
    assert _op("skill", facts, name="linked")["code"] == "link"
    # A name is one folder's name. Each of these has a file where a name
    # read as a path would land, so only the rule on the name refuses it.
    for folder in (skills.parent / "up", skills / "a" / "b"):
        folder.mkdir(parents=True)
    for place in (skills.parent / "up", skills / "a" / "b", skills, skills.parent):
        (place / "SKILL.md").write_text("no skill\n")
    for bad in ("../up", "a/b", "", ".", "..", None):
        assert _op("skill", facts, name=bad)["code"] == "missing", bad


def test_skill_takes_no_path(tree):
    ws, _o, _x, _s, facts = tree
    assert _op("skill", facts, name="x", path=str(ws / "a.md"))["code"] == "missing"


# -- the general loop ---------------------------------------------------------

def _reply(operations, facts, method, params=None, request_id=1):
    return rt._handle(operations, facts, {"jsonrpc": "2.0", "id": request_id, "method": method,
                                          "params": params or {}})


def test_the_loop_answers_the_protocols_four_methods():
    operations = {"b": lambda facts, args: {"ok": True, "seen": [facts, args]}, "a": lambda facts, args: 7}
    assert _reply(operations, {}, "initialize", {"protocolVersion": "2025-06-18"})["result"][
        "protocolVersion"] == "2025-06-18"
    assert _reply(operations, {}, "ping")["result"] == {}
    assert [t["name"] for t in _reply(operations, {}, "tools/list")["result"]["tools"]] == ["a", "b"]
    called = _reply(operations, {"f": 1}, "tools/call", {"name": "b", "arguments": {"x": 2}})["result"]
    assert json.loads(called["content"][0]["text"]) == {"ok": True, "seen": [{"f": 1}, {"x": 2}]}
    assert _reply(operations, {}, "nope")["error"]["code"] == -32601
    assert rt._handle(operations, {}, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_operation_that_fails_is_an_answer():
    def boom(_facts, _args):
        raise RuntimeError("no")
    operations = {"boom": boom, "odd": lambda facts, args: "text"}
    for name, code in (("boom", "failed"), ("odd", "failed"), ("gone", "unknown_operation"), (7, "unknown_operation")):
        result = _reply(operations, {}, "tools/call", {"name": name})["result"]
        assert json.loads(result["content"][0]["text"])["code"] == code, name


def test_the_joined_text_is_python_310_and_the_standard_library_only():
    text = _joined()
    tree = ast.parse(text, feature_version=(3, 10))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    # No ``from __future__`` line either: the program is the second half of one text.
    assert imported == {"contextlib", "errno", "json", "os", "posixpath", "re", "stat", "sys"}
    assert len(text.encode()) < 48 * 1024


def test_the_program_runs_as_a_process_from_its_text_in_the_environment(tree):
    ws, _o, _x, _s, facts = tree
    start = 'import os,sys;exec(compile(os.environ.pop("OTO_PROGRAM"),"<oto-program>","exec"))'
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", start, json.dumps(facts)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), "OTO_PROGRAM": _joined()})

    def ask(request_id, method, params):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                                     "params": params}).encode() + b"\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        assert ask(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]["capabilities"] == {"tools": {}}
        names = [tool["name"] for tool in ask(2, "tools/list", {})["result"]["tools"]]
        assert names == ["edit", "glob", "read", "skill", "skills", "stat", "write"]
        proc.stdin.write(b"this is no json\n")
        answer = ask(3, "tools/call", {"name": "read", "arguments": {"path": str(ws / "a.md")}})
        assert json.loads(answer["result"]["content"][0]["text"])["text"] == "alpha\nbeta\n"
        greek = ws / "greek.md"
        greek.write_text("καλημέρα\n", encoding="utf-8")
        answer = ask(4, "tools/call", {"name": "read", "arguments": {"path": str(greek)}})
        assert json.loads(answer["result"]["content"][0]["text"])["text"] == "καλημέρα\n"
        # A write, an edit of it and the delete's look, as the platform sends them.
        new = ws / "made" / "new.md"
        answer = ask(5, "tools/call", {"name": "write", "arguments": {"path": str(new), "content": "γεια\nσου\n"}})
        assert json.loads(answer["result"]["content"][0]["text"]) == {"ok": True, "bytes": 16, "created": True}
        answer = ask(6, "tools/call", {"name": "edit", "arguments": {"path": str(new), "old": "σου", "new": "σας"}})
        assert json.loads(answer["result"]["content"][0]["text"]) == {"ok": True, "count": 1}
        assert new.read_text(encoding="utf-8") == "γεια\nσας\n"
        answer = ask(7, "tools/call", {"name": "stat", "arguments": {"path": str(new)}})
        status = new.lstat()
        assert json.loads(answer["result"]["content"][0]["text"]) == {
            "ok": True, "size": status.st_size, "inode": status.st_ino, "changed": status.st_ctime_ns}
    finally:
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0


# -- write --------------------------------------------------------------------

_ROOT = os.geteuid() == 0        # the permission bits say nothing to root


def _partials(folder):
    return sorted(name for name in os.listdir(folder) if name.endswith(".partial"))


def test_write_makes_a_new_file_and_the_folders_on_its_way(tree):
    ws, _o, _x, _s, facts = tree
    target = ws / "made" / "deep" / "new.md"
    old = os.umask(0o027)
    try:
        answer = _op("write", facts, path=str(target), content="one\n")
    finally:
        os.umask(old)
    assert answer == {"ok": True, "bytes": 4, "created": True}
    assert target.read_text() == "one\n" and _partials(target.parent) == []
    # A new file and a new folder take the umask's bits, as any file of the session does.
    assert target.stat().st_mode & 0o777 == 0o640 and target.parent.stat().st_mode & 0o777 == 0o750


def test_write_moves_a_whole_new_file_over_the_name_and_keeps_its_bits(tree):
    ws, _o, _x, _s, facts = tree
    target = ws / "a.md"
    target.chmod(0o750)
    before = target.stat().st_ino
    reader = open(target)                # a reader that has the old file open
    try:
        answer = _op("write", facts, path=str(target), content="new\n")
        assert answer == {"ok": True, "bytes": 4, "created": False}
        assert target.read_text() == "new\n" and target.stat().st_mode & 0o777 == 0o750
        assert target.stat().st_ino != before        # a new file, never the old one written in place
        assert reader.read() == "alpha\nbeta\n"     # the reader keeps the old file, whole
    finally:
        reader.close()
    assert _partials(ws) == []


@pytest.mark.parametrize("name, code", [("sub", "folder"), ("ln.md", "link_at_name"), ("dangling.md", "link_at_name")])
def test_write_refuses_a_folder_and_a_link_at_the_name(tree, name, code):
    ws, _o, outside, _s, facts = tree
    (ws / "ln.md").symlink_to(outside / "secret.md")
    (ws / "dangling.md").symlink_to(outside / "not-there.md")
    assert _op("write", facts, path=f"{ws}/{name}", content="x")["code"] == code
    assert (outside / "secret.md").read_text() == "not a row\n" and not (outside / "not-there.md").exists()
    assert _partials(ws) == []


def test_write_never_waits_on_a_pipe_at_the_name(tree):
    ws, _o, _x, _s, facts = tree
    os.mkfifo(ws / "pipe.md")
    began = time.monotonic()
    assert _op("write", facts, path=str(ws / "pipe.md"), content="x")["code"] == "special"
    # A pipe that somebody reads opens for writing: it is still no regular file.
    reader = os.open(ws / "pipe.md", os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert _op("write", facts, path=str(ws / "pipe.md"), content="x")["code"] == "special"
        assert _op("edit", facts, path=str(ws / "pipe.md"), old="a", new="b")["code"] == "special"
        assert os.read(reader, 16) == b""
    finally:
        os.close(reader)
    assert time.monotonic() - began < 2 and _partials(ws) == []


def test_a_temp_name_that_is_taken_is_never_written_through(tree, monkeypatch):
    ws, _o, outside, _s, facts = tree
    monkeypatch.setattr(fp, "_temp_name", lambda name: ".taken.partial")
    (ws / ".taken.partial").symlink_to(outside / "secret.md")
    answer = _op("write", facts, path=str(ws / "a.md"), content="new\n")
    assert answer["ok"] is False and (outside / "secret.md").read_text() == "not a row\n"
    assert (ws / "a.md").read_text() == "alpha\nbeta\n"
    (ws / ".taken.partial").unlink()
    (ws / ".taken.partial").write_text("somebody's file\n")
    assert _op("write", facts, path=str(ws / "a.md"), content="new\n")["ok"] is False
    assert (ws / "a.md").read_text() == "alpha\nbeta\n"


def test_only_a_write_makes_a_folder(tree):
    ws, _o, _x, _s, facts = tree
    gone = str(ws / "not-there" / "deep" / "x.md")
    assert _op("read", facts, path=gone)["code"] == "missing"
    assert _op("edit", facts, path=gone, old="a", new="b")["code"] == "missing"
    assert _op("stat", facts, path=gone)["code"] == "missing"
    assert _op("glob", facts, path=str(ws / "not-there"), pattern="*")["code"] == "not_a_folder"
    assert not (ws / "not-there").exists()


def test_write_follows_no_link_on_the_way_and_makes_no_folder_through_one(tree):
    ws, other, outside, _s, facts = tree
    (ws / "out").symlink_to(outside)
    (ws / "row").symlink_to(other)
    for path in (ws / "out" / "x.md", ws / "out" / "made" / "x.md", ws / "row" / "x.md"):
        assert _op("write", facts, path=str(path), content="x")["code"] == "link"
    assert sorted(os.listdir(outside)) == ["secret.md"] and sorted(os.listdir(other)) == ["o.md"]


def test_a_link_put_where_a_folder_was_just_made_is_refused(tree, monkeypatch):
    ws, _o, outside, _s, facts = tree

    def swapped(name, mode, *, dir_fd):
        os.symlink(outside, name, dir_fd=dir_fd)          # another writer wins the name with a link

    monkeypatch.setattr(fp.os, "mkdir", swapped)
    assert _op("write", facts, path=str(ws / "made" / "x.md"), content="x")["code"] == "link"
    assert sorted(os.listdir(outside)) == ["secret.md"]


def test_write_is_refused_outside_every_row_and_under_a_refused_name(tree):
    ws, _o, outside, _s, facts = tree
    assert _op("write", facts, path=str(outside / "x.md"), content="x")["code"] == "not_a_session_folder"
    assert _op("write", facts, path="/tmp/oto-direct-files-write-x.md", content="x")["code"] == "not_a_session_folder"
    answer = _op("write", facts, path=str(ws / ".claude" / "settings.json"), content="x")
    assert answer["code"] == "refused_name" and answer["name"] == ".claude"
    assert sorted(os.listdir(outside)) == ["secret.md"] and not (ws / ".claude").exists()
    assert not os.path.exists("/tmp/oto-direct-files-write-x.md")


def test_edit_and_the_deletes_look_are_refused_outside_every_row_and_under_a_refused_name(tree):
    """The row check is each operation's own line: the kernel shows this
    test's process every one of these files, so only that line refuses."""
    ws, _o, outside, _s, facts = tree
    secret = outside / "secret.md"
    (ws / ".git").mkdir()
    held = ws / ".git" / "config"
    held.write_text("alpha = 1\n")
    before = secret.read_bytes()
    assert b"row" in before
    assert _op("edit", facts, path=str(secret), old="row", new="ROW") == {
        "ok": False, "code": "not_a_session_folder"}
    assert _op("stat", facts, path=str(secret)) == {"ok": False, "code": "not_a_session_folder"}
    assert _op("edit", facts, path=str(held), old="alpha", new="beta") == {
        "ok": False, "code": "refused_name", "name": ".git"}
    assert _op("stat", facts, path=str(held)) == {"ok": False, "code": "refused_name", "name": ".git"}
    assert secret.read_bytes() == before and held.read_text() == "alpha = 1\n"
    assert sorted(os.listdir(outside)) == ["secret.md"] and sorted(os.listdir(ws / ".git")) == ["config"]
    # The same calls inside a row are answered: the refusals above are the row check's.
    (ws / "in.md").write_text("alpha\n")
    assert _op("edit", facts, path=str(ws / "in.md"), old="alpha", new="beta") == {"ok": True, "count": 1}
    assert _op("stat", facts, path=str(ws / "in.md"))["ok"] is True


def test_write_holds_the_cap_in_bytes(tree):
    ws, _o, _x, _s, facts = tree
    assert _op("write", facts, path=str(ws / "full.md"), content="é" * 32)["bytes"] == 64
    assert _op("write", facts, path=str(ws / "over.md"), content="é" * 32 + "x")["code"] == "too_large"
    assert not (ws / "over.md").exists()
    assert _op("write", dict(facts, write_max=None), path=str(ws / "none.md"), content="x")["code"] == "too_large"


@pytest.mark.parametrize("content", ["a\ud800b", 7, None, b"x"])
def test_write_refuses_what_is_no_text(tree, content):
    ws, _o, _x, _s, facts = tree
    assert _op("write", facts, path=str(ws / "x.md"), content=content)["code"] == "bad_content"
    assert not (ws / "x.md").exists()


def test_write_names_a_file_that_stands_where_a_folder_is_needed(tree):
    ws, _o, _x, _s, facts = tree
    answer = _op("write", facts, path=str(ws / "a.md" / "x.md"), content="x")
    assert answer["code"] == "os" and answer["detail"] == os.strerror(errno.ENOTDIR)


@pytest.mark.skipif(_ROOT, reason="the permission bits say nothing to root")
def test_a_file_whose_own_bits_refuse_a_write_is_not_replaced(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "a.md").chmod(0o444)
    assert _op("write", facts, path=str(ws / "a.md"), content="x")["code"] == "denied"
    assert _op("edit", facts, path=str(ws / "a.md"), old="alpha", new="x")["code"] == "denied"
    assert (ws / "a.md").read_text() == "alpha\nbeta\n" and _partials(ws) == []


@pytest.mark.skipif(_ROOT, reason="the permission bits say nothing to root")
def test_a_folder_that_may_not_be_written_refuses_a_new_file(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "sub").chmod(0o555)
    try:
        assert _op("write", facts, path=str(ws / "sub" / "new.md"), content="x")["code"] == "denied"
        assert _op("write", facts, path=str(ws / "sub" / "made" / "new.md"), content="x")["code"] == "denied"
    finally:
        (ws / "sub").chmod(0o755)
    assert sorted(os.listdir(ws / "sub")) == ["b.md"]


@pytest.mark.skipif(_ROOT, reason="the permission bits say nothing to root")
def test_a_folder_on_the_way_needs_search_alone(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "sub").chmod(0o311)            # search and write, no read: it cannot be listed
    try:
        assert _op("write", facts, path=str(ws / "sub" / "deep" / "new.md"), content="x")["ok"] is True
        assert _op("read", facts, path=str(ws / "sub" / "b.md"))["text"] == "gamma\n"
        assert _op("stat", facts, path=str(ws / "sub" / "b.md"))["ok"] is True
    finally:
        (ws / "sub").chmod(0o755)


def test_a_write_that_fails_before_the_move_leaves_the_old_file_and_no_temp_file(tree, monkeypatch):
    ws, _o, _x, _s, facts = tree

    def no_room(*args, **kwargs):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(fp.os, "rename", no_room)
    assert _op("write", facts, path=str(ws / "a.md"), content="new\n")["code"] == "no_space"
    assert _op("write", facts, path=str(ws / "fresh.md"), content="new\n")["code"] == "no_space"
    assert (ws / "a.md").read_text() == "alpha\nbeta\n" and not (ws / "fresh.md").exists()
    assert _partials(ws) == []


def test_a_program_ended_in_the_middle_of_a_write_leaves_the_old_file_whole(tree):
    """The machine ends a program whose call passed its cap. Ended between
    the temp file and the move, the name still holds the old file, and what
    stays beside it is a ``.partial`` file, which nothing syncs."""
    ws, _o, _x, _s, facts = tree
    ended = "import os\nos.rename = lambda *a, **k: os.kill(os.getpid(), 9)\n"
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", ended + _joined(), json.dumps(facts)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, env={"PATH": os.environ.get("PATH", "")})
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "write", "arguments": {"path": str(ws / "a.md"), "content": "new\n"}}}
    out, _err = proc.communicate(json.dumps(request).encode() + b"\n", timeout=10)
    assert proc.returncode == -9 and out == b""             # it ended before it answered
    assert (ws / "a.md").read_text() == "alpha\nbeta\n"
    left = _partials(ws)
    assert len(left) == 1 and (ws / left[0]).read_text() == "new\n"


def test_the_temp_file_has_the_platforms_own_shape():
    import re
    assert re.fullmatch(r"\.a\.md\.[0-9a-f]{12}\.partial", fp._temp_name("a.md"))
    assert fp._temp_name("a.md") != fp._temp_name("a.md")
    long = fp._temp_name("n" * 300)
    assert re.fullmatch(r"\.n{200}\.[0-9a-f]{12}\.partial", long) and len(os.fsencode(long)) <= 255
    # A name cut inside a character keeps exactly its first 200 bytes.
    greek = "α" * 150 + "b"
    assert os.fsencode(fp._temp_name(greek))[1:201] == os.fsencode(greek)[:200]


# -- edit ---------------------------------------------------------------------

def test_edit_replaces_the_one_place_and_keeps_line_ends_and_bits(tree):
    ws, _o, _x, _s, facts = tree
    target = ws / "dos.md"
    target.write_bytes(b"one\r\ntwo\r\n")
    target.chmod(0o750)
    assert _op("edit", facts, path=str(target), old="two", new="2") == {"ok": True, "count": 1}
    assert target.read_bytes() == b"one\r\n2\r\n" and target.stat().st_mode & 0o777 == 0o750
    assert _partials(ws) == []


def test_edit_judges_how_often_the_text_is_there(tree):
    ws, _o, _x, _s, facts = tree
    target = ws / "a.md"
    target.write_text("x x x\n")
    assert _op("edit", facts, path=str(target), old="y", new="z")["code"] == "not_found_text"
    many = _op("edit", facts, path=str(target), old="x", new="z")
    assert many["code"] == "many" and many["count"] == 3 and target.read_text() == "x x x\n"
    assert _op("edit", facts, path=str(target), old="x", new="z", replace_all=True) == {"ok": True, "count": 3}
    assert target.read_text() == "z z z\n"


@pytest.mark.parametrize("name, code", [
    ("gone.md", "missing"), ("sub", "folder"), ("ln.md", "link_at_name"), ("b.bin", "binary"),
    ("latin.md", "not_text"), ("big.md", "too_large"),
])
def test_edit_refuses_what_it_cannot_edit(tree, name, code):
    ws, _o, _x, _s, facts = tree
    (ws / "ln.md").symlink_to(ws / "a.md")
    (ws / "b.bin").write_bytes(b"ab\x00cd")
    (ws / "latin.md").write_bytes(b"caf\xe9\n")
    (ws / "big.md").write_text("x" * 65)
    assert _op("edit", facts, path=f"{ws}/{name}", old="a", new="b")["code"] == code
    assert (ws / "a.md").read_text() == "alpha\nbeta\n" and _partials(ws) == []


@pytest.mark.parametrize("old, new", [("", "x"), (None, "x"), ("a", None), ("a\ud800", "x")])
def test_edit_refuses_strings_that_are_no_text(tree, old, new):
    ws, _o, _x, _s, facts = tree
    assert _op("edit", facts, path=str(ws / "a.md"), old=old, new=new)["code"] == "bad_content"


def test_an_edit_whose_result_is_over_the_cap_writes_nothing(tree):
    ws, _o, _x, _s, facts = tree
    target = ws / "a.md"
    target.write_text("x" * 60)
    assert _op("edit", facts, path=str(target), old="x", new="xx", replace_all=True)["code"] == "grows"
    assert _op("edit", facts, path=str(target), old="x" * 60, new="x" * 65)["code"] == "grows"
    assert target.read_text() == "x" * 60 and _partials(ws) == []
    assert _op("edit", facts, path=str(target), old="x" * 60, new="y" * 64) == {"ok": True, "count": 1}


@pytest.mark.parametrize("way", ["in place", "in place, the same size", "moved over", "removed"])
def test_an_edit_of_a_file_that_changed_meanwhile_writes_nothing(tree, monkeypatch, way):
    ws, _o, _x, _s, facts = tree
    target = ws / "a.md"
    read_all = fp._read_all

    def and_then_another_writer(fd, cap):
        data = read_all(fd, cap)
        if way == "in place":
            with open(target, "a") as other:
                other.write("theirs\n")
        elif way == "in place, the same size":
            time.sleep(0.05)                # a change time moves in steps of the kernel's clock tick
            with open(target, "r+") as other:
                other.write("ALPHA")
        elif way == "moved over":
            (ws / "theirs.md").write_text("theirs\n")
            os.replace(ws / "theirs.md", target)
        else:
            target.unlink()
        return data

    monkeypatch.setattr(fp, "_read_all", and_then_another_writer)
    assert _op("edit", facts, path=str(target), old="alpha", new="mine")["code"] == "changed"
    theirs = {"in place": "alpha\nbeta\ntheirs\n", "in place, the same size": "ALPHA\nbeta\n",
              "moved over": "theirs\n"}.get(way)
    assert (target.read_text() if theirs else not target.exists()) == (theirs or True)
    assert _partials(ws) == []


# -- stat: the delete's look --------------------------------------------------

def test_stat_answers_what_tells_the_file_from_every_other(tree):
    ws, _o, _x, _s, facts = tree
    status = (ws / "a.md").lstat()
    assert _op("stat", facts, path=str(ws / "a.md")) == {
        "ok": True, "size": status.st_size, "inode": status.st_ino, "changed": status.st_ctime_ns}
    (ws / "a.md").write_text("alpha\nbeta\nmore\n")
    again = _op("stat", facts, path=str(ws / "a.md"))
    assert (again["size"], again["changed"]) != (status.st_size, status.st_ctime_ns)


@pytest.mark.parametrize("name, code", [("gone.md", "missing"), ("sub", "folder"), ("ln.md", "special")])
def test_stat_answers_only_for_a_regular_file(tree, name, code):
    ws, _o, _x, _s, facts = tree
    (ws / "ln.md").symlink_to(ws / "a.md")           # a link at the name is never followed
    assert _op("stat", facts, path=f"{ws}/{name}")["code"] == code
    os.mkfifo(ws / "pipe")
    assert _op("stat", facts, path=str(ws / "pipe"))["code"] == "special"


def test_stat_names_where_a_folder_link_lands_and_keeps_the_last_name(tree):
    ws, other, outside, _s, facts = tree
    (ws / "alias").symlink_to("sub")
    (ws / "sub" / "ln.md").symlink_to(ws / "a.md")
    (ws / "out").symlink_to(outside)
    (ws / "row").symlink_to(other)
    (ws / ".git").mkdir()
    (ws / "state").symlink_to(".git")
    assert _op("stat", facts, path=str(ws / "alias" / "b.md")) == {
        "ok": False, "code": "lands", "landed": str(ws / "sub" / "b.md")}
    # The last name is kept as given: a link there is named, never followed.
    assert _op("stat", facts, path=str(ws / "alias" / "ln.md"))["landed"] == str(ws / "sub" / "ln.md")
    assert _op("stat", facts, path=str(ws / "sub" / "ln.md"), strict=True)["code"] == "special"
    assert _op("stat", facts, path=str(ws / "out" / "secret.md"))["code"] == "leaves"
    assert _op("stat", facts, path=str(ws / "row" / "o.md"))["code"] == "leaves"
    assert _op("stat", facts, path=str(ws / "state" / "config"))["code"] == "elsewhere"
    # Asked again for a judged place, no link is followed at any part.
    assert _op("stat", facts, path=str(ws / "alias" / "b.md"), strict=True)["code"] == "link"
    assert _op("stat", facts, path=str(ws / "sub" / "b.md"), strict=True)["ok"] is True


@pytest.mark.skipif(_ROOT, reason="the permission bits say nothing to root")
def test_stat_refuses_in_a_folder_the_session_cannot_write(tree):
    ws, _o, _x, _s, facts = tree
    (ws / "sub").chmod(0o555)
    try:
        assert _op("stat", facts, path=str(ws / "sub" / "b.md"))["code"] == "read_only"
    finally:
        (ws / "sub").chmod(0o755)
    assert _op("stat", facts, path=str(ws / "sub" / "b.md"))["ok"] is True
