"""The floor's differential corpus: the command gate (``auth/path_shell.py``),
the path policy (``auth/path_policy.py``) and the permission authority
(``api/hooks/permission.py``) asked every question the rest of the suite asks
them and every form the permission docs give, and compared with the answers
the gate gave when the corpus was recorded.

``tests/fixtures/floor_corpus/inputs.json`` holds the questions: the
commands, segments, paths, tool calls and contexts the suite's own tests hand
the gate (recorded from a run of the whole suite), the documented forms, and
the cases whose recorded answer is questioned. The ``verdicts-*.json`` files
hold the answers, the distinct ones in a table. A refactor of the three
modules changes no answer; a questioned answer stays as recorded until a
commit that changes it says why. ``FLOOR_CORPUS_WRITE=1`` records them again
(and fails the run, so a rewrite never passes silently).

Paths are stored against placeholders (``{AGENTS}``, ``{PROXY}``, ``{HOME}``)
and replayed against this run's own roots. The registry's protected
credential folders are pinned to the one the bundled manifests declare; the
working directory and ``HOME`` are pinned too. A case that names the test
process's own descriptors (``/proc/self``, ``/dev/stderr``) is compared only
when pytest captures by descriptor (its default): those paths resolve to
where pytest points the streams. A case that names the home of an account
this machine has (``~name``) is never compared: the record was made where
that account did not exist."""
import asyncio
import copy
import dataclasses
import hashlib
import itertools
import json
import os
import pwd
import re

import pytest

from api.hooks import permission, routing
from auth import path_policy, path_shell, roles
from auth.path_policy import PathDecision, SecurityContext
from core.permission_modes import terminal_mode
from core.placement import KIND_ADMIN_REMOTE, KIND_USER_REMOTE, PlacementCapabilities
from core.session import session_kind
from tests._paths import PROXY_DIR

DATA = PROXY_DIR / "tests" / "fixtures" / "floor_corpus"
WRITE = os.environ.get("FLOOR_CORPUS_WRITE") == "1"
AGENT = "corpus-agent"
OWN = "alice"
OTHER = "bob"
_PROTECTED_CREDENTIAL_DIRS = frozenset({"google-tokens"})
_PROCESS_RELATIVE = ("/proc/self", "/proc/thread-self", "/dev/std", "/dev/fd/")
_FLAG_NAMES = ("is_remote", "depth", "dangerous_only", "remote_host", "host_asks")


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------

def _roots() -> list[tuple[str, str]]:
    pairs = [
        (str(path_policy._PROXY_DIR), "{PROXY}"),
        (str(path_policy._AGENTS_DIR), "{AGENTS}"),
        (str(path_policy._HOME), "{HOME}"),
    ]
    return sorted((p for p in pairs if p[0] not in ("", "/")), key=lambda p: -len(p[0]))


def _to_placeholders(text: str) -> str:
    for real, mark in _roots():
        text = text.replace(real, mark)
    return text


def _real(value):
    """An input with this run's roots in place of the placeholders."""
    if isinstance(value, str):
        for real, mark in _roots():
            value = value.replace(mark, real)
        return value
    if isinstance(value, list):
        return [_real(v) for v in value]
    if isinstance(value, dict):
        return {k: _real(v) for k, v in value.items()}
    return value


def _set_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return repr(value)


def _plain(value) -> str:
    """A verdict as canonical JSON with this run's roots as placeholders."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return _to_placeholders(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                       default=_set_default))


def _decision(d: PathDecision, *extra) -> str:
    return _plain([d.allowed, d.reason, d.permission_tier, d.destructive, d.updated_input, *extra])


def _answer(fn, *args, **kwargs) -> str:
    """The gate's answer to one question, or what it raised (a raise is an
    answer the corpus records too)."""
    try:
        value = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001  # the raise is the recorded answer
        return _err(exc)
    if isinstance(value, tuple) and value and isinstance(value[0], PathDecision):
        return _decision(value[0], value[1])
    if isinstance(value, PathDecision):
        return _decision(value)
    return _plain(value)


def _flags(values: list) -> dict:
    return dict(zip(_FLAG_NAMES, values, strict=True))


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------

_TUPLE_FIELDS = ("session_allowed_roots", "available_scopes", "knowledge_libraries", "ssh_hosts")
# The SSH hosts the hosts columns carry (``SecurityContext.ssh_hosts``) and the
# hook's reader answers with.
SSH_HOSTS = (("server.lan", "deploy"),)


def _context(data: dict) -> SecurityContext:
    """A recorded context back as a ``SecurityContext`` (the recorded form
    keeps only the fields that differ from the defaults)."""
    data = _real(copy.deepcopy(data))
    place = data.pop("placement", {})
    place["device_grants"] = set(place.get("device_grants") or ())
    for name in _TUPLE_FIELDS:
        if name in data:
            data[name] = tuple(tuple(v) if isinstance(v, list) else v for v in data[name])
    return SecurityContext(placement=PlacementCapabilities(**place), **data)


def _remote(kind: str, *, full_fs: bool, home: str = "/home/office", os_name: str = "linux",
            agents_dir: str = "", runtime: str = "") -> PlacementCapabilities:
    return PlacementCapabilities(
        kind=kind, machine_id="corpus-machine", label="office", os=os_name, home_dir=home,
        agents_dir=agents_dir or f"{home}/.oto-dock/agents", os_user="office",
        allow_full_fs=full_fs, claude_runtime_root=runtime,
    )


def _matrix() -> dict[str, SecurityContext]:
    """The fixed contexts every command and path is asked under."""
    def ctx(role, **kw):
        base = {"role": role, "username": OWN, "agent": AGENT, "is_admin_agent": False}
        base.update(kw)
        return SecurityContext(**base)

    caller = str(path_policy._AGENTS_DIR / AGENT / "externals" / "phone" / "caller1")
    libs = (("lib", "", True), ("other", "sub", False))
    user_home = _remote(KIND_USER_REMOTE, full_fs=False)
    admin_full = _remote(KIND_ADMIN_REMOTE, full_fs=True)
    admin_home = _remote(KIND_ADMIN_REMOTE, full_fs=False)
    windows = _remote(KIND_USER_REMOTE, full_fs=False, home="C:/Users/frank", os_name="windows",
                      agents_dir="C:/Users/frank/OtoDock/agents")
    runtime = _remote(KIND_USER_REMOTE, full_fs=False, runtime="/tmp/claude-1000")
    return {
        "viewer": ctx(roles.VIEWER),
        "contributor": ctx(roles.CONTRIBUTOR),
        "editor": ctx(roles.EDITOR),
        "manager": ctx(roles.MANAGER),
        "admin": ctx(roles.ADMIN),
        "admin-agent": ctx(roles.ADMIN, is_admin_agent=True),
        "judge": ctx(roles.ADMIN, is_admin_agent=True, read_only=True),
        "judge-manager": ctx(roles.MANAGER, read_only=True),
        "service": ctx(roles.MANAGER, username="", session_scope="agent"),
        "service-knowledge": ctx(roles.MANAGER, username="", session_scope="agent",
                                 knowledge_rw=True, knowledge_libraries=libs),
        "service-libraries": ctx(roles.MANAGER, username="", session_scope="agent",
                                 knowledge_libraries=libs),
        "shared-only": ctx(roles.EDITOR, session_scope="agent"),
        "shared-only-viewer": ctx(roles.VIEWER, session_scope="agent"),
        "libraries": ctx(roles.MANAGER, knowledge_libraries=libs),
        "libraries-editor": ctx(roles.EDITOR, knowledge_libraries=libs),
        "external": ctx(roles.VIEWER, username="", session_scope="agent", principal="external",
                        external_channel="phone", external_id="caller1", external_home=caller),
        "cli-session": ctx(roles.MANAGER, cli_session_id="11111111-2222-4333-8444-555555555555"),
        "manager-ssh-hosts": ctx(roles.MANAGER, ssh_hosts=SSH_HOSTS),
        "editor-ssh-hosts": ctx(roles.EDITOR, ssh_hosts=SSH_HOSTS),
        "contributor-ssh-hosts": ctx(roles.CONTRIBUTOR, ssh_hosts=SSH_HOSTS),
        "remote-user-editor": ctx(roles.EDITOR, placement=user_home),
        "remote-user-viewer": ctx(roles.VIEWER, placement=user_home),
        "remote-full-editor": ctx(roles.EDITOR, placement=admin_full),
        "remote-full-viewer": ctx(roles.VIEWER, placement=admin_full),
        "remote-full-contributor": ctx(roles.CONTRIBUTOR, placement=admin_full),
        "remote-full-manager": ctx(roles.MANAGER, placement=admin_full),
        "remote-full-admin-agent": ctx(roles.ADMIN, is_admin_agent=True, placement=admin_full),
        "remote-home-manager": ctx(roles.MANAGER, placement=admin_home),
        "remote-home-contributor": ctx(roles.CONTRIBUTOR, placement=admin_home),
        "remote-windows-editor": ctx(roles.EDITOR, placement=windows),
        "remote-otodock-cli": ctx(roles.EDITOR, placement=user_home,
                                  session_allowed_roots=("/home/office/project",),
                                  work_cwd="/home/office/project"),
        "remote-runtime": ctx(roles.EDITOR, placement=runtime,
                              cli_session_id="11111111-2222-4333-8444-555555555555"),
        "remote-external": ctx(roles.VIEWER, username="", session_scope="agent",
                               principal="external", external_channel="phone",
                               external_id="caller1", placement=user_home),
    }


def _host_paths() -> list[str]:
    """Resolved host paths for the write check, against the matrix's agent."""
    a = f"{{AGENTS}}/{AGENT}"
    caller = f"{a}/externals/phone/caller1"
    return [
        f"{a}/workspace/x.md", f"{a}/workspace", f"{a}/workspace/.env",
        f"{a}/workspace/google-tokens/a.json", f"{a}/workspace/repo/.claude/settings.json",
        f"{a}/users/{OWN}", f"{a}/users/{OWN}/notes.md", f"{a}/users/{OWN}/workspace/x.md",
        f"{a}/users/{OWN}/context/c.md", f"{a}/users/{OWN}/context/memory/m.md",
        f"{a}/users/{OWN}/.claude/settings.json", f"{a}/users/{OWN}/.claude/dev-mods/x.py",
        f"{a}/users/{OWN}/.codex/config.toml", f"{a}/users/{OWN}/.codex/auth.json",
        f"{a}/users/{OWN}/.credentials/google-tokens/a.json", f"{a}/users/{OWN}/.git/config",
        f"{a}/users/{OTHER}/workspace/x.md", f"{a}/users/{OTHER}",
        f"{a}/knowledge/ref.md", f"{a}/knowledge/memory/m.md", f"{a}/knowledge/shared",
        f"{a}/knowledge/shared/lib/x.md", f"{a}/knowledge/shared/lib/sub/x.md",
        f"{a}/knowledge/shared/other/x.md", f"{a}/knowledge/shared/other/sub/x.md",
        f"{a}/knowledge/shared/libx/x.md", f"{a}/config/agent.md", f"{a}/config/context/c.md",
        f"{a}/.env", caller, f"{caller}/notes.md", f"{caller}/workspace/x.md",
        f"{caller}/context/c.md", f"{caller}/context/memory/m.md",
        f"{a}/externals/phone/caller2/workspace/x.md", "{AGENTS}/other-agent/workspace/x.md",
        "{HOME}/.claude/settings.json", "{HOME}/.claude/settings.local.json",
        "{HOME}/.claude/rules/r.md", "{HOME}/.claude/plans/p.md", "{HOME}/.ssh/id_ed25519",
        "{PROXY}/config.py", "{PROXY}/sessions/index.json",
        "/tmp/x", "/tmp/.claude.json", "/tmp/.claude/settings.json", "/tmp/.codex/config.toml",
        "/tmp/claude-1000/-workspace/s1/tasks/t1.output", "/etc/passwd", "/",
    ]


def _virtual_paths() -> list[str]:
    """Tool path arguments as an agent writes them: sandbox-virtual, relative,
    ``~``, a machine's own paths."""
    return [
        "/workspace/x.md", "/workspace/", "/workspace//etc/passwd", "/workspace/.env",
        "/workspace/.claude/settings.json", "/workspace/.codex/config.toml",
        "/workspace/repo/.claude/settings.json", "/workspace/google-tokens/a.json",
        f"/users/{OWN}/workspace/x.md", f"/users/{OWN}/notes.md", f"/users/{OWN}",
        f"/users/{OWN}/context/memory/m.md", f"/users/{OWN}/.claude/settings.json",
        f"/users/{OWN}/.codex/auth.json", f"/users/{OWN}/.credentials/google-tokens/a.json",
        f"/users/{OTHER}/workspace/x.md", f"/users/{OTHER}/",
        "/knowledge/ref.md", "/knowledge/memory/m.md", "/knowledge/shared",
        "/knowledge/shared/lib/x.md", "/knowledge/shared/other/sub/x.md",
        "/knowledge/shared/other/x.md", "/knowledge/.credentials/google-tokens/a.json",
        "/config/agent.md", "/config/context/c.md", "/caller/workspace/x.md",
        "/caller/context/c.md", "/caller/notes.md", "/caller/.claude/settings.json",
        "/tmp/x", "/tmp/.claude.json", "/tmp/.codex/config.toml",
        "/tmp/claude-1000/-workspace/s1/tasks/t1.output",
        "/tmp/claude-1000/-workspace/11111111-2222-4333-8444-555555555555/tasks/t1.output",
        "~/.claude/plans/p.md", "~/.claude/settings.json", "~/.ssh/id_ed25519", "~/x",
        "../x", "x.md", "workspace/x.md", "project/x.md", f"{AGENT}/users/{OWN}/workspace/x.md",
        "/etc/passwd", "/home/office/Desktop/x", "/home/office/.ssh/config",
        "/home/office/project/x.md", "/home/office/.oto-dock/agents/corpus-agent/workspace/x.md",
        "/home/office/.oto-dock/agents/corpus-agent/users/bob/x.md",
        "/home/office/.oto-dock/state/x", "/srv/data/x", "C:/Users/frank/x",
        "C:\\Users\\frank\\Desktop\\x.txt",
        "C:/Users/frank/OtoDock/agents/corpus-agent/workspace/x.md",
    ]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

_inputs_cache: dict = {}


def _inputs() -> dict:
    if not _inputs_cache:
        _inputs_cache.update(json.loads((DATA / "inputs.json").read_text()))
    return _inputs_cache


def _case_id(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


class _Book:
    """One family's answers: per case, one answer per column (the matrix's
    contexts, or a single column)."""

    def __init__(self, family: str, columns: list[str] | None = None):
        self.family = family
        self.columns = columns
        self.got: dict[str, list[str]] = {}
        self.shown: dict[str, str] = {}
        self.optional: set[str] = set()
        self.skip: set[str] = set()

    def add(self, case, answers: list[str], *, optional: bool = False,
            skip: bool = False) -> None:
        """``optional``: compared only when the run says so (descriptor
        capture); ``skip``: never compared on this machine."""
        cid = _case_id(case)
        self.got[cid] = answers
        self.shown[cid] = json.dumps(case, ensure_ascii=False)[:300]
        if optional:
            self.optional.add(cid)
        if skip:
            self.skip.add(cid)

    def check(self, *, compare_optional: bool) -> None:
        path = DATA / f"verdicts-{self.family}.json"
        if WRITE:
            table: list[str] = []
            index: dict[str, int] = {}
            cases = {}
            for cid in sorted(self.got):
                row = []
                for answer in self.got[cid]:
                    if answer not in index:
                        index[answer] = len(table)
                        table.append(answer)
                    row.append(index[answer])
                cases[cid] = row
            body = {"columns": self.columns or ["answer"], "verdicts": table, "cases": cases}
            path.write_text(_dump(body))
            pytest.fail(f"{path.name} written; run again without FLOOR_CORPUS_WRITE")
        assert path.exists(), f"no record for {self.family}: FLOOR_CORPUS_WRITE=1 writes it"
        rec = json.loads(path.read_text())
        assert rec["columns"] == (self.columns or ["answer"]), "the context columns changed"
        want = {cid: [rec["verdicts"][i] for i in row] for cid, row in rec["cases"].items()}
        skipped = self.skip | (set() if compare_optional else self.optional)
        missing = sorted(set(want) - set(self.got) - skipped)
        extra = sorted(set(self.got) - set(want))
        changed = [c for c in sorted(want.keys() & self.got.keys() - skipped) if want[c] != self.got[c]]
        lines = [f"{self.family}: {len(changed)} changed, {len(missing)} missing, {len(extra)} new"]
        for cid in changed[:20]:
            lines.append(f"  {self.shown[cid]}")
            for k, (w, g) in enumerate(zip(want[cid], self.got[cid], strict=True)):
                if w != g:
                    col = (self.columns or ["answer"])[k]
                    lines.append(f"    [{col}] recorded: {w}\n    [{col}] now:      {g}")
        lines += [f"  missing: {c}" for c in missing[:5]] + [f"  new: {self.shown[c]}" for c in extra[:5]]
        assert not (changed or missing or extra), "\n".join(lines)


def _dump(body: dict) -> str:
    cases = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, separators=(',', ':'))}"
                       for k, v in body["cases"].items())
    table = ",\n".join(f"  {json.dumps(v, ensure_ascii=False)}" for v in body["verdicts"])
    return ("{\n \"columns\": " + json.dumps(body["columns"]) + ",\n \"verdicts\": [\n" + table
            + "\n ],\n \"cases\": {\n" + cases + "\n }\n}\n")


_HOME_FORM = re.compile(r"~([A-Za-z_][\w-]*)")


def _descriptor_relative(value) -> bool:
    """A case that names the test process's own descriptors or directory:
    its answer follows where pytest points the streams."""
    text = json.dumps(value)
    return any(mark in text for mark in _PROCESS_RELATIVE)


def _home_present(value) -> bool:
    """A case that names the ``~name`` home of an account this machine has:
    the record was made where it had none, so the answer differs here."""
    text = json.dumps(value)
    for name in _HOME_FORM.findall(text):
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        return True
    return False


@pytest.fixture
def floor(monkeypatch, request):
    """The pins every family runs under; answers whether descriptor-relative
    cases are compared in this run."""
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_protected_credentials_subpaths",
                        lambda: _PROTECTED_CREDENTIAL_DIRS)
    monkeypatch.chdir(PROXY_DIR)
    monkeypatch.setenv("HOME", str(path_policy._HOME))
    return request.config.getoption("capture") == "fd"


def _err(exc: Exception) -> str:
    return _plain(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# The command gate
# ---------------------------------------------------------------------------

def test_the_matrix_and_its_record_agree():
    assert len(_matrix()) == len(set(_matrix())) >= 30


def test_a_home_this_machine_has_is_never_compared(tmp_path, monkeypatch):
    """A ``~name`` case whose account exists here is skipped under either
    capture mode; a descriptor case is compared only under descriptor
    capture."""
    if WRITE:
        pytest.skip("recording")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: object())
    assert _home_present(["ls", "ls ~user/x"]) and not _descriptor_relative("ls ~user/x")
    assert _descriptor_relative("cat /proc/self/status") and not _home_present("cat /tmp/x")
    monkeypatch.setitem(globals(), "DATA", tmp_path)
    record = {"columns": ["answer"], "verdicts": ["recorded"],
              "cases": {_case_id("home"): [0], _case_id("fd"): [0]}}
    (tmp_path / "verdicts-probe.json").write_text(json.dumps(record))
    book = _Book("probe")
    book.add("home", ["other"], skip=True)
    book.add("fd", ["other"], optional=True)
    book.check(compare_optional=False)
    with pytest.raises(AssertionError, match="1 changed"):
        book.check(compare_optional=True)


def test_segments_split_as_recorded(floor):
    book = _Book("split")
    for cmd in _inputs()["split"]:
        got = _answer(path_shell._split_command_segments, _real(cmd))
        book.add(cmd, [got], optional=_descriptor_relative(cmd), skip=_home_present(cmd))
    book.check(compare_optional=floor)


def test_path_arguments_as_recorded(floor):
    book = _Book("path-args")
    for name, segment in _inputs()["path_args"]:
        got = _answer(path_shell._extract_path_args, name, _real(segment))
        book.add([name, segment], [got], optional=_descriptor_relative(segment),
                 skip=_home_present(segment))
    book.check(compare_optional=floor)


def test_commands_under_every_context_as_recorded(floor):
    matrix = _matrix()
    book = _Book("bash", list(matrix))
    for entry in _inputs()["commands"]:
        cmd = _real(entry["command"])
        answers = [_answer(path_shell._check_bash, cmd, ctx) for ctx in matrix.values()]
        book.add(entry["command"], answers, optional=_descriptor_relative(cmd),
                 skip=_home_present(cmd))
    book.check(compare_optional=floor)


def test_recorded_command_strings_as_recorded(floor):
    """Every (command, context, flags) the suite handed the recursive check,
    replayed exactly: the nested levels, the floor pass, another host's
    command."""
    data = _inputs()
    book = _Book("command-strings")
    for cmd, ctx_key, flags in data["command_strings"]:
        got = _answer(path_shell._check_command_string,
                      _real(cmd), _context(data["contexts"][ctx_key]), **_flags(flags))
        book.add([cmd, ctx_key, flags], [got], optional=_descriptor_relative(cmd),
                 skip=_home_present(cmd))
    book.check(compare_optional=floor)


def test_recorded_classifications_as_recorded(floor):
    data = _inputs()
    book = _Book("classify")
    for segment, ctx_key, flags in data["classify"]:
        got = _answer(path_shell._classify_command,
                      _real(segment), _context(data["contexts"][ctx_key]), **_flags(flags))
        book.add([segment, ctx_key, flags], [got], optional=_descriptor_relative(segment),
                 skip=_home_present(segment))
    book.check(compare_optional=floor)


# ---------------------------------------------------------------------------
# The path policy
# ---------------------------------------------------------------------------

def test_paths_under_every_context_as_recorded(floor):
    matrix = _matrix()
    book = _Book("paths", list(matrix))
    for raw in _host_paths():
        resolved = path_policy._resolve_path(_real(raw))
        book.add(["write", raw], [_answer(path_policy._check_write_path, resolved, ctx)
                                  for ctx in matrix.values()])
    for raw in _virtual_paths():
        for tool in ("Write", "Read", "Edit", "Delete"):
            book.add([tool, raw], [_answer(path_policy.check_tool_access, tool, {"file_path": raw}, ctx)
                                   for ctx in matrix.values()])
    book.check(compare_optional=floor)


def test_recorded_path_checks_as_recorded(floor):
    data = _inputs()
    book = _Book("path-checks")
    for raw, ctx_key in data["write_paths"]:
        resolved = path_policy._resolve_path(_real(raw))
        got = _answer(path_policy._check_write_path, resolved, _context(data["contexts"][ctx_key]))
        book.add(["write", raw, ctx_key], [got], optional=_descriptor_relative(raw),
                 skip=_home_present(raw))
    for raw, ctx_key, writing in data["path_arg_checks"]:
        got = _answer(path_policy._check_path_arg,
                      _real(raw), _context(data["contexts"][ctx_key]), writing=writing)
        book.add(["arg", raw, ctx_key, writing], [got], optional=_descriptor_relative(raw),
                 skip=_home_present(raw))
    book.check(compare_optional=floor)


def test_recorded_tool_calls_as_recorded(floor):
    data = _inputs()
    book = _Book("tool-calls")
    for tool, tool_input, ctx_key in data["tool_calls"]:
        got = _answer(path_policy.check_tool_access,
                      tool, _real(tool_input), _context(data["contexts"][ctx_key]))
        book.add([tool, tool_input, ctx_key], [got],
                 optional=_descriptor_relative(tool_input), skip=_home_present(tool_input))
    book.check(compare_optional=floor)


# ---------------------------------------------------------------------------
# The permission authority
# ---------------------------------------------------------------------------

#: An MCP tool's manifest tier, by server (the registry is not consulted).
#: ``blender`` is an app connector whose raw-code tool is ``sensitive``.
_MCP_TIERS = {"notes": "open", "crm": "standard", "mail": "sensitive", "bank": "critical",
              "computer": "standard", "browser": "standard", "blender": "sensitive"}
#: The device MCPs and the capability each needs granted.
_DEVICE_CAPS = {"computer": "computer", "browser": "browser", "blender": "app"}
_CLIENTS = (session_kind.DASHBOARD.name, session_kind.TASK.name, session_kind.PHONE.name,
            "", session_kind.APP.name, session_kind.MEETING.name)
# The words before 1.9 (``default``, ``acceptEdits``) stay: a released
# install's stored row still reads as its current mode.
_MODES = ("default", "acceptEdits", "ask", "autoReview", "plan", "dontAsk", "auto", "judge")
_LIVE_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "auto", "dontAsk", "sideways")


def _permission_tools() -> list[tuple[str, dict]]:
    tools = [
        ("Read", {"file_path": "/workspace/x.md"}),
        ("Read", {"file_path": f"/users/{OTHER}/x.md"}),
        ("Glob", {"pattern": "**/*.md"}),
        ("Grep", {"pattern": "x", "path": "/workspace"}),
        ("Write", {"file_path": "/workspace/x.md", "content": "x"}),
        ("Write", {"file_path": "/tmp/.claude/plans/p.md", "content": "x"}),
        ("Write", {"file_path": "C:\\Users\\frank\\.claude\\plans\\p.md", "content": "x"}),
        ("Write", {"file_path": f"/users/{OWN}/.claude/settings.json", "content": "x"}),
        ("Edit", {"file_path": "/workspace/x.md", "old_string": "a", "new_string": "b"}),
        ("NotebookEdit", {"notebook_path": "/workspace/n.ipynb", "new_source": "x"}),
        ("Delete", {"file_path": "/workspace/x.md"}),
        ("Bash", {"command": "ls /workspace"}),
        ("Bash", {"command": "touch /workspace/x"}),
        ("Bash", {"command": "rm /workspace/x"}),
        ("Bash", {"command": "curl https://example.com"}),
        ("Bash", {"command": "docker ps"}),
        ("Bash", {"command": "frobnicate --now"}),
        ("Bash", {"command": "git status"}),
        ("Bash", {"command": "git push"}),
        ("Bash", {"command": f"cat /users/{OTHER}/x.md"}),
        ("Bash", {"command": "ssh deploy@server.lan uptime"}),
        ("Bash", {"command": "ssh root@other.lan id"}),
        ("Monitor", {"command": "tail -f /workspace/build.log"}),
        ("PowerShell", {"command": "Get-ChildItem"}),
        ("RunThing", {"command": "ls"}),
        ("SomeTool", {"path": "/workspace/x"}),
        ("mcp__notes__read", {}),
        ("mcp__crm__update", {}),
        ("mcp__mail__send", {}),
        ("mcp__bank__transfer", {}),
        ("mcp__computer__screenshot", {}),
        ("mcp__computer__run_code", {}),
        ("mcp__browser__click", {}),
        ("mcp__meetings-mcp__direct_to", {"targets": ["x"]}),
        ("mcp__meetings_mcp__direct_to", {"targets": ["x"]}),
        ("AskUserQuestion", {"questions": [{"question": "q?"}]}),
        ("ExitPlanMode", {"plan": "the plan"}),
        ("EnterPlanMode", {}),
        ("WebFetch", {"url": "https://example.com/a"}),
        ("WebFetch", {"url": "http://192.168.1.10/a"}),
        ("TodoWrite", {"todos": []}),
        ("Task", {"prompt": "x"}),
        ("apply_patch", {"command": "*** Begin Patch\n*** Add File: /workspace/a.md\n+x\n*** End Patch"}),
    ]
    for tool, tool_input in _inputs()["permission_tools"]:
        pair = (tool, _real(tool_input))
        if pair not in tools:
            tools.append(pair)
    return tools


_BASE_SCENARIO = {"ctx": "manager", "interactive": False, "live": "", "approve": True,
                  "allowed": False, "meeting": None, "revoked": "", "layer": "",
                  "implementing": False, "grants": []}


def _mode_scenarios():
    """Every stored mode on every client kind, headless and interactive, the
    person allowing and refusing."""
    for mode, client, inter, approve in itertools.product(_MODES, _CLIENTS, (False, True), (True, False)):
        yield {**_BASE_SCENARIO, "mode": mode, "client": client, "interactive": inter,
               "approve": approve}


def _live_scenarios():
    """The terminal's live mode (the hook's report) over every stored mode
    and client kind, on an engine whose terminal reports its mode."""
    for mode, client, live in itertools.product(_MODES, _CLIENTS, _LIVE_MODES):
        yield {**_BASE_SCENARIO, "mode": mode, "client": client, "interactive": True, "live": live,
               "layer": "terminal"}


def _special_scenarios():
    """One dimension at a time: the allow-memory, device grants, roles and
    placements, an external caller, a revoked target, no security context,
    the engine stubs, a plan already approved, a meeting turn."""
    dash = session_kind.DASHBOARD.name
    for mode in _MODES:
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "allowed": True}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "grants": ["browser", "computer"]}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "grants": ["app"]}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "ctx": "admin-agent"}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "ctx": "viewer", "approve": False}
        yield {**_BASE_SCENARIO, "mode": mode, "client": session_kind.PHONE.name, "ctx": "external"}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "revoked": "The machine was unpaired."}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "ctx": ""}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "ctx": "remote-full-editor"}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "ctx": "judge"}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "layer": "holds-turn"}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "layer": "canonical"}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "layer": "reviews"}
        yield {**_BASE_SCENARIO, "mode": mode, "client": session_kind.TASK.name, "layer": "reviews"}
        for live in ("default", "bypassPermissions"):
            yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "interactive": True,
                   "live": live, "layer": "codex-terminal"}
        for live in ("auto", "acceptEdits"):
            yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "interactive": True,
                   "live": live, "layer": "terminal-reviews"}
        yield {**_BASE_SCENARIO, "mode": mode, "client": dash, "implementing": True}
        for routed, moderator in (("", False), ("direct_to", False), ("end_meeting", True),
                                  ("end_meeting", False)):
            yield {**_BASE_SCENARIO, "mode": mode, "client": session_kind.TASK.name,
                   "meeting": {"routed": routed, "moderator": moderator}}


class _Layer:
    """An engine stub: one whose question tool holds the turn open
    (Codex's), one that names MCP tools with a sanitized server key, one
    whose own reviewer judges the call (Auto), one whose terminal reports
    its mode and asks from the hook (Claude Code's, with no reviewer, or
    with its classifier running in its auto mode), or Codex's terminal,
    which does neither."""

    def __init__(self, kind: str):
        self.kind = kind

    def engine_reviews(self, session_id, live_mode):
        return self.kind == "reviews" or (self.kind == "terminal-reviews" and live_mode == "auto")

    def terminal_reports_mode(self):
        return self.kind in ("terminal", "terminal-reviews")

    def hook_can_ask(self):
        return self.kind in ("terminal", "terminal-reviews")

    def canonical_tool_name(self, session_id, tool_name):
        if self.kind == "canonical" and tool_name.startswith("mcp__meetings_mcp__"):
            return "mcp__meetings-mcp__" + tool_name[len("mcp__meetings_mcp__"):]
        return tool_name

    def capabilities_for(self, session_id):
        holds = self.kind == "holds-turn"
        behaviour = type("B", (), {"question_tool_holds_turn": holds})()
        return type("C", (), {"behaviour": behaviour})()


class _Pump:
    session_id = "s"
    implementing_plan = True


@pytest.fixture
def decide(floor, monkeypatch):
    """``decide(scenario, tool, input)``: the permission authority's answer
    under one scenario, with every session-state read patched where the
    module reads it (the way the hook tests patch it): the decision, the
    items it queued (request ids blanked) and the side effects it made."""
    from core.events import stream_pump
    from core.sandbox import session_config_dir
    from core.session import session_manager, session_state
    from services import path_policy_v2
    from services.mcp import mcp_permissions, mcp_registry
    from storage import database as task_store

    state: dict = {}
    effects: list = []
    queues: dict[str, asyncio.Queue] = {}
    pumps: dict = {}
    matrix = _matrix()

    def _queue(sid):
        return queues.setdefault(sid, asyncio.Queue())

    async def _wait(request_id, session_id, timeout):
        return state["approve"]

    async def _chat_id(session_id):
        return "chat-1"

    def _meeting(sid):
        m = state["meeting"]
        if not m or sid != "s":
            return None
        return {"pump_session_id": "meeting-m1", "parent_chat_id": "chat-p",
                "agent_slug": "helper", "parent_session_id": "parent",
                "routed_tool": m["routed"], "is_moderator": m["moderator"], "turn_text_chars": 0}

    monkeypatch.setattr(permission, "record_hook_activity", lambda sid: effects.append(["activity", sid]))
    monkeypatch.setattr(permission, "get_session_mode",
                        lambda sid: "default" if sid == "parent" else state["mode"])
    monkeypatch.setattr(permission, "recorded_session_mode",
                        lambda sid: "default" if sid == "parent" else state["mode"])
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: state["client"])
    monkeypatch.setattr(permission, "get_session_security", lambda sid: state["ctx_obj"])
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: state["interactive"])
    monkeypatch.setattr(permission, "_park_interactive_on_dialog",
                        lambda sid, tool: effects.append(["park", tool]))
    monkeypatch.setattr(permission, "set_session_mode",
                        lambda sid, mode: effects.append(["mode", sid, mode]))
    monkeypatch.setattr(permission, "is_session_tool_allowed", lambda sid, tool: state["allowed"])
    monkeypatch.setattr(permission, "remember_session_tool_allow",
                        lambda sid, tool: effects.append(["remember", tool]))
    monkeypatch.setattr(permission, "get_permission_queue", _queue)
    monkeypatch.setattr(permission, "wait_for_permission", _wait)
    monkeypatch.setattr(permission, "watch_prompt", lambda request_id: None)
    monkeypatch.setattr(permission, "took_no_answer", lambda request_id: False)
    monkeypatch.setattr(routing, "get_meeting_session_info", _meeting)
    monkeypatch.setattr(routing, "mark_meeting_turn_routed",
                        lambda sid, short: effects.append(["routed", short]))
    monkeypatch.setattr(routing, "resolve_hook_chat_id", _chat_id)
    monkeypatch.setattr(task_store, "update_chat",
                        lambda chat_id, **kw: effects.append(["chat", chat_id, sorted(kw.items())]))
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda ctx: state["revoked"])
    monkeypatch.setattr(session_manager, "engine_layer_for_session",
                        lambda sid: _Layer(state["layer"]) if state["layer"] else None)
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier",
                        lambda server, tool: _MCP_TIERS.get(server, "standard"))
    monkeypatch.setattr(mcp_registry, "device_capability_for_server",
                        lambda server: _DEVICE_CAPS.get(server, ""))
    monkeypatch.setattr(stream_pump, "_active_pumps", pumps)
    monkeypatch.setattr(session_config_dir, "attached_ssh_hosts", lambda agent: SSH_HOSTS)
    # Every call answers alone: the terminal's last word and an Allow kept
    # for the engine's repeat request never carry over to the next.
    live_words: dict = {}
    allowed_calls: dict = {}
    monkeypatch.setattr(session_state, "_live_words", live_words)
    monkeypatch.setattr(permission, "_ALLOWED_CALLS", allowed_calls)

    async def _decide(sc: dict, tool: str, tool_input: dict) -> str:
        ctx = matrix[sc["ctx"]] if sc["ctx"] else None
        if ctx is not None and sc["grants"]:
            ctx = dataclasses.replace(ctx, placement=dataclasses.replace(
                ctx.placement, device_grants=set(sc["grants"])))
        state.clear()
        state.update(sc, ctx_obj=ctx)
        effects.clear()
        queues.clear()
        pumps.clear()
        live_words.clear()
        allowed_calls.clear()
        if sc["interactive"]:
            # The terminal was spawned in the stored mode (its registration).
            live_words["s"] = terminal_mode(sc["mode"])
        if sc["implementing"]:
            pumps["chat-x"] = _Pump()
        try:
            got = {"result": await permission.decide_tool_permission(
                "s", tool, copy.deepcopy(tool_input), live_permission_mode=sc["live"])}
        except Exception as exc:  # noqa: BLE001  # a raise is an answer the corpus records too
            got = {"raised": f"{type(exc).__name__}: {exc}"}
        items = []
        for sid in sorted(queues):
            while not queues[sid].empty():
                item = dict(queues[sid].get_nowait())
                if "request_id" in item:
                    item["request_id"] = "<id>"
                items.append([sid, item])
        got["queued"] = items
        got["effects"] = list(effects)
        return _plain(got)

    _decide.compare_optional = floor
    return _decide


async def _permission_family(decide, family: str, scenarios) -> None:
    tools = _permission_tools()
    book = _Book(family, [f"{tool}#{_case_id(tool_input)[:8]}" for tool, tool_input in tools])
    for sc in scenarios:
        book.add(sc, [await decide(sc, tool, tool_input) for tool, tool_input in tools])
    book.check(compare_optional=decide.compare_optional)


@pytest.mark.asyncio
async def test_permission_by_mode_and_client_as_recorded(decide):
    await _permission_family(decide, "permission-modes", _mode_scenarios())


@pytest.mark.asyncio
async def test_permission_by_live_mode_as_recorded(decide):
    await _permission_family(decide, "permission-live", _live_scenarios())


@pytest.mark.asyncio
async def test_permission_one_dimension_at_a_time_as_recorded(decide):
    await _permission_family(decide, "permission-special", _special_scenarios())
