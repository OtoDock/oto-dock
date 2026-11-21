"""Direct-LLM builtin file tools: the platform's half.

Every file tool runs in the session's sandbox on the compute machine, in
the file program (``file_program.py``, started by the general part,
``sandbox_program.py``). There the kernel shows the session its own folders
and nothing else, read-only where its role table says so: the mounts are the
wall. This module opens no file of a session. It keeps what stays in the
platform:

- the check of a path as text (``locate``): a refused name, a path in no
  session folder and a write under a read-only row are refused here first,
  with the sentence the model should see. It reads
  ``SandboxBuilder.workspace_mount_table``, the same ordered ``Mount`` list
  the machine renders as the sandbox's mounts. It only words an early
  refusal: the program and the kernel stand under it;
- what the program is told (``program_facts``) and the calls;
- the second look at a place a link lands (``landing``);
- where the platform's bookkeeping finds a written file, from the row's
  text (``record_place``);
- every sentence.

The role / RBAC / library rules stay in
``auth.path_policy.check_tool_access`` (Pass-1 of the builtin gate in
``builtins.py``). After a write the platform's bookkeeping runs, and a
delete is removed by the platform's own delete owner once the sandbox has
judged the file (``services/infra/file_bookkeeping.py``). The proxy's process
opens a file of an agent's folder on a model's call in three places only, all
in that owner: the hand-on of a written file to paired computers, the capture
and removal of a deleted file, and the library projection after a write or a
delete under ``knowledge/``.

Virtual paths are the sandbox form every prompt uses (``/workspace/…``,
``/knowledge/…``, ``/users/<u>/…``, ``/config/…``); a relative path resolves
against the session cwd (``/users/<u>`` for a user mount, ``/workspace`` for
the agent scope — ``SandboxBuilder.get_cwd``).
"""

from __future__ import annotations

import functools
import posixpath
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from core import agent_folder
from core.layers.direct import sandbox_program
from core.sandbox.sandbox import Mount, SandboxBuilder, SandboxConfig

if TYPE_CHECKING:
    from core.sandbox.sandbox_render import SandboxDescription

# Path components no tool may traverse: MCP OAuth tokens, the CLI state dirs
# (hooks + settings live there), and git internals. Mirrors the sandbox's own
# protected-destination list + the path-policy credential gate.
REFUSED_COMPONENTS = frozenset({".credentials", ".claude", ".codex", ".git"})
# Directories Glob never descends into (runtime noise, never workspace content).
GLOB_SKIP_DIRS = REFUSED_COMPONENTS | frozenset({
    "node_modules", "__pycache__", ".venv", "venv", ".quarantine", ".uv-python",
})

READ_DEFAULT_LINES = 2000
READ_MAX_BYTES = 256 * 1024
WRITE_MAX_BYTES = 1024 * 1024
GLOB_MAX_ENTRIES = 200

#: The refusals of a link, as the person reads them.
LEAVES = "The path leaves the session folder through a symlink."
ELSEWHERE = "The path goes through a symlink to a place the file tools cannot reach."


class FileToolError(Exception):
    """A refusal or failure whose message goes back to the model verbatim."""


# ---------------------------------------------------------------------------
# The text check
# ---------------------------------------------------------------------------

def mount_table(cfg: SandboxConfig) -> list[Mount]:
    """The session's mount decisions — the same list bwrap renders."""
    return SandboxBuilder(cfg).workspace_mount_table()


def session_cwd(cfg: SandboxConfig) -> str:
    return SandboxBuilder(cfg).get_cwd()


def has_writable_mount(mounts: Sequence[Mount]) -> bool:
    """Whether any TOOL-REACHABLE mount is writable: the CLI state dirs the
    table stacks RW on a read-only root (a Shared-only viewer's
    ``/workspace/.claude``) are refused above, so they do not make a
    session writable for the person."""
    return any(
        m.rw and not any(p in REFUSED_COMPONENTS for p in m.sandbox.split("/"))
        for m in mounts
    )


def text_bytes(value: str, what: str) -> int:
    """How many bytes ``value`` is as UTF-8. A string that is no text (a
    lone surrogate) is refused here: it could not be sent to the file
    program, and no file holds it."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise FileToolError(f"{what} holds a character that cannot be written as text") from None


def normalize_virtual(raw: str, cwd: str) -> str:
    """Absolute, ``..``-collapsed virtual path (relative → against ``cwd``)."""
    raw = (raw or "").strip()
    if not raw:
        raise FileToolError("file_path is required")
    text_bytes(raw, "file_path")
    if raw.startswith("~"):
        raise FileToolError(
            "'~' is not a session folder here. Use /workspace, /knowledge, "
            "/users/<you>, /caller or /config"
        )
    if not raw.startswith("/"):
        raw = posixpath.join(cwd, raw)
    norm = posixpath.normpath(raw)
    if not norm.startswith("/"):
        raise FileToolError(f"cannot resolve path: {raw}")
    return norm


def _matching_mount(mounts: Sequence[Mount], vpath: str) -> Mount | None:
    """Longest ``sandbox`` prefix wins; on a tie the LATER entry (bwrap's
    later-bind precedence — the RO root + RW subdir stacking)."""
    best: Mount | None = None
    for m in mounts:
        dest = m.sandbox.rstrip("/") or "/"
        if vpath == dest or vpath.startswith(dest + "/"):
            if best is None or len(dest) >= len(best.sandbox.rstrip("/") or "/"):
                best = m
    return best


def locate(
    mounts: Sequence[Mount], raw: str, *, cwd: str, writing: bool = False,
) -> tuple[str, Mount]:
    """The normalized virtual path of ``raw`` and the row that holds it,
    judged on the text alone: a refused name, a path in no session folder
    and a write under a read-only row are refused with the sentence the
    model should see. Nothing is opened. It only words an early refusal:
    the file program and the kernel stand under it."""
    vpath = normalize_virtual(raw, cwd)
    parts = [p for p in vpath.split("/") if p]
    hit = next((p for p in parts if p in REFUSED_COMPONENTS), None)
    if hit:
        raise FileToolError(f"{vpath}: the {hit} subtree is not accessible to tools")
    mount = _matching_mount(mounts, vpath)
    if mount is None:
        roots = ", ".join(sorted({m.sandbox for m in mounts})) or "(none)"
        raise FileToolError(
            f"{vpath} is not inside a session folder (available: {roots})"
        )
    if writing and not mount.rw:
        if not has_writable_mount(mounts):
            raise FileToolError(
                "no writable location in this session: every folder is read-only"
            )
        raise FileToolError(f"{vpath} is read-only in this session")
    return vpath, mount


# ---------------------------------------------------------------------------
# The file program: every file tool inside the session's sandbox
# ---------------------------------------------------------------------------
#
# The platform's half of the file tools that run in the session's sandbox
# (``file_program.py`` is the other half, ``sandbox_program.py`` the general
# part that starts it). Here: what the program is told (the facts), the
# calls, the second look at a place a link lands, where the bookkeeping
# finds a written file, and every sentence. The opens are the program's,
# under the kernel's mounts.

#: The program's answer for a path through a link: where it lands.
LANDS = "lands"
#: What the model reads when the program gave no answer of its own. Each
#: says what is true and what to do. No program to ask (the machine's link
#: went, and the next message builds a new session):
NOT_REACHABLE = ("The file tools cannot reach OtoDock's compute machine right now. "
                 "Send your message again in a moment.")
#: The machine could not start it, and the next call tries again:
NOT_STARTED = "The file tools could not start on OtoDock's compute machine. Try again in a moment."
#: It ended before it answered a call. The machine is there, and the next
#: call starts it again or says that it rests:
STOPPED = "The file tools stopped before they answered. Try again in a moment."
#: It stopped or could not start several times in a row and rests:
RESTING = ("The file tools stopped or could not start on OtoDock's compute machine, several times in a row. "
           "They are tried again in {seconds} seconds.")
#: A call passed its cap. The program was closed and starts again at the
#: next call. A search visits every folder below the one it is given,
#: whatever its pattern, so only a smaller folder shortens it:
NO_ANSWER = ("The file tools did not answer in {seconds} seconds and were stopped. "
             "They start again at the next call. A search of a very large folder can take that long: "
             "name a smaller folder.")
#: A write or an edit that the program was asked and gave no answer for: the
#: file may be written. Never "nothing was written" where that is not known.
NOT_KNOWN = "It is not known whether {path} was {done}: the file tools gave no answer. Read it to see."
#: A call that was never sent, so nothing changed:
UNSENT = ("The file tools could not send this call: it is too large, or it holds a character that is no text. "
          "Nothing was changed.")
#: A delete whose removal the platform began and that did not come to an
#: answer: the file may be gone. Never "cannot delete" where that is not known.
DELETE_NOT_KNOWN = "It is not known whether {path} was deleted: the delete did not finish. Look for the file to see."
#: An edit the program called done without a count: the file was changed.
EDIT_UNCOUNTED = "The file tools did not say how many places changed. Read it to see."
#: What ``NOT_KNOWN`` says was done, by the verb of a tool that changes a file.
_CHANGES = {"write": "written", "edit": "changed"}
_REFUSED_WRITE = "cannot {verb} {path}: the file system refused the write (read-only or no permission)"
#: What the platform keeps of a refusal's own words, and of one hit of a
#: search (a path's limit). The program's caps are the first line. These are
#: the platform's own, so that no answer hands it more than one of ours.
_WORDS_MAX = 200
_HIT_MAX = 4096

#: The sentence of each code the program answers, for ``{path}`` (with
#: ``{verb}``, the refused ``{name}``, the program's own ``{detail}`` and
#: the ``{seconds}`` of a cap or a rest).
_SENTENCES: dict[str, str] = {
    "missing": "File not found: {path}",
    "folder": "{path} is a folder. Use Glob to list it.",
    "binary": ("{path} is a binary file. Read returns text only. "
               "Documents and images go through the file-tools MCP."),
    "special": "cannot {verb} {path}: it is not a regular file",
    "link": "cannot {verb} {path}: the path goes through a symlink, so use the real path instead",
    "leaves": LEAVES,
    "elsewhere": ELSEWHERE,
    LANDS: ELSEWHERE,
    "refused_name": "{path}: the {name} subtree is not accessible to tools",
    "not_a_session_folder": "{path} is not inside a session folder",
    "bad_path": "{path} is not inside a session folder",
    "not_a_folder": "{path} is not a folder",
    "bad_pattern": "pattern must be relative to the folder (e.g. **/*.md)",
    "link_at_name": "{path} is a symlink. Write the real path instead.",
    "denied": _REFUSED_WRITE,
    "read_only": _REFUSED_WRITE,
    "no_space": "cannot {verb} {path}: not enough storage in the agent's bucket for this write",
    "bad_content": "cannot {verb} {path}: the text holds a character that cannot be written",
    "too_large": "content is larger than {cap} KB. Write accepts up to that size.",
    "not_text": "{path} is not valid UTF-8 text",
    "not_found_text": "old_string was not found in {path}",
    "many": ("old_string appears {count} times in {path}. Add surrounding context to make it unique, "
             "or set replace_all."),
    "grows": "cannot edit {path}: the result would be larger than {cap} KB",
    "changed": "{path} changed while Edit worked on it. Nothing was written. Read it again.",
    sandbox_program.NOT_RUNNING: NOT_REACHABLE,
    sandbox_program.NOT_STARTED: NOT_STARTED,
    sandbox_program.ENDED: STOPPED,
    sandbox_program.RESTING: RESTING,
    sandbox_program.TIMED_OUT: NO_ANSWER,
    sandbox_program.CLOSED: NOT_REACHABLE,
    sandbox_program.UNSENT: UNSENT,
}
#: The sentences that differ by the tool: a code's own words for one verb.
_SENTENCES_OF: dict[str, dict[str, str]] = {
    "write": {"folder": "{path} is a folder"},
    "edit": {
        "folder": "{path} is a folder",
        "binary": "{path} is a binary file. Edit works on text files.",
        "too_large": "{path} is larger than {cap} KB. Edit works on text files up to that size.",
    },
    "delete": {
        "folder": "{path} is a folder. Delete removes single files.",
        "special": "{path} is not a regular file. Delete removes regular files only.",
    },
}
#: The codes that say a skill is not there (anything else is a failure).
_NO_SUCH_SKILL = frozenset({"missing", "too_large", "folder", "special", "link"})

#: One operation of the file program: ``call(operation, args)``, and for an
#: operation that changes something ``call(operation, args, then=...)``
#: (``sandbox_program.call``).
ProgramCall = Callable[..., Awaitable[dict]]
#: What the platform does once a path's call was answered: ``then(path,
#: answer)``, with the path the program was asked for.
ThenAt = Callable[[str, dict], Coroutine[Any, Any, None]]
#: What judges a place a link lands, before it is opened: ``judge(asked,
#: landed)`` raises ``FileToolError``.
LandingJudge = Callable[[str, str], None]


def program_facts(description: SandboxDescription, builder: SandboxBuilder) -> dict:
    """What the file program is told at its start: the rows of the role
    table its sandbox is rendered from, the refused names, the names a
    search skips, the caps and the session's skills folder. The program
    holds none of these itself."""
    rows = sorted({
        bind.sandbox.rstrip("/") or "/" for bind in description.binds
        if not any(part in REFUSED_COMPONENTS for part in bind.sandbox.split("/"))
    })
    config_dir = builder.get_env_overrides().get("CLAUDE_CONFIG_DIR", "")
    return {
        "rows": rows,
        "refused": sorted(REFUSED_COMPONENTS),
        "glob_skip": sorted(GLOB_SKIP_DIRS),
        "read_max": READ_MAX_BYTES,
        "write_max": WRITE_MAX_BYTES,
        "glob_max": GLOB_MAX_ENTRIES,
        "skills": f"{config_dir}/skills" if config_dir else "",
        "skill_max": _skill_max(),
    }


def _skill_max() -> int:
    """The cap of one skill's text: the materializer's own."""
    from core.sandbox.skills_materializer import _MAX_SKILL_BYTES
    return _MAX_SKILL_BYTES


#: The file tools' program in a session's sandbox.
PROGRAM = sandbox_program.ProgramSpec(
    name=f"{sandbox_program.NAME_PREFIX}files",
    source=Path(__file__).with_name("file_program.py").read_text(encoding="utf-8"),
    facts=program_facts,
)


async def call_program(session, operation: str, args: dict, *, then: sandbox_program.Then | None = None) -> dict:
    """One operation of the session's file program. ``then`` is what the
    platform does with the answer of an operation that changes something
    (a written file's record): from the moment the program is asked, the
    call and ``then`` are one piece that a stop cannot split."""
    return await sandbox_program.call(getattr(session, "mcp_manager", None), PROGRAM.name, operation, args,
                                      then=then)


def refusal(answer: dict, vpath: str, *, verb: str = "read") -> FileToolError:
    """The sentence of an answer that is no success. A write or an edit
    that the program was asked and did not answer says that it is not known
    whether the file changed."""
    if verb in _CHANGES and sandbox_program.asked(answer):
        return FileToolError(NOT_KNOWN.format(path=vpath, done=_CHANGES[verb]))
    code = str(answer.get("code") or "")
    detail = str(answer.get("detail") or "the file tools failed")[:_WORDS_MAX]
    name = str(answer.get("name") or "")[:_WORDS_MAX]
    seconds, count = answer.get("seconds"), answer.get("count")
    template = _SENTENCES_OF.get(verb, {}).get(code) or _SENTENCES.get(code, "cannot {verb} {path}: {detail}")
    return FileToolError(template.format(path=vpath, verb=verb, name=name, detail=detail,
                                         seconds=seconds if type(seconds) is int else "some",
                                         count=count if type(count) is int else "several",
                                         cap=WRITE_MAX_BYTES // 1024))


def landing(mounts: Sequence[Mount], asked: str, landed: str, *, writing: bool = False) -> None:
    """A place the program says ``asked`` lands, judged again on the text:
    a normal path with no refused name, in a session folder (a writable one
    for ``writing``), inside the row ``asked`` starts in. These are the
    program's own rules, checked here too, so that no answer of the program
    widens what is opened."""
    try:
        vpath, _row = locate(mounts, landed, cwd="/", writing=writing)
    except FileToolError:
        raise FileToolError(ELSEWHERE) from None
    row = _matching_mount(mounts, asked)
    if vpath != landed or row is None:
        raise FileToolError(ELSEWHERE)
    dest = row.sandbox.rstrip("/") or "/"
    if landed != dest and not landed.startswith(dest + "/"):
        raise FileToolError(LEAVES)


async def _through_links(call: ProgramCall, operation: str, vpath: str, args: dict,
                         judge: LandingJudge, then: ThenAt | None = None) -> dict:
    """One operation on a path. The program opens no path through a link:
    it names where the path lands, ``judge`` decides on that place, and the
    program is asked again for it with no link followed. A second landing is
    never followed. ``then`` rides each of the two calls with the path that
    call asked for."""
    def piece(path: str) -> dict:
        return {} if then is None else {"then": functools.partial(then, path)}

    answer = await call(operation, {**args, "path": vpath}, **piece(vpath))
    if answer.get("code") != LANDS:
        return answer
    landed = answer.get("landed")
    if not isinstance(landed, str):
        raise FileToolError(ELSEWHERE)
    judge(vpath, landed)
    return await call(operation, {**args, "path": landed, "strict": True}, **piece(landed))


def number_lines(text: str, offset: int | None = None, limit: int | None = None, *,
                 truncated: bool = False) -> str:
    """``cat -n`` style lines of a read's text: the CLI Read tool's shape
    (1-based ``offset`` line, ``limit`` lines, default 2000)."""
    lines = text.splitlines()
    if not lines:
        return "(empty file)"
    start = offset if isinstance(offset, int) and offset > 0 else 1
    count = limit if isinstance(limit, int) and limit > 0 else READ_DEFAULT_LINES
    window = lines[start - 1:start - 1 + count]
    if not window:
        return f"(no lines at offset {start}: the file has {len(lines)} lines)"
    out = "\n".join(f"{start + i:6d}\t{line}" for i, line in enumerate(window))
    remaining = len(lines) - (start - 1 + len(window))
    if remaining > 0:
        out += f"\n... ({remaining} more lines: continue with offset={start + len(window)})"
    if truncated:
        out += f"\n... (read stopped at {READ_MAX_BYTES // 1024} KB)"
    return out


async def read(call: ProgramCall, vpath: str, offset: int | None = None, limit: int | None = None, *,
               judge: LandingJudge) -> str:
    """A numbered text read of ``vpath`` (256 KB byte cap, binary refused),
    opened by the file program. An in-mount link reads where it lands."""
    answer = await _through_links(call, "read", vpath, {}, judge)
    if answer.get("ok") is not True:
        raise refusal(answer, vpath)
    text = str(answer.get("text") or "")
    # The read's cap, held here too: a text of more characters never came
    # out of that many bytes.
    cut = len(text) > READ_MAX_BYTES
    return number_lines(text[:READ_MAX_BYTES], offset, limit, truncated=cut or bool(answer.get("truncated")))


def glob_pattern(pattern: str) -> str:
    """A search's pattern, stripped: one that is empty, absolute or climbs
    is refused with the sentence the model should see."""
    pattern = (pattern or "").strip()
    if not pattern:
        raise FileToolError("pattern is required (e.g. **/*.md)")
    if pattern.startswith("/") or ".." in pattern.split("/"):
        raise FileToolError("pattern must be relative to the folder (e.g. **/*.md)")
    return pattern


def _plain_rel(rel: object) -> bool:
    """Whether a search's hit is a path below the searched folder."""
    from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path
    try:
        return isinstance(rel, str) and len(rel) <= _HIT_MAX and normalize_rel_path(rel) == rel
    except PathOutsideRoot:
        return False


class Found(NamedTuple):
    """What a search answers: the paths it found, and how many folders it
    could not open. A search with such a folder is not whole, and whoever
    words the answer says so (``unread_line``)."""
    paths: list[str]
    unread: int


def unread_line(unread: int) -> str:
    """What a search says about the folders it could not open."""
    if unread == 1:
        return "... (1 folder could not be opened and was not searched)"
    return f"... ({unread} folders could not be opened and were not searched)"


async def glob(call: ProgramCall, vpath: str, pattern: str, *, judge: LandingJudge) -> Found:
    """Files under the folder ``vpath`` whose path below it matches
    ``pattern`` (``**`` supported), as virtual paths, sorted, capped at
    ``GLOB_MAX_ENTRIES``, and the count of folders the search could not
    open. The file program walks: it enters no link and no runtime folder,
    and lists a link only where it lands on a regular file of its own row."""
    pattern = glob_pattern(pattern)
    answer = await _through_links(call, "glob", vpath, {"pattern": pattern}, judge)
    if answer.get("ok") is not True:
        raise refusal(answer, vpath, verb="list")
    rels = answer.get("rels")
    found = [posixpath.join(vpath, rel) for rel in (rels if isinstance(rels, list) else []) if _plain_rel(rel)]
    unread = answer.get("unread")
    return Found(sorted(found)[:GLOB_MAX_ENTRIES], unread if type(unread) is int and unread > 0 else 0)


async def skill_names(call: ProgramCall) -> list[str]:
    """The ids of the skills in the session's skills folder."""
    answer = await call("skills", {})
    if answer.get("ok") is not True:
        raise refusal(answer, "the skills folder")
    names = answer.get("names")
    return sorted(n for n in (names if isinstance(names, list) else []) if isinstance(n, str))


async def skill(call: ProgramCall, name: str) -> str | None:
    """The text of the skill ``name``, None when the session has no such
    skill. A program that cannot be reached raises its sentence."""
    answer = await call("skill", {"name": name})
    if answer.get("ok") is True:
        return str(answer.get("text") or "")[:_skill_max()]
    if answer.get("code") in _NO_SUCH_SKILL:
        return None
    raise refusal(answer, f"the skill {name}")


# -- Write, Edit and the delete's look ----------------------------------------

def record_place(mounts: Sequence[Mount], vpath: str, agent: str) -> tuple[str, Path] | None:
    """Where the platform's bookkeeping finds the file at ``vpath``: its
    path below the agent's folder and the path of the platform's own copy,
    both from the row's text (the agent's folder followed once when the
    operator made it a link, ``core.agent_folder``). Nothing is opened. None
    for a path in no row and for a row with no place below the agent's
    folder (a mask)."""
    mount = _matching_mount(mounts, vpath)
    if mount is None:
        return None
    try:
        # A mask's empty folder and the null device sit outside every agent's folder.
        _root, where = agent_folder.beneath_agent(mount.host, agent)
    except OSError:
        return None
    dest = mount.sandbox.rstrip("/") or "/"
    inner = vpath[len(dest):].lstrip("/")
    rel = "/".join(part for part in (where, inner) if part)
    return (rel, Path(mount.host) / inner) if rel else None


async def write(call: ProgramCall, vpath: str, content: object, *, then: sandbox_program.Then) -> tuple[bool, int]:
    """Create or replace the text file at ``vpath`` (parents created, 1 MB
    of UTF-8 at most), written by the file program: whole or not there, and
    never through a link. Returns whether the name was free, and the bytes
    written. ``then`` gets the program's answer (``call_program``)."""
    if not isinstance(content, str):
        raise FileToolError("content must be a string")
    size = text_bytes(content, "content")
    if size > WRITE_MAX_BYTES:
        raise FileToolError(
            f"content is {size // 1024} KB. Write accepts up to {WRITE_MAX_BYTES // 1024} KB. "
            "Use the file-tools MCP for larger documents."
        )
    answer = await call("write", {"path": vpath, "content": content}, then=then)
    if answer.get("ok") is not True:
        raise refusal(answer, vpath, verb="write")
    return answer.get("created") is True, size


async def edit(call: ProgramCall, vpath: str, old: object, new: object, replace_all: bool = False, *,
               then: sandbox_program.Then) -> int | None:
    """Replace the exact string ``old`` in the text file at ``vpath``:
    unique unless ``replace_all``. The file program reads, replaces and
    writes in one call, and writes nothing when its look before the move
    shows another file than the one it read.
    Returns the number of replacements, or None when the program said that
    it changed the file and named no count: ``then`` makes the record on the
    program's ``ok``, so that answer is never worded as a failure. ``then``
    gets the program's answer."""
    if not isinstance(old, str) or not old:
        raise FileToolError("old_string is required")
    if not isinstance(new, str):
        raise FileToolError("new_string must be a string")
    if old == new:
        raise FileToolError("old_string and new_string are identical. There is nothing to change.")
    # Neither string can be in a file under the cap, or leave one there, when
    # it is longer than the cap. Held together, the call is always small
    # enough to send.
    if text_bytes(old, "old_string") + text_bytes(new, "new_string") > WRITE_MAX_BYTES:
        raise FileToolError(
            f"old_string and new_string together are larger than {WRITE_MAX_BYTES // 1024} KB. "
            "Edit works on text files up to that size."
        )
    answer = await call("edit", {"path": vpath, "old": old, "new": new, "replace_all": bool(replace_all)},
                        then=then)
    if answer.get("ok") is not True:
        raise refusal(answer, vpath, verb="edit")
    count = answer.get("count")
    return count if type(count) is int and count > 0 else None


async def look(call: ProgramCall, vpath: str, *, judge: LandingJudge, then: ThenAt) -> dict:
    """The delete's look: the file program's answer for the regular file at
    ``vpath``, in a folder the session can write in the kernel's view, with
    the three values that tell the file from every other (``file_facts``).
    The folder may be reached through a link inside its row (judged where it
    lands). The last name stays as given: a link there is never followed.
    That is the program's rule, and it is held here too: a landing under
    another last name is refused before it is judged or looked at, so the
    name that is removed is the name the model gave and the approval showed.
    ``then(path, answer)`` rides the call with the path that was looked at."""
    def same_name(asked: str, landed: str) -> None:
        if posixpath.basename(landed) != posixpath.basename(asked):
            raise FileToolError(ELSEWHERE)
        judge(asked, landed)

    return await _through_links(call, "stat", vpath, {}, same_name, then)


def file_facts(answer: dict) -> tuple[int, int, int]:
    """The three values of a look's answer: the size, the inode number and
    the change time, each a whole number. Anything else is no answer to
    remove a file on."""
    values = (answer.get("size"), answer.get("inode"), answer.get("changed"))
    if not all(type(value) is int and value >= 0 for value in values):
        raise FileToolError("the file tools gave no answer to delete the file on")
    return cast("tuple[int, int, int]", values)
