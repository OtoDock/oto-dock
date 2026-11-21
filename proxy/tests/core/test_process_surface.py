"""Who in the proxy starts a process, and who builds a sandbox's command.

The proxy is the control plane: a session, a terminal, a tool's process, an
app's server, a step and a build belong on a compute machine or a paired
computer, started by the satellite there. What the proxy's own process may
start is short and none of it runs an agent's code: git for the memory and
config folders, the Docker tools' commands, ffmpeg for media, a syntax check,
the quota helper, an engine's sign-in.

What the test proves, in words the release notes may use: no part of the
server starts a chat's engine, a terminal, a tool or a build. The release
gate reads every module of the server and fails on a new one. It does not
prove that the server cannot: it reads names, and the last paragraph lists
what it cannot see.

One pass over every ``.py`` under ``proxy/`` and under ``audio/``, the
package the proxy imports into its own process (tests, docs and the venv
excluded), finds each site that names a process start or a sandbox command
builder: a call of it, the function handed on
(``to_thread(subprocess.run, ...)``), a command list that opens with a
sandbox's program, by its name or by a path to it, or a call of one of the
runners that start a build step or a script. A name is read through the
module's imports (``tests/_import_names.py``, a relative import made
absolute), as the one-door check reads them: ``import subprocess as
sp`` and ``from os import posix_spawn as start`` name the same starts. It is
also followed through the tree's own modules: a start one module imported
under a name of its own is seen where another module imports that name.
``SITES`` lists them by file and name, each with what it runs and
the function each site sits in, once per site. The first test keeps the list
exact in both directions: a new site fails until it is listed with its
reason, also a second one in a function already listed, and a listed site
that left the code fails until it is removed.

No listed site starts an agent's code. The names of the starts the proxy
has no business with (a terminal's, a piped engine's, a sandbox command's
builder, a boot probe of a sandbox) stay in the pass, so a call of one is a
new site that fails. A call of the render's own check or of one of its
probes, of the Codex client, of the tool stand-in or of the system packages'
installer starts a process on a line inside that source, which is listed:
that caller is the release gate's one-door check to refuse (each is a door of
its entry). The last test holds the split that goes with it: an engine class
holds no session.

The pass reads syntax. What stays invisible to it: a module or a function
held in a variable (``sp = subprocess``), a name in a ``getattr`` string, a
star import, ``importlib`` and ``__import__``, a start behind a library the pass does not name (``multiprocessing``, a
process pool, ``pexpect``, ``os.startfile``, the Docker SDK), and a wrapper of
a start defined in another module under a name of its own. The last is why a
new helper that starts a process is itself a listed site.
"""

from __future__ import annotations

import ast
import collections
import functools
from pathlib import Path, PurePosixPath

import pytest

from tests import _import_names as names
from tests._paths import REPO_ROOT

PROXY = Path(REPO_ROOT) / "proxy"
_EXCLUDED_PARTS = {"tests", "docs", "venv", ".venv", "node_modules", "__pycache__"}

AUDIO = Path(REPO_ROOT) / "audio"
#: The trees whose code runs in the proxy's process, each with the prefix its
#: files are listed under: a path from ``proxy/``.
_TREES = ((PROXY, ""), (AUDIO, "../audio/"))

_SUBPROCESS = frozenset({"Popen", "run", "call", "check_output", "check_call", "getoutput", "getstatusoutput"})
_OS = frozenset({"execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
                 "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                 "posix_spawn", "posix_spawnp", "fork", "forkpty", "system", "popen", "openpty"})
_PTY = frozenset({"fork", "openpty", "spawn"})
#: The modules whose starts are told by the module's name, as an import binds it.
_STARTS = {"subprocess": _SUBPROCESS, "os": _OS, "pty": _PTY}
# The async starts (asyncio's, a loop's own, anyio's) and the tool client's
# own start, and the names a start inside the proxy would carry: a
# terminal's, a piped engine's, the two builders of a sandbox's command and
# the two boot probes of a sandbox.
_BARE = frozenset({"create_subprocess_exec", "create_subprocess_shell", "subprocess_exec", "subprocess_shell",
                   "run_process", "open_process", "stdio_client",
                   "spawn_pty", "spawn_piped", "build_command_prefix", "build_command",
                   "netns_preflight", "tmpfs_cap_preflight"})
# The runners that start a build step or a script: an attribute, or the bare
# name called or handed on (a variable of the same name is no runner).
_RUNNERS = frozenset({"run_unconfined", "_run_bounded", "run_local", "build_local"})
_SANDBOX_PROGRAMS = frozenset({"bwrap", "pasta", "unshare", "oto-sandbox-net"})

STAYS = "stays"

#: ``(file as a path from proxy/, what it names)`` -> ``(stays or goes, what
#: it runs, the function of each site)``; ``<module>`` for a site outside a
#: function.
SITES: dict[tuple[str, str], tuple[str, str, tuple[str, ...]]] = {
    # ── what the control plane itself runs ──────────────────────────────────
    ("api/auth/openai_oauth.py", "create_subprocess_exec"): (
        STAYS, "an engine's sign-in (codex login)", ("oauth_start",)),
    ("services/apps/app_deploy.py", "subprocess.run"): (
        STAYS, "the syntax check of an app's script at deploy", ("step_script_problem",)),
    ("services/infra/git_writer.py", "subprocess.run"): (
        STAYS, "git, on the histories in the proxy's own data: it is never given a folder a session writes",
        ("_git",)),
    ("services/infra/storage_quota.py", "subprocess.run"): (STAYS, "the quota helper", ("_run_helper",)),
    ("services/media/media_pipeline.py", "subprocess.run"): (
        STAYS, "ffmpeg and ffprobe, through one door", ("_run_media_tool",)),
    ("services/mcp/compose_rewrite.py", "subprocess.run"): (
        STAYS, "docker network reads, for the Docker tools", ("_collect_used_subnets", "_collect_used_subnets")),
    ("services/mcp/docker_manager.py", "subprocess.Popen"): (
        STAYS, "docker compose, for the Docker tools", ("_LiveRun", "_end_group", "_exited", "_run_compose_streaming")),
    ("services/mcp/docker_manager.py", "subprocess.run"): (
        STAYS, "docker and docker compose, for the Docker tools",
        ("_build_state", "_compose_up_attempts", "_engine_api_version", "_image_id",
         "_settle_interrupted_recreate", "_settle_interrupted_recreate", "_warn_foreign_mcp_leftovers",
         "container_state", "remove_container", "remove_image", "start_container", "stop_container")),
    ("services/containers/lifecycle.py", "subprocess.run"): (
        STAYS, "docker compose stop, a Docker tool put to sleep", ("sleep_container",)),
    ("services/mcp/mcp_output_relocation.py", "subprocess.Popen"): (
        STAYS, "docker cp, a Docker tool's outputs", ("_docker_cp",)),
    ("services/mcp/mcp_venv_bootstrap.py", "subprocess.run"): (STAYS, "node --version", ("_node_major",)),
    # ── code under proxy/ that runs elsewhere ───────────────────────────────
    ("compute_start.py", "os.execve"): (STAYS, "the compute machine's own start: it becomes the satellite", ("main",)),
    ("compute_start.py", "subprocess.run"): (
        STAYS, "the compute machine's own start: its two checks", ("check", "main")),
    ("core/stdio_path_interceptor.py", "subprocess.Popen"): (
        STAYS, "runs inside a session, around its tool's program", ("StandIn.__init__", "StandIn._start", "main")),
    ("hooks/credential_helper.py", "os.execve"): (STAYS, "runs inside a session: git and gh", ("_run",)),
    ("hooks/credential_helper.py", "subprocess.run"): (
        STAYS, "runs inside a session: git and gh", ("_remotes", "_run")),
    # ── the sources of the satellite's vendored copies ──────────────────────
    ("core/layers/codex/app_server_client.py", "create_subprocess_exec"): (
        STAYS, "the satellite's Codex client, vendored from here", ("AppServerClient._taskkill_tree", "AppServerClient.start")),
    ("core/layers/codex/app_server_client.py", "argv:bwrap"): (
        STAYS, "the satellite's Codex client, vendored from here", ("<module>",)),
    ("core/sandbox/sandbox_render.py", "subprocess.run"): (
        STAYS, "the one render's check, its size probe and its pasta version read, vendored to machines",
        ("_first_line", "preflight", "probe_bwrap_size")),
    ("core/sandbox/sandbox_render.py", "argv:bwrap"): (STAYS, "the one render, vendored to machines", ("render",)),
    ("core/sandbox/sandbox_render.py", "argv:pasta"): (
        STAYS, "the one render's check, vendored to machines", ("preflight",)),
    ("services/infra/outbound_url.py", "subprocess.run"): (STAYS, "ip route, vendored to machines", ("read_onlink",)),
    ("services/mcp/mcp_installer.py", "create_subprocess_exec"): (
        STAYS, "a build step's child: vendored to machines; here the platform's tool build and the image build",
        ("_run_bounded",)),
    ("services/mcp/mcp_installer.py", "_run_bounded"): (
        STAYS, "the unconfined build runner: a paired computer's build and the image build's, vendored",
        ("run_unconfined",)),
    ("services/mcp/mcp_installer.py", "subprocess.run"): (
        STAYS, "taskkill on a Windows machine, vendored", ("_kill_tree",)),
    ("services/mcp/mcp_system_deps.py", "subprocess.run"): (
        STAYS, "vendored to machines, never called here",
        ("_is_package_installed", "_is_package_installed", "_is_package_installed", "_is_package_installed",
         "_node_version", "install_system_requirements")),
    ("services/mcp/mcp_venv_bootstrap.py", "run_unconfined"): (
        STAYS, "the bundled tools' build, in the image build's stage and an install's build command: never the server",
        ("_build_python", "_rebuild_node_addons")),
}


_NO_BINDINGS: dict[str, str] = {}
_NO_EXPORTS: dict[str, dict[str, str]] = {}


def _named(node: ast.AST, bound: dict[str, str] = _NO_BINDINGS,
           exports: dict[str, dict[str, str]] = _NO_EXPORTS) -> str | None:
    """What ``node`` names of a process start, ``bound`` being its module's
    imports (``_bindings``) and ``exports`` every module's of the tree, by
    import name, so a start one module imported is seen where another module
    takes it from there."""
    if isinstance(node, ast.Attribute):
        base = node.value.id if isinstance(node.value, ast.Name) else None
        module = bound.get(base, base) if base else None
        attr = node.attr
        if module:
            module, _, attr = names.through(f"{module}.{attr}", exports).rpartition(".")
        if module in _STARTS and attr in _STARTS[module]:
            return f"{module}.{attr}"
        if attr in _BARE or attr in _RUNNERS:
            return attr
    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        module, _, name = names.through(bound.get(node.id, node.id), exports).rpartition(".")
        if module in _STARTS and name in _STARTS[module]:
            return f"{module}.{name}"
        if name in _BARE or name == "Popen":
            return name
    elif isinstance(node, ast.Call):
        handed = [names.through(bound.get(n.id, n.id), exports).rpartition(".")[2]
                  for n in (node.func, *node.args) if isinstance(n, ast.Name)]
        named = [name for name in handed if name in _RUNNERS]
        return named[0] if named else None
    elif (isinstance(node, (ast.List, ast.Tuple)) and node.elts and isinstance(node.elts[0], ast.Constant)
          and isinstance(node.elts[0].value, str) and PurePosixPath(node.elts[0].value).name in _SANDBOX_PROGRAMS):
        return f"argv:{PurePosixPath(node.elts[0].value).name}"
    return None


class _Sites(ast.NodeVisitor):
    """Each site of one module, with the function it sits in."""

    def __init__(self, bound: dict[str, str], exports: dict[str, dict[str, str]]) -> None:
        self.found: list[tuple[str, str]] = []
        self._bound = bound
        self._exports = exports
        self._scope: list[str] = []

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def generic_visit(self, node: ast.AST) -> None:
        name = _named(node, self._bound, self._exports)
        if name:
            self.found.append((name, ".".join(self._scope) or "<module>"))
        super().generic_visit(node)


def _sites_of(tree: ast.AST, bound: dict[str, str], exports: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
    pass_ = _Sites(bound, exports)
    pass_.visit(tree)
    return pass_.found


def _sites_in(text: str) -> list[tuple[str, str]]:
    tree = ast.parse(text)
    return _sites_of(tree, names.bindings(tree), {})


@functools.cache
def _files() -> dict[str, Path]:
    """Each module of the trees, by its path from ``proxy/``."""
    return {key: path for root, prefix in _TREES
            for key, path in names.tree_files(root, prefix, _EXCLUDED_PARTS)}


def _parse(rel: str) -> ast.AST:
    return ast.parse(_files()[rel].read_text(encoding="utf-8"))


@functools.cache
def _bound() -> dict[str, dict[str, str]]:
    """Each module's imports, read once, one module's syntax in memory at a
    time: every tree held at once is about half a gigabyte, kept for the
    worker's life, and the suite's workers share one memory cap."""
    with names.paused_gc():
        return {rel: names.bindings(_parse(rel), rel) for rel in _files()}


@functools.cache
def _tree() -> dict[str, list[tuple[str, str]]]:
    """Each module's sites, read once, again one module at a time."""
    bound = _bound()
    exports = {names.module_of(rel): imports for rel, imports in bound.items()}
    with names.paused_gc():
        return {rel: _sites_of(_parse(rel), bound[rel], exports) for rel in _files()}


def _found(sources: dict[str, str] | None = None) -> dict[tuple[str, str], list[str]]:
    """Every site in the tree: ``(file, name)`` -> the function of each site,
    sorted. ``sources`` puts a module's text in place of the file's, or adds
    one; a module that imports from one of them is read again."""
    modules = dict(_tree())
    if sources:
        added = {}
        for rel, text in sources.items():
            tree = ast.parse(text)
            added[rel] = (tree, names.bindings(tree, rel))
        bound = {**_bound(), **{rel: imports for rel, (_tree_of, imports) in added.items()}}
        exports = {names.module_of(rel): imports for rel, imports in bound.items()}
        touched = {names.module_of(rel) for rel in added}
        for rel, imports in bound.items():
            if rel in added:
                modules[rel] = _sites_of(added[rel][0], imports, exports)
            elif any(target.rpartition(".")[0] in touched for target in imports.values()):
                modules[rel] = _sites_of(_parse(rel), imports, exports)
    sites: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for rel, found in modules.items():
        for name, function in found:
            sites[(rel, name)].append(function)
    return {key: sorted(functions) for key, functions in sites.items()}


def _unlisted(found: dict[tuple[str, str], list[str]]) -> tuple[list[str], list[str]]:
    """The sites the list lacks, and the listed ones the code lacks."""
    new, gone = [], []
    for key in sorted(set(found) | set(SITES)):
        have = collections.Counter(found.get(key, []))
        listed = collections.Counter(SITES[key][2] if key in SITES else ())
        new += [f"{key[0]} {function} {key[1]}" for function in sorted((have - listed).elements())]
        gone += [f"{key[0]} {function} {key[1]}" for function in sorted((listed - have).elements())]
    return new, gone


def test_every_process_start_in_the_proxy_is_on_the_list():
    new, gone = _unlisted(_found())
    assert not new, (
        "a proxy module starts a process, or builds a sandbox's command, and is not on the list. "
        "A session, a tool, an app's server, a step and a build run on a machine, through the "
        f"satellite: {new}")
    assert not gone, f"listed and no longer in the code, remove it: {gone}"


def _names(source: str) -> list[str]:
    """What the pass names in ``source``, once per site, sorted."""
    return sorted(name for name, _function in _sites_in(source))


def test_the_pass_sees_a_handed_on_start_and_a_literal_command():
    assert _names("import asyncio, subprocess\n"
                  "async def f():\n"
                  "    await asyncio.to_thread(subprocess.run, ['git', 'status'])\n"
                  "    cmd = ['bwrap', '--unshare-all']\n"
                  "    await asyncio.create_subprocess_exec(*cmd)\n"
                  "    run_local = True\n"
                  "    if run_local:\n"
                  "        await asyncio.to_thread(build_local, cmd)\n") == [
        "argv:bwrap", "build_local", "create_subprocess_exec", "subprocess.run"]


_SPELLINGS = {
    # The six spellings a review found the pass blind to.
    "an aliased subprocess": ("import subprocess as sp\n\ndef f(cmd):\n    return sp.Popen(cmd)\n", ["subprocess.Popen"]),
    "a name imported from subprocess": (
        "from subprocess import check_output\n\ndef f(cmd):\n    return check_output(cmd)\n",
        ["subprocess.check_output"]),
    "a loop's own start": (
        "async def f(loop, proto):\n    await loop.subprocess_exec(proto, 'bwrap', '--version')\n", ["subprocess_exec"]),
    "os.spawnv of a program by its path": (
        "import os\n\ndef f():\n    os.spawnv(os.P_NOWAIT, '/usr/bin/bwrap', ['/usr/bin/bwrap', '--version'])\n",
        ["argv:bwrap", "os.spawnv"]),
    "an aliased pty": ("import pty as p\n\ndef f():\n    return p.fork()\n", ["pty.fork"]),
    "anyio's run_process": (
        "import anyio\n\nasync def f(cmd):\n    return await anyio.run_process(cmd)\n", ["run_process"]),
    # The same rules, by their other spellings.
    "an aliased os": ("import os as _os\n\ndef f(path, argv):\n    _os.execv(path, argv)\n", ["os.execv"]),
    "a name imported from os under another": (
        "from os import posix_spawn as start\n\ndef f(path, argv, env):\n    return start(path, argv, env)\n",
        ["os.posix_spawn"]),
    "a name imported from pty": ("from pty import openpty\n\ndef f():\n    return openpty()\n", ["pty.openpty"]),
    "an imported start handed on": (
        "import asyncio\nfrom subprocess import run as go\n\nasync def f(cmd):\n"
        "    return await asyncio.to_thread(go, cmd)\n", ["subprocess.run"]),
    "asyncio's start under another name": (
        "from asyncio import create_subprocess_exec as start\n\nasync def f(cmd):\n    return await start(*cmd)\n",
        ["create_subprocess_exec"]),
    "a loop's shell start": (
        "async def f(loop, proto, line):\n    await loop.subprocess_shell(proto, line)\n", ["subprocess_shell"]),
    "anyio's open_process": (
        "import anyio\n\nasync def f(cmd):\n    return await anyio.open_process(cmd)\n", ["open_process"]),
    "a runner under another name": (
        "from services.scripts.runner import run_local as go\n\nasync def f(spec):\n    return await go(spec)\n",
        ["run_local"]),
    "a runner from a relative import": (
        "from .runner import build_local as go\n\nasync def f(spec):\n    return await go(spec)\n", ["build_local"]),
    "os.spawnlpe": ("import os\n\ndef f(argv):\n    os.spawnlpe(os.P_WAIT, 'git', *argv)\n", ["os.spawnlpe"]),
    "a command tuple with a path to pasta": (
        "def f(extra):\n    return ('/usr/local/bin/pasta', '--quiet', *extra)\n", ["argv:pasta"]),
    # The two shell readers a narrow pass found the pass blind to.
    "subprocess.getoutput": ("import subprocess\n\ndef f(line):\n    return subprocess.getoutput(line)\n",
                             ["subprocess.getoutput"]),
    "getstatusoutput imported": (
        "from subprocess import getstatusoutput\n\ndef f(line):\n    return getstatusoutput(line)\n",
        ["subprocess.getstatusoutput"]),
    "getoutput under an aliased module": (
        "import subprocess as sp\n\ndef f(line):\n    return sp.getoutput(line)\n", ["subprocess.getoutput"]),
    # What is no start: a name of the same spelling that no import binds to a
    # process module, a path that only ends like a program, an import alone.
    "an aliased os that starts nothing": (
        "import os as _os\n\ndef f(path):\n    return _os.path.exists(path), _os.environ.get('X')\n", []),
    "the same words on another object": (
        "def f(job, items):\n    job.run(items)\n    spawnv = fork = 1\n    return ['bwrap.log', spawnv, fork]\n", []),
    "an import alone": ("from subprocess import CalledProcessError, run\n", []),
}


@pytest.mark.parametrize(("source", "named"), list(_SPELLINGS.values()), ids=list(_SPELLINGS))
def test_the_pass_sees_the_common_spellings_of_a_start(source, named):
    assert _names(source) == named


def test_the_audio_package_runs_in_the_proxy_and_is_read_too():
    """``ws/audio.py`` imports it, so a start there is a start of the
    proxy's own process."""
    if not AUDIO.is_dir():
        pytest.skip("no audio package beside the proxy")
    assert any(rel.startswith("../audio/") for rel in _tree())
    new, gone = _unlisted(_found({"../audio/providers/a_new_voice.py": (
        "import subprocess as sp\n\n\ndef speak(cmd):\n    return sp.run(cmd)\n")}))
    assert new == ["../audio/providers/a_new_voice.py speak subprocess.run"] and gone == []


def _with_a_second_start(rel: str, function: str, call: str) -> str:
    """The module's text with one more statement at the end of ``function``."""
    tree = ast.parse((PROXY / rel).read_text(encoding="utf-8"))
    [target] = [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function]
    target.body.append(ast.parse(call).body[0])
    return ast.unparse(tree)


def test_a_second_start_in_a_listed_function_is_seen():
    rel = "api/auth/openai_oauth.py"
    new, gone = _unlisted(_found({rel: _with_a_second_start(
        rel, "oauth_start", "asyncio.create_subprocess_exec('codex', 'logout')")}))
    assert new == [f"{rel} oauth_start create_subprocess_exec"] and gone == []


@pytest.mark.parametrize("caller", [
    "from core.zz_starts import P\n\n\ndef go(cmd):\n    return P(cmd)\n",
    "from core import zz_starts\n\n\ndef go(cmd):\n    return zz_starts.P(cmd)\n",
    "import core.zz_starts as starts\n\n\ndef go(cmd):\n    return starts.P(cmd)\n",
    "from core.zz_starts import go_on as P\n\n\ndef go(cmd):\n    return P(cmd)\n",
], ids=["the-name", "through-the-module", "the-module-aliased", "a-second-hop"])
def test_a_start_imported_from_a_proxy_module_that_imported_it_is_seen(caller):
    """A module that imports a start under a name of its own hands it on to
    every module that imports that name from it: the second module starts the
    process. A name is followed through the tree's own modules, so the start
    is seen where it is called."""
    new, gone = _unlisted(_found({
        "core/zz_starts.py": "from subprocess import Popen as P\nfrom core.zz_starts import P as go_on\n",
        "core/zz_caller.py": caller}))
    assert new == ["core/zz_caller.py go subprocess.Popen"] and gone == []


def test_a_new_caller_of_a_runner_is_seen():
    new, gone = _unlisted(_found({"services/new_door.py": (
        "from services.scripts import runner\n"
        "async def check(spec):\n"
        "    return await runner.run_local(spec)\n")}))
    assert new == ["services/new_door.py check run_local"] and gone == []


def test_an_engine_class_holds_no_session():
    """The two engines that run a process are engines alone: neither is a
    layer that holds sessions, and neither defines a session method of the
    layer's contract (a start, a turn, a stop, a close). The holders are
    the remote layer and Direct LLM's."""
    from core.engine import Engine
    from core.execution_layer import ExecutionLayer
    from core.layers.cli.layer import CLIExecutionLayer
    from core.layers.codex.layer import CodexCLIExecutionLayer
    from core.session import session_manager
    session_methods = {name for name in vars(ExecutionLayer) if not name.startswith(("__", "_abc_"))}
    assert {"start_session", "_send_turn", "abort", "close_session", "owns_session"} <= session_methods
    for cls in (CLIExecutionLayer, CodexCLIExecutionLayer):
        assert issubclass(cls, Engine) and not issubclass(cls, ExecutionLayer)
        own = {name for klass in cls.__mro__ if klass not in (Engine, object) and klass.__module__ != "abc"
               for name in vars(klass)}
        assert not own & session_methods, f"{cls.__name__} defines {sorted(own & session_methods)}"
    assert set(session_manager.holding_engines()) == {"direct-llm"}
