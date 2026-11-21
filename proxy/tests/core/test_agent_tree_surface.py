"""Every raw file call in the server and the satellite: a listed site with its reason.

The pattern this guards: the server's own code opened, wrote or removed a
file by its path, in a folder that a session, a person, a tool or a paired
computer can write. A link or a pipe planted at that path sends the call
wherever the server's account reaches, or holds it. The safe file helpers
(``services/infra/safe_fs.py``, and the satellite's twin
``satellite/host/safe_fs.py``) do each of those jobs beneath a root, with no
link followed at any part and no pipe waited on. So a raw call is either on
the server's own data, or a judged site with the reason it is sound there.

One pass over every ``.py`` under ``proxy/`` and ``audio/``, the package the
proxy imports into its own process (tests, docs and the venv left out), and
one over the repository's ``satellite/`` (its tests and vendored copies left
out) find each raw file call: a module's file function named anywhere
(called, handed on as in ``to_thread(shutil.rmtree, path)``, or kept under
another name), and a path's method called or handed on. A name is read
through the module's imports and the tree's own modules
(``tests/_import_names.py``): ``import shutil as sh`` and ``from os import
unlink as rm`` are the same calls. ``agent_tree_sites.py`` lists every site
by file and function, with the calls in it, a reason and why. The list is
exact in both directions, per call: a new call fails until it is listed, also
a second one in a function already listed, and a listed call that left the
code fails until its row changes. Every row says why; a row of the agents
tree names the audit row it was judged in, ``new``, or ``to judge``.

What it does not count: a query (``stat``, ``exists``, ``realpath``) returns
facts and no content, and the open that follows a check is itself a site; a
call of ``safe_fs`` (never listed); a call of one path that is the safe
helpers' ``fd_path(...)`` or ``io.BytesIO(...)``, read through the imports.
An archive's ``extractall`` and ``extract`` are sites whatever the archive was
opened from. The two owners, ``safe_fs`` and its twin, are left out: their raw
calls work on the handles they hold, and their own tests judge them.

What stays invisible to it, one line each:

- a path handed in from another module: the row's reason is the only judge of
  where it leads;
- a listed call whose path changes: a row is keyed by function and calls,
  never by argument, so it keeps passing;
- a wrapper of a raw call under a name of its own that is not in ``WRAPPERS``;
- code run by name (``getattr``, ``importlib``), a star import, a function or
  a path's method kept in a variable;
- a module rebound to a plain name (``shutil = sh``): its calls vanish, and
  the failure then asks to take them out of their rows;
- a lower-case attribute of a library module taken for a module
  (``platformdirs.user_data_path.read_text()``);
- a socket bound by its path, an image object saved to a path
  (``Image.open(BytesIO(b)).save(dest)``), an archive's ``add`` of a path;
- the root a ``safe_fs`` call is handed: the third list holds it
  (``test_safe_fs_roots.py``);
- a library's opener not in ``_MODULE_CALLS``, and a query whose answer a
  later call trusts.

Left out, and why: ``phone/`` is a service of its own, in its own container
with no agents folder mounted; the first-party tools under ``mcps/custom/``
run inside a session's sandbox with its rights, except the two that mount the
whole agents folder in their own containers (file-tools and video-tools),
which no pass reads yet.

A behaviour test drives the file audit's paths over links and pipes as well.
"""

from __future__ import annotations

import ast
import collections
import functools
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from tests import _import_names as names
from tests._paths import REPO_ROOT
from tests.core import agent_tree_sites
from tests.core.agent_tree_sites import (
    AGENTS,
    PROXY_SITES,
    REASONS,
    SATELLITE_SITES,
    TO_JUDGE,
    Site,
)

PROXY = Path(REPO_ROOT) / "proxy"
#: The package the proxy imports into its own process, read with the proxy's
#: pass and listed under ``../audio/``.
AUDIO = Path(REPO_ROOT) / "audio"
SATELLITE = Path(REPO_ROOT) / "satellite"
# ``build``: a plain ``pip install ./audio`` leaves a copy of the package under ``audio/build/lib``.
_PROXY_EXCLUDED = {"tests", "docs", "venv", ".venv", "node_modules", "__pycache__", "build"}
_SATELLITE_EXCLUDED = {"tests", "_vendored", "__pycache__"}

#: The owners of the safe opens, left out of the pass: their raw calls work
#: on the handles they hold.
PROXY_OWNER = "services/infra/safe_fs.py"
SATELLITE_OWNER = "host/safe_fs.py"

#: The calls of a module that take a path, by the module's import name.
_MODULE_CALLS: dict[str, frozenset[str]] = {
    "builtins": frozenset({"open"}),
    "io": frozenset({"open", "FileIO", "open_code"}),
    "codecs": frozenset({"open"}),
    "gzip": frozenset({"open", "GzipFile"}),
    "bz2": frozenset({"open"}),
    "lzma": frozenset({"open"}),
    "wave": frozenset({"open"}),
    "os": frozenset({"open", "remove", "unlink", "rename", "renames", "replace", "mkdir", "makedirs", "rmdir",
                     "removedirs", "listdir", "scandir", "walk", "fwalk", "chmod", "lchmod", "chown", "lchown",
                     "symlink", "link", "utime", "truncate", "mkfifo", "mknod", "setxattr", "removexattr"}),
    "shutil": frozenset({"copy", "copy2", "copyfile", "copytree", "copymode", "copystat", "move", "rmtree",
                         "chown", "make_archive", "unpack_archive"}),
    "glob": frozenset({"glob", "iglob"}),
    "filecmp": frozenset({"cmp", "cmpfiles", "dircmp"}),
    "tarfile": frozenset({"open", "TarFile"}),
    "zipfile": frozenset({"ZipFile", "Path"}),
    "sqlite3": frozenset({"connect"}),
    "aiosqlite": frozenset({"connect"}),
    "aiofiles": frozenset({"open"}),
    "aiofiles.os": frozenset({"remove", "unlink", "rename", "replace", "mkdir", "makedirs", "rmdir", "listdir",
                              "scandir"}),
    "anyio": frozenset({"open_file"}),
    "shelve": frozenset({"open"}),
    "dbm": frozenset({"open"}),
    "logging": frozenset({"FileHandler"}),
    "logging.handlers": frozenset({"RotatingFileHandler", "TimedRotatingFileHandler", "WatchedFileHandler"}),
    "PIL.Image": frozenset({"open"}),
    "fastapi.responses": frozenset({"FileResponse"}),
    "starlette.responses": frozenset({"FileResponse"}),
    "tempfile": frozenset({"mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile", "TemporaryDirectory",
                           "SpooledTemporaryFile"}),
}
_CALL_NAMES = frozenset(name for calls in _MODULE_CALLS.values() for name in calls)
#: How a call is written in the list.
_SHORT = {"builtins.open": "open", "PIL.Image.open": "Image.open", "fastapi.responses.FileResponse": "FileResponse",
          "starlette.responses.FileResponse": "FileResponse"}
#: The calls that take one path: one that is a descriptor's or bytes in
#: memory makes no site. A call of two (a copy, a move) is a site always.
_ONE_PATH = frozenset({"open", "io.open", "io.FileIO", "Image.open", "FileResponse", "tarfile.open",
                       "zipfile.ZipFile", "sqlite3.connect", "wave.open", "gzip.open", "codecs.open"})
#: A path's methods, called or handed on, on any object that is not an
#: imported module.
PATH_METHODS = frozenset({"read_text", "read_bytes", "write_text", "write_bytes", "open", "unlink", "rename",
                          "replace", "mkdir", "rmdir", "iterdir", "walk", "glob", "rglob", "chmod", "lchmod", "touch",
                          "symlink_to", "hardlink_to"})
#: An archive's methods that write by a path, whatever the archive was opened
#: from (its opener is excused when it reads bytes in memory).
_ARCHIVE_METHODS = frozenset({"extractall", "extract"})
#: ``str.replace`` takes two arguments: a path's ``replace`` and ``rename`` take
#: one, by position or as ``target``.
_ONE_ARGUMENT = frozenset({"rename", "replace"})
#: An argument that is no path, by its import name: a descriptor's path, bytes
#: in memory.
_NOT_A_PATH_ARGUMENTS = frozenset({"services.infra.safe_fs.fd_path", "io.BytesIO"})
#: Where a ``tempfile`` maker takes its folder by position (its index plus
#: one); the others take it seventh.
_TEMPFILE_DIR = {"tempfile.mkstemp": 3, "tempfile.mkdtemp": 3, "tempfile.TemporaryDirectory": 3,
                 "tempfile.SpooledTemporaryFile": 8}
#: Wrappers of a raw call under a name of their own, by import name.
PROXY_WRAPPERS = {"services.mcp.mcp_installer.remove_tree": "remove_tree"}
SATELLITE_WRAPPERS = {"config.force_rmtree": "force_rmtree", "config.atomic_replace": "atomic_replace"}

#: The safe helper for each job, in each tree's owner.
_HELPERS = {
    "proxy": {"read": "read_bytes_beneath or open_regular_for_read", "write": "atomic_write_beneath",
              "make a folder": "mkdirs_beneath", "remove": "unlink_beneath or rmtree_beneath",
              "list": "walk_beneath", "move": "rename_beneath or move_beneath",
              "copy": "copy_file_beneath or copytree_beneath"},
    "satellite": {"read": "read_bytes_beneath or open_regular_for_read", "write": "atomic_write_beneath",
                  "make a folder": "mkdirs_beneath", "remove": "unlink_beneath or rmdir_beneath",
                  "move": "rename_beneath"},
}
#: Each reason by the name a row writes it with.
_REASON_NAMES = {getattr(agent_tree_sites, name): name
                 for name in ("OWN", "COPY", "SANDBOX", "ELSEWHERE", "NOT_A_PATH", "PERSON", "AGENTS")}
#: An ``AGENTS`` row says which row (A to E) or finding (F, G) of the file
#: audit it is, or that it is new.
_AGENTS_WHY = re.compile(r"^(?:[A-G]\d{1,2}\b|new\b|" + re.escape(TO_JUDGE) + r")")
#: The one form of a row left on the roadmap.
_ACCEPTED_WHY = re.compile(r"^(?:[A-G]\d{1,2}|new): " + re.escape(agent_tree_sites.ACCEPTED) + r"'.+'$")
#: The owner's functions that take a root and open nothing: they read text.
_TEXT_ONLY = frozenset({"rel_under", "split_under"})
#: The place of a root a helper takes by its keyword only.
KEYWORD_ONLY = 99
#: What a root is when it is one of these, by its import name.
ROOT_NAMES = {"config.AGENTS_DIR": "agents", "config.SESSIONS_DIR": "sessions", "config.MCPS_DIR": "tools",
              **{f"config.{name}": "own" for name in ("RECOVER_BIN_DIR", "PREVIEW_SNAPSHOT_DIR", "UPLOAD_STAGING_DIR",
                                                       "MEDIA_CACHE_DIR", "HOOKS_DIR", "PLATFORM_DATA_DIR")}}
#: The calls whose answer is a root of a kind, by their import name.
ROOT_MAKERS = {"core.agent_folder.agent_root": "agent", "core.agent_folder.beneath_agent": "agent",
               "services.infra.safe_fs.open_root": "handle", "services.infra.safe_fs.open_dir_beneath": "handle",
               "services.infra.safe_fs.open_beneath": "handle", "host.safe_fs.open_root": "handle",
               "os.open": "handle", "os.dup": "handle", "tempfile.mkdtemp": "private", "config.otodock_dir": "own"}
#: The calls that call a function they are handed, with its place and
#: whether they hand it their keywords too: the function's parameters take
#: what follows it (``to_thread(f, root)`` hands ``f`` its root). A partial's
#: later arguments are out of sight.
FORWARDERS = {"asyncio.to_thread": (0, True), "functools.partial": (0, True), "core.file_commit.run": (0, True),
              "core.file_commit.run_in": (1, True), "core.file_commit.behind": (0, False),
              "run_in_executor": (1, False)}
#: The calls that keep their argument's root (a spelling, the same folder).
_SAME_ROOT = frozenset({"str", "os.fspath", "pathlib.Path", "Path", "os.path.realpath", "os.path.normpath"})
#: The placeholders the failures' texts print, pasted and never written.
_PASTED = ("<reason>", "<every raw call", "<what the path is and why the call is sound>",
           "<what the root is and why no other process replaces it>")


class _Frame(NamedTuple):
    """A function (or the module) as the pass stands in it: its import
    name, the names its own body binds, its parameters, its nested
    functions and the class it is a method of."""

    name: str
    assigned: dict[str, list[tuple[ast.expr, object]]]
    params: dict[str, ast.arg]
    nested: frozenset[str]
    klass: str


class _Function(NamedTuple):
    """What a tree function hands on to the roots: its parameters in order
    (a method's ``self`` left out), its keyword-only ones, the term of each
    default, and the term of each return (a tuple's elements kept apart)."""

    positional: tuple[str, ...]
    keyword: frozenset[str]
    defaults: dict[str, tuple]
    returns: tuple[tuple, ...]
    method: bool


class _Pass:
    """Each raw file call of one module, with the function it sits in; and
    each root handed to the tree owner's safe helpers, as a term the third
    list resolves (``test_safe_fs_roots.py``)."""

    def __init__(self, rel: str, bound: dict[str, str], exports: dict[str, dict[str, str]],
                 wrappers: dict[str, str], modules: frozenset[str], heads: frozenset[str],
                 owner: str = "", helpers: dict[str, tuple[tuple[int, str], ...]] | None = None) -> None:
        self.found: list[tuple[str, str]] = []
        #: ``(function, term, spelling)`` of each root handed to a safe helper.
        self.roots: list[tuple[str, tuple, str]] = []
        #: Each function of the module, by its import name.
        self.functions: dict[str, _Function] = {}
        #: ``(callee, positional terms, keyword terms, first starred place)``
        #: of each call of a function the tree may hold.
        self.handed: list[tuple[str, tuple, dict[str, tuple], int]] = []
        #: The tree functions read as a value, not called (their callers are
        #: out of sight), and the names of methods called on an object.
        self.referenced: set[str] = set()
        self.loose: set[str] = set()
        self._owner = owner
        self._helpers = helpers or {}
        self._satellite = owner == "host.safe_fs"
        self._frames: list[_Frame] = []
        self._module = names.module_of(rel)
        self._bound = bound
        self._exports = exports
        self._wrappers = wrappers
        self._modules = modules
        self._heads = heads
        self._local_wrappers = {target.rpartition(".")[2] for target in wrappers
                                if target.rpartition(".")[0] == self._module}
        self._attributes = _CALL_NAMES | {target.rpartition(".")[2] for target in wrappers}

    def visit(self, node: ast.AST, scope: tuple[str, ...] = ()) -> None:
        """Statements are entered one by one, so each site is told by the
        function and class it sits in (a decorator, a default and a base
        belong to the definition); an expression is walked whole, since no
        definition stands inside one."""
        if not self._frames:
            self._frames.append(_Frame(self._module, _assigned(node), {}, _nested(node), ""))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.arg) or child is getattr(node, "returns", None) or (
                    isinstance(node, ast.AnnAssign) and child is node.annotation):
                continue  # an annotation names a type, never a call
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._enter(child, (*scope, child.name))
            elif isinstance(child, ast.ClassDef):
                self.visit(child, (*scope, child.name))
            elif isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case, ast.arguments)):
                self.visit(child, scope)
            else:
                self._expression(child, ".".join(scope) or "<module>")

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef, scope: tuple[str, ...]) -> None:
        """A function: its frame for the roots, its body, then its summary."""
        name = f"{self._module}.{'.'.join(scope)}"
        args = node.args
        everyone = tuple(a for a in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg) if a)
        outer = self._frames[-1]
        klass = scope[-2] if len(scope) > 1 and outer.name == self._module and not outer.params else outer.klass
        positional = tuple(a.arg for a in (*args.posonlyargs, *args.args))
        if klass and positional and positional[0] in ("self", "cls"):
            positional = positional[1:]
        defaults = {a.arg: self._term(d) for a, d in zip((*args.posonlyargs, *args.args)[-len(args.defaults):]
                                                       if args.defaults else (), args.defaults, strict=True)}
        defaults.update({a.arg: self._term(d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                         if d is not None})
        self._frames.append(_Frame(name, _assigned(node), {a.arg: a for a in everyone}, _nested(node), klass))
        try:
            self.visit(node, scope)
            returns = tuple(self._return_term(r.value) for r in _own(node, ast.Return) if r.value is not None)
        finally:
            self._frames.pop()
        self.functions[name] = _Function(positional, frozenset(a.arg for a in args.kwonlyargs), defaults, returns,
                                         bool(klass) and outer.name == self._module)

    def _return_term(self, value: ast.expr) -> tuple:
        """A returned tuple's elements, or what the value is whole and as the
        first two elements it may hold (``beneath_agent``'s root and rel)."""
        if isinstance(value, ast.Tuple):
            return ("tuple", tuple(self._term(element) for element in value.elts))
        return ("value", self._term(value), self._term(value, 0), self._term(value, 1))

    def _callee(self, func: ast.AST) -> str | None:
        """The import name of the function a call calls: through the imports,
        a method of the class it sits in (``self.name``), a function nested in
        an enclosing one, or one of the module's own."""
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
            klass = self._frames[-1].klass
            return f"{self._module}.{klass}.{func.attr}" if klass else None
        if isinstance(func, ast.Name) and func.id not in self._bound:
            for frame in reversed(self._frames):
                if func.id in frame.nested:
                    return f"{frame.name}.{func.id}"
        return self._target(func)

    def _term(self, e: ast.AST, index: int | None = None, depth: int = 0, upto: int | None = None) -> tuple:
        """What ``e`` is as a root, in the frames up to ``upto``: a named kind
        ``("k", kind)``, a parameter ``("p", function, name)``, a tree
        function's return ``("r", function, index)``, one of several
        ``("or", terms)``, or unnamed ``("u",)``: a folder
        below a root among them, since a root is opened by its real place and
        only the root itself is known to be one no other process replaces."""
        frames = self._frames if upto is None else self._frames[:upto]
        if depth > 12:
            return ("u",)
        if isinstance(e, ast.Starred):
            return self._term(e.value, 0, depth + 1, upto)
        if isinstance(e, ast.Await):
            return self._term(e.value, index, depth + 1, upto)
        if isinstance(e, ast.Subscript) and isinstance(e.slice, ast.Constant) and isinstance(e.slice.value, int):
            return self._term(e.value, e.slice.value, depth + 1, upto)
        if isinstance(e, ast.Call):
            if isinstance(e.func, ast.Attribute) and e.func.attr in ("resolve", "absolute") and not e.args:
                return self._term(e.func.value, None, depth + 1, upto)
            callee = self._callee(e.func)
            forwarder = FORWARDERS.get("run_in_executor" if isinstance(e.func, ast.Attribute) and (
                e.func.attr == "run_in_executor") else callee or "")
            if forwarder and callee != "functools.partial" and forwarder[0] < len(e.args):
                handed = self._callee(e.args[forwarder[0]])
                return ("r", handed, index) if handed else ("u",)
            if callee in ROOT_MAKERS:
                return ("k", ROOT_MAKERS[callee]) if index in (None, 0) else ("u",)
            if isinstance(e.func, ast.Name) and e.func.id == "str" and e.func.id not in self._bound:
                callee = "str"
            if callee in _SAME_ROOT and e.args:
                return self._term(e.args[0], None, depth + 1, upto)
            return ("r", callee, index) if callee else ("u",)
        if isinstance(e, ast.Attribute):
            if e.attr in ("dirfd", "rootfd", "fd") or e.attr.endswith("_fd"):
                return ("k", "handle")
            if self._satellite and e.attr in ("agents_dir", "agent_dir"):
                return ("k", "agents" if e.attr == "agents_dir" else "agent")
            return ("k", ROOT_NAMES[self._target(e) or ""]) if (self._target(e) or "") in ROOT_NAMES else ("u",)
        if isinstance(e, ast.Name):
            for place in range(len(frames) - 1, -1, -1):
                frame = frames[place]
                if place == 0 and e.id in self._bound:
                    target = names.through(self._bound[e.id], self._exports)
                    return ("k", ROOT_NAMES[target]) if target in ROOT_NAMES else ("u",)
                if e.id in frame.assigned:
                    terms = tuple(self._bound_term(value, how, depth, place + 1, index)
                                  for value, how in frame.assigned[e.id])
                    return terms[0] if len(set(terms)) == 1 else ("or", terms)
                if e.id in frame.params:
                    annotation = frame.params[e.id].annotation
                    if isinstance(annotation, ast.Name) and annotation.id == "int":
                        return ("k", "handle")
                    return ("p", frame.name, e.id) if index is None else ("u",)
        return ("u",)

    def _bound_term(self, value: ast.expr, how: object, depth: int, upto: int, index: int | None) -> tuple:
        """The term of a name a statement bound: an assignment's value (one
        element of a tuple target), what ``with`` holds (an ``open_root``
        gives a held handle), or one of the values a loop takes."""
        if isinstance(how, int):
            return self._term(value, how, depth + 1, upto) if index is None else ("u",)
        if how is None:
            return self._term(value, index, depth + 1, upto)
        if index is not None:
            return ("u",)
        if how == "with":
            callee = self._callee(value.func) if isinstance(value, ast.Call) else None
            return ("k", "handle") if callee in ROOT_MAKERS and ROOT_MAKERS[callee] == "handle" else ("u",)
        if how == "loop":
            if isinstance(value, (ast.Tuple, ast.List)) and value.elts:
                terms = tuple(self._term(element, None, depth + 1, upto) for element in value.elts)
                return terms[0] if len(set(terms)) == 1 else ("or", terms)
        return ("u",)

    def _roots_of(self, node: ast.Call, function: str, called: set[int]) -> None:
        """A call of the owner's safe helper: each root it is handed. A call of
        any other function the tree may hold, or one a forwarder calls: what
        it hands on."""
        callee = self._callee(node.func)
        starred = next((place for place, arg in enumerate(node.args) if isinstance(arg, ast.Starred)), len(node.args))
        forwarder = FORWARDERS.get("run_in_executor" if isinstance(node.func, ast.Attribute) and (
            node.func.attr == "run_in_executor") else callee or "")
        if forwarder and forwarder[0] < min(starred, len(node.args)):
            place, keywords_too = forwarder
            handed = self._callee(node.args[place])
            if handed and self._may_hold(handed):
                called.add(id(node.args[place]))
                keywords = {k.arg or "**": self._term(k.value) if k.arg else ("u",)
                            for k in node.keywords} if keywords_too else {}
                rest = node.args[place + 1:]
                if callee == "functools.partial":
                    keywords["**"] = ("u",)
                    later = len(rest)
                else:
                    later = starred - place - 1 if starred < len(node.args) else len(rest)
                self.handed.append((handed, tuple(self._term(a) for a in rest), keywords, later))
        if not callee:
            return
        module, _, leaf = callee.rpartition(".")
        if module == self._owner and leaf in self._helpers:
            for place, keyword in self._helpers[leaf]:
                arg = next((k.value for k in node.keywords if k.arg == keyword), None)
                if arg is None and place < len(node.args) and place <= starred:
                    arg = node.args[place]
                if arg is not None:
                    self.roots.append((function, self._term(arg), ast.unparse(arg).replace(" ", "")))
        elif self._may_hold(callee):
            keywords = {k.arg or "**": self._term(k.value) if k.arg else ("u",) for k in node.keywords}
            self.handed.append((callee, tuple(self._term(a) for a in node.args), keywords, starred))
        if isinstance(node.func, ast.Attribute) and not (
                isinstance(node.func.value, ast.Name) and node.func.value.id in ("self", "cls")):
            self.loose.add(node.func.attr)

    def _may_hold(self, name: str) -> bool:
        """Whether ``name`` may be a function of the tree."""
        return name.split(".")[0] in self._heads or name.startswith(f"{self._module}.")

    def _expression(self, node: ast.AST, function: str) -> None:
        excused: set[int] = set()
        called: set[int] = set()
        for inner in ast.walk(node):
            if self._owner and isinstance(inner, ast.Call):
                called.add(id(inner.func))
                self._roots_of(inner, function, called)
            elif self._owner and isinstance(inner, (ast.Name, ast.Attribute)) and id(inner) not in called and (
                    isinstance(inner.ctx, ast.Load)):
                value = self._callee(inner)
                if value and self._may_hold(value):
                    self.referenced.add(value)
            if id(inner) in excused:
                continue
            if isinstance(inner, ast.Call):
                self._call(inner, function, excused)
            elif isinstance(inner, (ast.Name, ast.Attribute)) and isinstance(inner.ctx, ast.Load):
                named = self._module_call(inner)
                if named:
                    self.found.append((named, function))

    def _call(self, node: ast.Call, function: str, excused: set[int]) -> None:
        """A module's call is counted where its name is read (``_module_call``),
        unless the call takes no path; a path's method is counted here."""
        named = self._module_call(node.func)
        if named and self._holds_no_path(named, node):
            excused.add(id(node.func))
        elif not named:
            method = self._path_method(node.func)
            if method and not (method.rpartition(".")[2] in _ONE_ARGUMENT and not _one_target(node)):
                self.found.append((method, function))
        for arg in (*node.args, *(keyword.value for keyword in node.keywords)):
            if isinstance(arg, ast.Attribute) and not self._module_call(arg):
                handed = self._path_method(arg)
                if handed:
                    self.found.append((handed, function))

    def _is_module(self, target: str) -> bool:
        """Whether an imported name stands for a module (``os``, a tree
        module) rather than an object a module holds (``Path``,
        ``config.AGENTS_DIR``)."""
        if target in self._modules or target in _MODULE_CALLS:
            return True
        return target.split(".")[0] not in self._heads and target.rpartition(".")[2][:1].islower()

    def _named(self, target: str) -> str | None:
        """The listed spelling of a module's call, or None."""
        target = names.through(target, self._exports)
        if target in self._wrappers:
            return self._wrappers[target]
        module, _, name = target.rpartition(".")
        if name in _MODULE_CALLS.get(module, ()):
            return _SHORT.get(target, target)
        return None

    def _module_call(self, node: ast.AST) -> str | None:
        """What ``node`` names of a module's file call, read through the
        imports, or None."""
        if isinstance(node, ast.Name):
            if node.id == "open" and "open" not in self._bound:
                return "open"
            if node.id in self._bound:
                return self._named(self._bound[node.id])
            if node.id in self._local_wrappers:
                return self._named(f"{self._module}.{node.id}")
            return None
        if not isinstance(node, ast.Attribute) or node.attr not in self._attributes:
            return None
        chain, root = [node.attr], node.value
        while isinstance(root, ast.Attribute):
            chain.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name) and root.id in self._bound:
            return self._named(".".join([self._bound[root.id], *reversed(chain)]))
        return None

    def _path_method(self, func: ast.AST) -> str | None:
        """``Path.<method>`` when ``func`` is a path's method on an object
        that is not an imported module (``Archive.<method>`` for an archive's
        extract), or None."""
        if not isinstance(func, ast.Attribute) or func.attr not in PATH_METHODS | _ARCHIVE_METHODS:
            return None
        chain, root = [], func.value
        while isinstance(root, ast.Attribute):
            chain.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name) and root.id in self._bound:
            prefix = ".".join([self._bound[root.id], *reversed(chain)])
            if self._is_module(names.through(prefix, self._exports)):
                return None
        return f"{'Archive' if func.attr in _ARCHIVE_METHODS else 'Path'}.{func.attr}"

    def _target(self, node: ast.AST) -> str | None:
        """The import name ``node`` stands for (a name, or an attribute of an
        imported name), read through the imports; a name the module defines
        is the module's own."""
        chain = []
        while isinstance(node, ast.Attribute):
            chain.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        base = self._bound.get(node.id, f"{self._module}.{node.id}")
        return names.through(".".join([base, *reversed(chain)]), self._exports)

    def _holds_no_path(self, named: str, node: ast.Call) -> bool:
        """A call that takes no path: a temporary file in the system's own
        folder, an archive read from an object, or a call of one path that is
        the safe helpers' descriptor path or bytes in memory."""
        keywords = {keyword.arg for keyword in node.keywords}
        if named.startswith("tempfile."):
            return ("dir" not in keywords and None not in keywords
                    and len(node.args) < _TEMPFILE_DIR.get(named, 7))
        if named not in _ONE_PATH:
            return False
        if not node.args and "fileobj" in keywords:
            return True
        first = node.args[0] if node.args else None
        return isinstance(first, ast.Call) and self._target(first.func) in _NOT_A_PATH_ARGUMENTS


def _own(node: ast.AST, kind: type) -> list:
    """The nodes of ``kind`` in a function's own body, never in a function,
    a class or a lambda nested in it."""
    found, queue = [], list(ast.iter_child_nodes(node))
    while queue:
        child = queue.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, kind):
            found.append(child)
        queue.extend(ast.iter_child_nodes(child))
    return found


def _assigned(node: ast.AST) -> dict[str, list[tuple[ast.expr, object]]]:
    """The names a function's own body (or a module's top) binds: each with
    the value and how (``None``, a tuple target's place, or ``"with"``)."""
    out: dict[str, list[tuple[ast.expr, object]]] = collections.defaultdict(list)

    def bind(target: ast.expr, value: ast.expr, how: object) -> None:
        if isinstance(target, ast.Name):
            out[target.id].append((value, how))
        elif isinstance(target, ast.Tuple) and how is None:
            for place, element in enumerate(target.elts):
                if isinstance(element, ast.Name):
                    out[element.id].append((value, place))

    for statement in _own(node, (ast.Assign, ast.AnnAssign, ast.With, ast.AsyncWith, ast.NamedExpr,
                                 ast.For, ast.AsyncFor, ast.comprehension)):
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.comprehension)):
            for target in ast.walk(statement.target):
                if isinstance(target, ast.Name):
                    out[target.id].append((statement.iter, "loop" if target is statement.target else "u"))
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                bind(target, statement.value, None)
        elif isinstance(statement, (ast.AnnAssign, ast.NamedExpr)) and statement.value is not None:
            bind(statement.target, statement.value, None)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                for target in ast.walk(item.optional_vars) if item.optional_vars else ():
                    if isinstance(target, ast.Name):
                        out[target.id].append((item.context_expr, "with" if target is item.optional_vars else "u"))
    return out


def _nested(node: ast.AST) -> frozenset[str]:
    """The functions defined right in ``node``'s body (a module's own, or a
    function's nested ones)."""
    return frozenset(child.name for child in _own(node, ())
                     if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))) | frozenset(
        child.name for child in ast.iter_child_nodes(node) if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))


def _one_target(node: ast.Call) -> bool:
    """A path's ``rename`` or ``replace``: one argument, by position or as
    ``target``."""
    keywords = [keyword.arg for keyword in node.keywords]
    return (len(node.args) == 1 and not keywords) or (not node.args and keywords == ["target"])


def _files(tree: str) -> list[tuple[str, Path]]:
    """Each module a pass reads, by its key in the list."""
    if tree == "proxy":
        return (names.tree_files(PROXY, "", _PROXY_EXCLUDED, frozenset({PROXY_OWNER}))
                + (names.tree_files(AUDIO, "../audio/", _PROXY_EXCLUDED) if AUDIO.is_dir() else []))
    return names.tree_files(SATELLITE, "", _SATELLITE_EXCLUDED, frozenset({SATELLITE_OWNER}))


def _wrappers(tree: str) -> dict[str, str]:
    return PROXY_WRAPPERS if tree == "proxy" else SATELLITE_WRAPPERS


@functools.lru_cache(maxsize=4)
def _module_names(modules: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    """A tree's import names and their first parts, made once per tree."""
    return frozenset(modules), frozenset(module.split(".")[0] for module in modules)


def _visit(tree: str, rel: str, module: ast.AST, bound: dict[str, str],
           exports: dict[str, dict[str, str]]) -> _Pass:
    modules, heads = _module_names(tuple(exports))
    pass_ = _Pass(rel, bound, exports, _wrappers(tree), modules, heads, OWNERS[tree], root_helpers(tree))
    pass_.visit(module)
    return pass_


#: The import name of each tree's owner of the safe helpers.
OWNERS = {"proxy": "services.infra.safe_fs", "satellite": "host.safe_fs"}


@functools.cache
def root_helpers(tree: str) -> dict[str, tuple[tuple[int, str], ...]]:
    """Each function of the tree's owner that takes a root (a parameter
    ``root``, ``src_root`` or ``dst_root``) and opens beneath it, with the
    place of each root among its arguments and its name: read from the
    owner, so a new helper is read the day it is written."""
    path = PROXY / PROXY_OWNER if tree == "proxy" else SATELLITE / SATELLITE_OWNER
    helpers = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_") or node.name in _TEXT_ONLY:
            continue
        positional = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
        roots = tuple((positional.index(name) if name in positional else KEYWORD_ONLY, name)
                      for name in (*positional, *(a.arg for a in node.args.kwonlyargs))
                      if name in ("root", "src_root", "dst_root"))
        if roots:
            helpers[node.name] = roots
    return helpers


class _Facts(NamedTuple):
    """What the tests keep of one module: its imports, its raw calls with
    their functions, and what the third list reads of its roots."""

    bound: dict[str, str]
    calls: list[tuple[str, str]]
    roots: list[tuple[str, tuple, str]]
    functions: dict[str, _Function]
    handed: list[tuple[str, tuple, dict[str, tuple], int]]
    referenced: set[str]
    loose: set[str]


def _facts_of(tree: str, rel: str, module: ast.AST, bound: dict[str, str],
              exports: dict[str, dict[str, str]]) -> _Facts:
    pass_ = _visit(tree, rel, module, bound, exports)
    return _Facts(bound, pass_.found, pass_.roots, pass_.functions, pass_.handed, pass_.referenced, pass_.loose)


@functools.cache
def _facts(tree: str) -> dict[str, _Facts]:
    """Each module's facts, read once, one module's syntax in memory at a
    time: the imports of every module first (a name is followed through the
    tree), then the calls. Every tree held at once is about half a gigabyte,
    and the suite's workers run under one memory cap."""
    files = _files(tree)
    with names.paused_gc():
        bound = {rel: names.bindings(ast.parse(path.read_text(encoding="utf-8")), rel) for rel, path in files}
        exports = {names.module_of(rel): imports for rel, imports in bound.items()}
        return {rel: _facts_of(tree, rel, ast.parse(path.read_text(encoding="utf-8")), bound[rel], exports)
                for rel, path in files}


def _sites(found: dict[str, list[tuple[str, str]]]) -> dict[tuple[str, str], list[str]]:
    sites: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for rel, calls in found.items():
        for named, function in calls:
            sites[(rel, function)].append(named)
    return {key: sorted(calls) for key, calls in sites.items()}


@functools.cache
def _tree_found(tree: str) -> dict[tuple[str, str], list[str]]:
    """Every site of ``tree``: ``(file, function)`` -> its calls, sorted."""
    return _sites({rel: facts.calls for rel, facts in _facts(tree).items()})


def _module_found(tree: str, rel: str, text: str) -> dict[tuple[str, str], list[str]]:
    """The sites of one module's text alone, its names read through its own
    imports: what a change to one file brings, without the whole tree."""
    module = ast.parse(text)
    bound = names.bindings(module, rel)
    return _sites({rel: _visit(tree, rel, module, bound, {names.module_of(rel): bound}).found})


def _listed(tree: str) -> dict[tuple[str, str], Site]:
    return PROXY_SITES if tree == "proxy" else SATELLITE_SITES


class _Diff(NamedTuple):
    """What the code and the list disagree on, each line ``file, function:
    calls``: a function with no row (``new``), a listed function with more
    calls than its row (``grown``), calls a row holds that the code lost
    (``gone``), and rows whose function was renamed (``moved``)."""

    new: list[str]
    grown: list[str]
    gone: list[str]
    moved: list[str]


def _line(key: tuple[str, str], calls: list[str]) -> str:
    return f"{key[0]}, function {key[1]}: {' '.join(calls)}"


def _differences(tree: str, found: dict[tuple[str, str], list[str]], files: set[str] | None = None,
                 rows: dict[tuple[str, str], Site] | None = None) -> _Diff:
    """Where ``found`` and the list (or ``rows``, the roots' list) disagree;
    ``files`` limits the comparison to those files. A function whose calls
    all left one row and arrived whole in another of the same file is
    moved."""
    listed = {key: site for key, site in (_listed(tree) if rows is None else rows).items()
              if files is None or key[0] in files}
    new, gone = {}, {}
    for key in sorted(set(found) | set(listed)):
        have = collections.Counter(found.get(key, []))
        want = collections.Counter(listed[key].calls.split() if key in listed else [])
        if have - want:
            new[key] = sorted((have - want).elements())
        if want - have:
            gone[key] = sorted((want - have).elements())
    moved = _moved_files(listed, found, new, gone)
    for key, calls in list(new.items()):
        twin = next((old for old, left in gone.items() if old[0] == key[0] and left == calls
                     and old not in found and key not in listed), None)
        if twin is not None:
            moved.append(f"{key[0]}: the row of {twin[1]} now belongs to {key[1]}")
            del new[key], gone[twin]
    return _Diff([_line(key, calls) for key, calls in new.items() if key not in listed],
                 [_line(key, calls) for key, calls in new.items() if key in listed],
                 [_line(key, calls) for key, calls in gone.items()], moved)


def _moved_files(listed: dict[tuple[str, str], Site], found: dict[tuple[str, str], list[str]],
                 new: dict[tuple[str, str], list[str]], gone: dict[tuple[str, str], list[str]]) -> list[str]:
    """A file renamed whole: every row of a listed file is gone, nothing is
    found in it, and the same functions with the same calls arrive in one
    file that has no row. Its lines leave ``new`` and ``gone``."""
    moved = []
    found_files = {key[0] for key in found}
    listed_files = {key[0] for key in listed}
    for rel in sorted(listed_files - found_files):
        left = {key[1]: calls for key, calls in gone.items() if key[0] == rel}
        for other in sorted({key[0] for key in new} - listed_files):
            if {key[1]: calls for key, calls in new.items() if key[0] == other} == left:
                moved.append(f"the file {rel} moved to {other}: change the file of its rows")
                for function in left:
                    del gone[(rel, function)], new[(other, function)]
                break
    return moved


def _parts(line: str) -> tuple[str, str, list[str]]:
    rel, _, rest = line.partition(", function ")
    function, _, calls = rest.partition(": ")
    return rel, function, calls.split()


def _instruction(tree: str, diff: _Diff) -> str:
    """What to do, for a contributor who never saw this test."""
    lists = "PROXY_SITES" if tree == "proxy" else "SATELLITE_SITES"
    owner = "proxy/services/infra/safe_fs.py" if tree == "proxy" else "satellite/host/safe_fs.py"
    helpers = "; ".join(f"to {job}: {name}" for job, name in _HELPERS[tree].items())
    use_the_helper = (
        "If the path can be inside the agents folder (a workspace, a person's folders, .claude, .codex, config, "
        "knowledge, an app's folders), a session or a person may have put a link or a pipe there. Do the job "
        f"through {owner}, which opens beneath a root with no link followed ({helpers}). Then the call leaves "
        "the list.")
    parts = []
    if diff.new:
        rows = []
        for line in diff.new:
            rel, function, calls = _parts(line)
            rows.append(f'    ("{rel}", "{function}"): Site(<reason>, "{" ".join(sorted(calls))}", '
                        f'"<what the path is and why the call is sound>"),')
        reasons = ", ".join(list(_REASON_NAMES.values())[:-1]) + " or " + list(_REASON_NAMES.values())[-1]
        parts.append(
            f"A raw file call is not on the list in proxy/tests/core/agent_tree_sites.py ({lists}):\n  "
            + "\n  ".join(diff.new) + "\n\n" + use_the_helper
            + " If the path is the server's own data (its sessions folder, a temporary folder it made, a shipped "
            "file), add the row below with a reason from proxy/tests/core/agent_tree_sites.py (" + reasons
            + ", each defined there) and a why in place of <...>:\n" + "\n".join(rows))
    if diff.grown:
        rows = []
        listed = _listed(tree)
        for line in diff.grown:
            rel, function, calls = _parts(line)
            site = listed[(rel, function)]
            merged = " ".join(sorted([*site.calls.split(), *calls]))
            rows.append(f'    ("{rel}", "{function}"): Site({_REASON_NAMES[site.reason]}, "{merged}", "{site.why}"),')
        parts.append(
            f"A function that already has a row in proxy/tests/core/agent_tree_sites.py ({lists}) makes a new raw "
            "file call:\n  " + "\n  ".join(diff.grown) + "\n\nThe row's why must hold for the new call too. "
            + use_the_helper + " If the why holds, change the row's calls to these, never add a second row:\n"
            + "\n".join(rows))
    return "\n\n".join(parts)


def _assert_exact(tree: str, found: dict[tuple[str, str], list[str]], files: set[str] | None = None) -> None:
    diff = _differences(tree, found, files)
    problems = []
    if diff.new or diff.grown:
        problems.append(_instruction(tree, diff))
    if diff.moved:
        problems.append("A function or a file was renamed or moved; change the key of its rows, the judgement stays:"
                        "\n  " + "\n  ".join(diff.moved))
    if diff.gone:
        problems.append("Listed in proxy/tests/core/agent_tree_sites.py and no longer in the code. Take these "
                        "calls out of their rows (a row left with none goes):\n  " + "\n  ".join(diff.gone))
    assert not problems, "\n\n".join(problems)


def _repeated_keys(text: str) -> list[str]:
    """The keys a dict literal of the list holds twice, as ``NAME: key``."""
    repeated = []
    for node in ast.walk(ast.parse(text)):
        target = (node.targets[0] if isinstance(node, ast.Assign) else
                  node.target if isinstance(node, ast.AnnAssign) else None)
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
            seen = set()
            for key in node.value.keys:
                value = ast.literal_eval(key) if key is not None else None
                if value in seen:
                    repeated.append(f"{target.id}: {value}")
                seen.add(value)
    return repeated


def test_every_raw_file_call_in_the_server_is_listed():
    _assert_exact("proxy", _tree_found("proxy"))


def test_every_raw_file_call_in_the_satellite_is_listed():
    if not SATELLITE.is_dir():
        pytest.skip("no satellite beside the proxy")
    _assert_exact("satellite", _tree_found("satellite"))


def _unexplained(tree: str, rows: dict[tuple[str, str], Site] | None = None) -> list[str]:
    """The rows (of the list, or ``rows``) that do not say why."""
    out = []
    for (rel, function), site in sorted((_listed(tree) if rows is None else rows).items()):
        if any(pasted in site.why or pasted in site.calls for pasted in _PASTED) or "<" in site.calls:
            out.append(f"{rel}, function {function}: the row still holds a placeholder from the failure's text")
        elif not site.why.strip():
            out.append(f"{rel}, function {function}: say what the path is and why the call is sound there")
        elif "accepted" in site.why.split(",")[0] and not _ACCEPTED_WHY.match(site.why):
            out.append(f"{rel}, function {function}: a row left on the roadmap reads \"<the audit's row or new>: "
                       f"{agent_tree_sites.ACCEPTED}'<its title>'\" and nothing else")
        elif site.reason == AGENTS and not _AGENTS_WHY.match(site.why):
            out.append(f"{rel}, function {function}: a row of the agents tree starts with the audit's row, "
                       f"'new' or '{TO_JUDGE.strip()}', then says why it is sound")
        elif site.reason not in REASONS:
            out.append(f"{rel}, function {function}: '{site.reason}' is no reason of the list")
    return out


@pytest.mark.parametrize("tree", ["proxy", "satellite"])
def test_every_row_says_why(tree):
    """The brief's rule was narrower: a row in a module that names the agents
    tree says why. A module that takes its path from a caller names nothing
    of the tree (the pasted photo's), so every row says why."""
    unexplained = _unexplained(tree)
    assert not unexplained, "In proxy/tests/core/agent_tree_sites.py:\n  " + "\n  ".join(unexplained)


def test_no_row_waits_to_be_judged():
    waiting = [f"{tree}: {key}" for tree in ("proxy", "satellite") for key, site in _listed(tree).items()
               if site.why.startswith(TO_JUDGE)]
    assert not waiting, f"rows still to judge: {waiting}"


def test_the_owners_are_where_the_pass_leaves_them_out_and_hold_the_helpers_it_names():
    for tree, root, owner in (("proxy", PROXY, PROXY_OWNER), ("satellite", SATELLITE, SATELLITE_OWNER)):
        if not root.is_dir():
            continue
        defined = {node.name for node in ast.parse((root / owner).read_text(encoding="utf-8")).body
                   if isinstance(node, ast.FunctionDef)}
        named = {word for text in _HELPERS[tree].values() for word in text.split(" or ")}
        assert named <= defined, (tree, named - defined)


def _calls(source: str, rel: str = "services/zz_new.py", tree: str = "proxy") -> list[str]:
    """What the pass finds in ``source`` alone, sorted."""
    return sorted(call for calls in _module_found(tree, rel, source).values() for call in calls)


_SPELLINGS = {
    "open": ("def f(p):\n    with open(p) as fh:\n        return fh.read()\n", ["open"]),
    "an aliased module": ("import shutil as sh\n\ndef f(p):\n    sh.rmtree(p)\n", ["shutil.rmtree"]),
    "a name imported under another": ("from os import unlink as rm\n\ndef f(p):\n    rm(p)\n", ["os.unlink"]),
    "handed on": ("import asyncio, os, shutil\n\nasync def f(p):\n    await asyncio.to_thread(shutil.rmtree, p)\n"
                  "    await asyncio.get_running_loop().run_in_executor(None, os.unlink, p)\n",
                  ["os.unlink", "shutil.rmtree"]),
    "kept under another name": ("import os, shutil\n\nREMOVE = {'file': os.unlink}\n\n"
                                "def f(p, gone=shutil.rmtree):\n    rm = os.rmdir\n    return rm\n",
                                ["os.rmdir", "os.unlink", "shutil.rmtree"]),
    "a path's method handed on": (
        "import asyncio\n\nasync def f(p):\n    return await asyncio.to_thread(p.read_text, encoding='utf-8')\n",
        ["Path.read_text"]),
    "a path's methods": ("from pathlib import Path\n\ndef f(p: Path):\n    p.parent.mkdir(parents=True)\n"
                         "    p.write_text('x')\n    Path(p).read_bytes()\n    p.replace(p.with_suffix('.b'))\n",
                         ["Path.mkdir", "Path.read_bytes", "Path.replace", "Path.write_text"]),
    "a path held by a module": ("import config\nfrom config import AGENTS_DIR\n\ndef f():\n"
                                "    config.AGENTS_DIR.mkdir()\n    AGENTS_DIR.iterdir()\n",
                                ["Path.iterdir", "Path.mkdir"]),
    "a wrapper under its own name": (
        "from services.mcp import mcp_installer\n\ndef f(p):\n    mcp_installer.remove_tree(p)\n", ["remove_tree"]),
    "the os module's calls": ("import os\n\ndef f(p):\n    os.makedirs(p)\n    os.chmod(p, 0o700)\n"
                              "    for _ in os.scandir(p):\n        pass\n", ["os.chmod", "os.makedirs", "os.scandir"]),
    "archives, databases and logs": (
        "import logging, sqlite3, tarfile, zipfile\n\ndef f(p):\n    sqlite3.connect(p)\n    tarfile.open(p)\n"
        "    zipfile.ZipFile(p)\n    logging.FileHandler(p)\n",
        ["logging.FileHandler", "sqlite3.connect", "tarfile.open", "zipfile.ZipFile"]),
    "a file served by its path": ("from fastapi.responses import FileResponse\n\ndef f(p):\n"
                                  "    return FileResponse(p)\n", ["FileResponse"]),
    "a temporary file in a folder of the caller's": (
        "import tempfile\n\ndef f(d, kw):\n    return (tempfile.mkstemp(dir=d), tempfile.mkstemp('', '', d), "
        "tempfile.mkdtemp(**kw), tempfile.mkstemp())\n",
        ["tempfile.mkdtemp", "tempfile.mkstemp", "tempfile.mkstemp"]),
    "a copy from a descriptor to a name": (
        "import os, shutil\nfrom services.infra import safe_fs\n\ndef f(fd, dest):\n"
        "    shutil.copyfile(safe_fs.fd_path(fd), dest)\n    os.replace(safe_fs.fd_path(fd), dest)\n",
        ["os.replace", "shutil.copyfile"]),
    "a path's walk": ("def f(p):\n    return list(p.walk())\n", ["Path.walk"]),
    "a temporary folder by position": (
        "import tempfile\n\ndef f(d):\n    tempfile.TemporaryDirectory(None, None, d)\n"
        "    tempfile.SpooledTemporaryFile(0, 'w+b', -1, None, None, None, None, d)\n"
        "    tempfile.SpooledTemporaryFile(0, 'w+b', -1, None, None, None, None)\n",
        ["tempfile.SpooledTemporaryFile", "tempfile.TemporaryDirectory"]),
    "an archive opened from bytes and extracted": (
        "import io, tarfile\n\ndef f(blob, dest, zf):\n"
        "    tarfile.open(fileobj=io.BytesIO(blob)).extractall(dest, filter='data')\n    zf.extract('a', dest)\n",
        ["Archive.extract", "Archive.extractall"]),
    "a function of the module's own named fd_path": (
        "def fd_path(x):\n    return x\n\ndef f(p):\n    return open(fd_path(p))\n", ["open"]),
    "a rename or a replace by keyword": (
        "def f(p, q):\n    p.rename(target=q)\n    p.replace(target=q)\n", ["Path.rename", "Path.replace"]),
    "io.open_code and filecmp": (
        "import filecmp, io\n\ndef f(a, b):\n    io.open_code(a)\n    return filecmp.cmp(a, b)\n",
        ["filecmp.cmp", "io.open_code"]),
    # What is no site.
    "the helper's fd_path under another name, BytesIO imported": (
        "from io import BytesIO\nfrom PIL import Image\nfrom services.infra.safe_fs import fd_path as fp\n\n"
        "def f(fd, b):\n    Image.open(BytesIO(b))\n    return open(fp(fd))\n", []),
    "str.replace and a dict's methods": (
        "def f(s, d):\n    return s.replace('a', 'b'), d.get('open'), s.rename\n", []),
    "a query": ("import os\n\ndef f(p):\n    return os.path.exists(p), os.stat(p), p.is_file(), p.resolve()\n", []),
    "safe_fs": ("from services.infra import safe_fs\n\ndef f(root, rel):\n"
                "    return safe_fs.read_bytes_beneath(root, rel, max_size=1)\n", []),
    "a descriptor's path or bytes in memory": (
        "import io\nfrom PIL import Image\nfrom fastapi.responses import FileResponse\nfrom services.infra import "
        "safe_fs\n\ndef f(fd, blob, st):\n    Image.open(io.BytesIO(blob))\n"
        "    return FileResponse(safe_fs.fd_path(fd), stat_result=st)\n", []),
    "another module's function of a path's name": (
        "import webbrowser\n\ndef f(url):\n    webbrowser.open(url)\n", []),
    "an import alone": ("import os, shutil\nfrom os import unlink\n", []),
}


@pytest.mark.parametrize(("source", "calls"), list(_SPELLINGS.values()), ids=list(_SPELLINGS))
def test_the_pass_sees_the_spellings_of_a_raw_call(source, calls):
    assert _calls(source) == calls


def test_a_relative_import_of_a_satellite_wrapper_is_seen():
    source = "from ..config import force_rmtree as wipe\n\ndef f(p):\n    wipe(p)\n"
    assert _calls(source, rel="sessions/zz_new.py", tree="satellite") == ["force_rmtree"]


def test_a_wrapper_is_seen_in_its_own_module():
    source = "import shutil\n\ndef remove_tree(p):\n    shutil.rmtree(p)\n\ndef f(p):\n    remove_tree(p)\n"
    assert _calls(source, rel="services/mcp/mcp_installer.py") == ["remove_tree", "shutil.rmtree"]


def _function_of(rel: str, function: str, module: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The definition of ``function`` (``Class.method``, ``outer.inner``) in
    ``module``."""
    node: ast.AST = module
    for part in function.split("."):
        [node] = [n for n in ast.iter_child_nodes(node)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)), (rel, function)
    return node


def _a_listed_function_of_the_agents_tree() -> tuple[str, str]:
    """An ``AGENTS`` row of a top-level function, the first one in order."""
    for (rel, function), site in sorted(PROXY_SITES.items()):
        if site.reason == AGENTS and "." not in function and function != "<module>":
            return rel, function
    raise AssertionError("no AGENTS row to change")


def _changed(rel: str, function: str, change) -> str:
    module = ast.parse((PROXY / rel).read_text(encoding="utf-8"))
    change(_function_of(rel, function, module))
    return ast.unparse(module)


def test_a_second_call_in_a_listed_function_shows_its_row_merged():
    """A function that already has a row and gains a call: the failure shows
    that row, its reason and its why, with the calls merged, so the
    judgement is read again and never pasted over."""
    rel, function = _a_listed_function_of_the_agents_tree()
    site = PROXY_SITES[(rel, function)]
    text = _changed(rel, function, lambda f: f.body.extend(ast.parse("import shutil as sh\nsh.rmtree(x)").body))
    diff = _differences("proxy", _module_found("proxy", rel, text), {rel})
    assert diff.grown == [f"{rel}, function {function}: shutil.rmtree"] and not diff.new and not diff.gone
    message = _instruction("proxy", diff)
    merged = " ".join(sorted([*site.calls.split(), "shutil.rmtree"]))
    assert "already has a row" in message and "safe_fs.py" in message
    assert f'("{rel}", "{function}"): Site({_REASON_NAMES[site.reason]}, "{merged}", "{site.why}")' in message


def test_the_same_call_a_second_time_in_a_listed_function_fails():
    """The calls are counted, not a set: a second read where the row lists
    one is a new call to judge."""
    rel, function = next(key for key, site in sorted(PROXY_SITES.items())
                         if site.reason == AGENTS and "." not in key[1] and key[1] != "<module>"
                         and "Path.mkdir" in site.calls.split())
    text = _changed(rel, function, lambda f: f.body.extend(ast.parse("zz_folder.mkdir()").body))
    diff = _differences("proxy", _module_found("proxy", rel, text), {rel})
    assert diff.grown == [f"{rel}, function {function}: Path.mkdir"] and not diff.new and not diff.gone


def test_no_key_is_listed_twice():
    """Python keeps the last of two equal keys and drops the first, its
    judgement with it: a row pasted a second time is refused."""
    text = (Path(__file__).parent / "agent_tree_sites.py").read_text(encoding="utf-8")
    assert _repeated_keys(text) == []
    pasted = 'PROXY_SITES = {("a.py", "f"): 1, ("b.py", "f"): 2, ("a.py", "f"): 3}\n'
    assert _repeated_keys(pasted) == ["PROXY_SITES: ('a.py', 'f')"]


def test_a_new_raw_call_in_a_new_module_fails():
    rel = "services/zz_keep.py"
    source = ("import config\n\n\ndef keep(agent, name, data):\n"
              "    (config.AGENTS_DIR / agent / 'workspace' / name).write_bytes(data)\n")
    diff = _differences("proxy", _module_found("proxy", rel, source), {rel})
    assert diff == _Diff([f"{rel}, function keep: Path.write_bytes"], [], [], [])


def test_a_call_that_left_the_code_fails():
    rel, function = _a_listed_function_of_the_agents_tree()
    text = _changed(rel, function, lambda f: setattr(f, "body", [ast.Pass()]))
    diff = _differences("proxy", _module_found("proxy", rel, text), {rel})
    assert not diff.new and not diff.grown and any(line.startswith(f"{rel}, function {function}:")
                                                   for line in diff.gone)


def test_a_renamed_function_is_told_as_moved():
    rel, function = _a_listed_function_of_the_agents_tree()
    text = _changed(rel, function, lambda f: setattr(f, "name", f"{f.name}_renamed"))
    diff = _differences("proxy", _module_found("proxy", rel, text), {rel})
    assert diff == _Diff([], [], [], [f"{rel}: the row of {function} now belongs to {function}_renamed"])


def test_a_row_with_no_why_or_a_pasted_one_fails(monkeypatch):
    rel, function = _a_listed_function_of_the_agents_tree()
    site = PROXY_SITES[(rel, function)]
    for why in ("", "<what the path is and why the call is sound>", "sound, trust me"):
        monkeypatch.setitem(PROXY_SITES, (rel, function), Site(site.reason, site.calls, why))
        assert any(line.startswith(f"{rel}, function {function}:") for line in _unexplained("proxy")), why


def test_a_pasted_placeholder_is_refused_on_an_own_data_row_and_in_the_calls(monkeypatch):
    """The failure prints placeholders for an own-data row: pasted as they
    stand, in the why or in the calls, the row is refused."""
    key, site = next((key, site) for key, site in sorted(PROXY_SITES.items()) if site.reason == agent_tree_sites.OWN)
    rel, function = key
    for why in ("", "<what the path is and why the call is sound>"):
        monkeypatch.setitem(PROXY_SITES, key, Site(site.reason, site.calls, why))
        assert any(line.startswith(f"{rel}, function {function}:") for line in _unexplained("proxy")), why
    monkeypatch.setitem(PROXY_SITES, key, Site(site.reason, f"<every raw call of {function}, sorted>", site.why))
    assert any(line.startswith(f"{rel}, function {function}:") for line in _unexplained("proxy"))


def test_the_audio_package_is_read_with_the_proxy():
    """``ws/audio.py`` imports it, so its calls are the server's own process's
    calls, as the process surface reads it too."""
    if not AUDIO.is_dir():
        pytest.skip("no audio package beside the proxy")
    assert any(rel.startswith("../audio/") for rel, _path in _files("proxy"))


def test_the_failure_names_the_reasons_and_prints_the_calls():
    """For a function with no row, the calls column is a fact: the failure
    fills it in, and names the reasons a row may take."""
    rel = "services/zz_keep.py"
    source = ("import config\n\n\ndef keep(agent, data):\n    folder = config.AGENTS_DIR / agent\n"
              "    folder.mkdir()\n    (folder / 'x').write_bytes(data)\n")
    message = _instruction("proxy", _differences("proxy", _module_found("proxy", rel, source), {rel}))
    assert "OWN, COPY, SANDBOX, ELSEWHERE, NOT_A_PATH, PERSON or AGENTS" in message
    assert f'("{rel}", "keep"): Site(<reason>, "Path.mkdir Path.write_bytes", ' in message
    assert "every raw call" not in message


def test_a_moved_file_is_told_as_a_move():
    """A file renamed whole: its rows are gone in one file and the same
    functions arrive whole in another. The failure says the file moved."""
    rel = sorted({key[0] for key in PROXY_SITES})[0]
    moved_to = "services/zz_moved_here.py"
    found = {(moved_to if key[0] == rel else key[0], key[1]): sorted(site.calls.split())
             for key, site in PROXY_SITES.items() if key[0] == rel}
    diff = _differences("proxy", found, {rel, moved_to})
    assert diff == _Diff([], [], [], [f"the file {rel} moved to {moved_to}: change the file of its rows"])


def test_a_why_may_hold_a_bracket_but_not_a_pasted_placeholder(monkeypatch):
    key, site = next((key, site) for key, site in sorted(PROXY_SITES.items()) if site.reason == agent_tree_sites.OWN)
    monkeypatch.setitem(PROXY_SITES, key, Site(site.reason, site.calls, "an html <head> in its own data folder"))
    assert not any(line.startswith(f"{key[0]}, function {key[1]}:") for line in _unexplained("proxy"))
    monkeypatch.setitem(PROXY_SITES, key, Site(site.reason, site.calls, "<what the path is and why the call is sound>"))
    assert any("still holds a placeholder" in line for line in _unexplained("proxy"))


def test_a_row_left_on_the_roadmap_has_one_honest_form(monkeypatch):
    """A lane may stop on a site and leave it on the roadmap. Its row then
    says so in one fixed form, never that the call is sound."""
    rel, function = _a_listed_function_of_the_agents_tree()
    site = PROXY_SITES[(rel, function)]
    good = f"A10: {agent_tree_sites.ACCEPTED}'The file tree listing'"
    titled = f"C15: {agent_tree_sites.ACCEPTED}'The installed tool folders' remainder'"
    for why, refused in ((good, False), (titled, False), ("A10: accepted", True),
                         ("A10: accepted for now, see the roadmap", True),
                         (f"{agent_tree_sites.ACCEPTED}'The file tree listing'", True),
                         (f"A10: {agent_tree_sites.ACCEPTED}''", True)):
        monkeypatch.setitem(PROXY_SITES, (rel, function), Site(site.reason, site.calls, why))
        flagged = any(line.startswith(f"{rel}, function {function}:") for line in _unexplained("proxy"))
        assert flagged is refused, why


def test_an_agents_row_names_a_row_or_a_finding_of_the_audit(monkeypatch):
    """The audit's table rows run A to E and its findings F and G: a row of
    the agents tree may start with either, and with nothing else."""
    rel, function = _a_listed_function_of_the_agents_tree()
    site = PROXY_SITES[(rel, function)]
    for why, refused in (("F22: hashed through a descriptor", False), ("G1: read beneath the hold", False),
                         ("B6: made beneath the root", False), ("H1: a row of no audit", True),
                         ("a sibling of F21: removed beneath the root", True)):
        monkeypatch.setitem(PROXY_SITES, (rel, function), Site(AGENTS, site.calls, why))
        flagged = any(line.startswith(f"{rel}, function {function}:") for line in _unexplained("proxy"))
        assert flagged is refused, why
