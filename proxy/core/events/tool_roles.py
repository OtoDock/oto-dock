"""The platform's tool vocabulary, by role.

Every engine presents its tool calls under its own names — Claude Code's
``Bash`` / ``Read`` / ``Write``, Codex's ``commandExecution`` / ``fileChange``
/ ``update_plan``, Direct LLM's builtins — and maps them into the CANONICAL
names below at its own edge (``ExecutionLayer.canonical_tool_name``).
Generic code — the permission authority, the checks' changed set, the event
pump, the transcript tailers, the history seed, the hook script's twin, the
dashboard's mirror (``dashboard/src/lib/tools/roles.ts``) — never compares a
tool NAME. It asks the tool's ROLE (what the call does), its PAYLOAD (what
the call carries, and under which input keys) and, for a shell, its DIALECT.

A stdlib-only leaf: ``storage/`` may import it; the hook scripts cannot, so
``hooks/tool_result_forwarder.py`` carries ``TOOL_ROLES`` as a byte twin the
release gate's twin rule pins (same key order, string values on both sides).

The canonical names are FROZEN: they are the ``tool_name`` the CLIs put on
the hook wire and the ``event_data.name`` persisted in ``chat_messages``
(``TODO_SNAPSHOT`` is the one every engine's checklist snapshot is stored
under, whatever tool produced it). A new role or name here is the whole
change for the code above. ``tests/execution/test_tool_roles.py`` binds the
tables, the mirror and the twin; ``tests/execution/test_engine_id_surface.py``
(pass 3) refuses a canonical name compared anywhere else.
"""

from __future__ import annotations

# --- Roles: what a call does ------------------------------------------------

SHELL = "shell"              # runs a command; ``dialect_of`` says which shell
READ = "read"                # reads one file
GLOB = "glob"                # lists files by pattern ("N files")
SEARCH = "search"            # searches file contents ("N results")
WRITE = "write"              # writes or edits files, by path or by patch
DELETE = "delete"            # deletes a file: a write for the gates, never auto-approved
WEB_FETCH = "web_fetch"      # fetches a URL (the SSRF gate)
WEB_SEARCH = "web_search"    # a web search query
SUBAGENT = "subagent"        # spawns a subagent (its own block; its tools are gated on their own)
CHECKLIST = "todo"           # the checklist
TASK_READ = "task_read"      # reads the CLI's session task list
TASK_WRITE = "task_write"    # edits the CLI's session task list
DISCOVERY = "discovery"      # loads tool schemas — no effect on anything
SKILL = "skill"              # loads a skill by name
WORKFLOW = "workflow"        # runs a workflow script
PLAN_ENTER = "plan_enter"
PLAN_EXIT = "plan_exit"
QUESTION = "question"        # asks the human
ESCALATION = "escalation"    # Codex's privileged approvals presented as tools: a sandbox escalation, terminal input to an elevated command

ROLES: tuple[str, ...] = (
    SHELL, READ, GLOB, SEARCH, WRITE, DELETE, WEB_FETCH, WEB_SEARCH, SUBAGENT,
    CHECKLIST, TASK_READ, TASK_WRITE, DISCOVERY, SKILL, WORKFLOW, PLAN_ENTER,
    PLAN_EXIT, QUESTION, ESCALATION,
)

# --- Payload kinds: what a call carries -------------------------------------

COMMAND = "command"          # a shell command
FILE_PATH = "file_path"      # one file
SEARCH_PATH = "path"         # an optional directory to search under
PATCH = "patch"              # a patch text naming its files
URL = "url"
QUERY = "query"
NAME = "name"                # a skill or workflow name
DESCRIPTION = "description"  # a subagent's brief
TODOS = "todos"              # the checklist items
NONE = ""

#: The input keys a payload kind is read from, in order — the gates and the
#: renderers read the FIRST non-empty one. A CLI names its notebook path
#: ``notebook_path``; a Codex patch arrives under ``command`` on the hook
#: wire, ``input`` in the rollout, ``patch`` / ``patch_text`` elsewhere.
PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    COMMAND: ("command",),
    FILE_PATH: ("file_path", "notebook_path"),
    SEARCH_PATH: ("path",),
    PATCH: ("command", "patch", "input", "patch_text"),
    URL: ("url",),
    QUERY: ("query",),
    NAME: ("name",),
    DESCRIPTION: ("description",),
    TODOS: ("todos",),
}

#: An UNDECLARED tool that carries one of these as a string is a command
#: tool the platform does not know — the permission authority refuses it.
SHELL_PAYLOAD_KEYS: tuple[str, ...] = ("command", "cmd", "script")

# --- Shell dialects ----------------------------------------------------------

POSIX = "posix"
POWERSHELL = "powershell"

# --- The tables --------------------------------------------------------------
# Byte twin of ``TOOL_ROLES``: ``proxy/hooks/tool_result_forwarder.py`` —
# keep the key order and the string values identical on both sides.

TOOL_ROLES: dict[str, str] = {
    "Bash": "shell",
    "Monitor": "shell",
    "PowerShell": "shell",
    "Read": "read",
    "Glob": "glob",
    "Grep": "search",
    "Write": "write",
    "Edit": "write",
    "MultiEdit": "write",
    "NotebookEdit": "write",
    "apply_patch": "write",
    "Delete": "delete",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "web_search": "web_search",
    "Agent": "subagent",
    "Task": "subagent",
    "TodoWrite": "todo",
    "TodoRead": "todo",
    "TaskGet": "task_read",
    "TaskList": "task_read",
    "TaskOutput": "task_read",
    "TaskCreate": "task_write",
    "TaskUpdate": "task_write",
    "TaskStop": "task_write",
    "ToolSearch": "discovery",
    "tool_search": "discovery",
    "Skill": "skill",
    "Workflow": "workflow",
    "EnterPlanMode": "plan_enter",
    "ExitPlanMode": "plan_exit",
    "AskUserQuestion": "question",
    "request_user_input": "question",
    "CodexEscalation": "escalation",
    "CodexTerminalInput": "escalation",
}

TOOL_PAYLOADS: dict[str, str] = {
    "Bash": COMMAND,
    "Monitor": COMMAND,
    "PowerShell": COMMAND,
    "Read": FILE_PATH,
    "Glob": SEARCH_PATH,
    "Grep": SEARCH_PATH,
    "Write": FILE_PATH,
    "Edit": FILE_PATH,
    "MultiEdit": FILE_PATH,
    "NotebookEdit": FILE_PATH,
    "apply_patch": PATCH,
    "Delete": FILE_PATH,
    "WebFetch": URL,
    "WebSearch": QUERY,
    "web_search": QUERY,
    "Agent": DESCRIPTION,
    "Task": DESCRIPTION,
    "TodoWrite": TODOS,
    "TodoRead": NONE,
    "TaskGet": NONE,
    "TaskList": NONE,
    "TaskOutput": NONE,
    "TaskCreate": NONE,
    "TaskUpdate": NONE,
    "TaskStop": NONE,
    "ToolSearch": QUERY,
    "tool_search": QUERY,
    "Skill": NAME,
    "Workflow": NAME,
    "EnterPlanMode": NONE,
    "ExitPlanMode": NONE,
    "AskUserQuestion": NONE,
    "request_user_input": NONE,
    "CodexEscalation": NONE,
    "CodexTerminalInput": NONE,
}

SHELL_DIALECTS: dict[str, str] = {
    "Bash": POSIX,
    "Monitor": POSIX,
    "PowerShell": POWERSHELL,
}

#: The persisted block name every engine's checklist snapshot carries — a
#: native TodoWrite row, the synthesized snapshot of the CLI's Task* family,
#: Codex's ``update_plan``. What the panel restore looks for.
TODO_SNAPSHOT = "TodoWrite"


# --- The questions -----------------------------------------------------------

def role_of(name: str) -> str:
    """The tool's role, ``""`` for a tool the platform does not know."""
    return TOOL_ROLES.get(name or "", "")


def payload_of(name: str) -> str:
    """The payload kind the tool carries (``NONE`` for a structured call or
    an unknown tool)."""
    return TOOL_PAYLOADS.get(name or "", NONE)


def dialect_of(name: str) -> str:
    """The shell dialect of a ``shell`` tool, ``""`` otherwise."""
    return SHELL_DIALECTS.get(name or "", "")


def is_known(name: str) -> bool:
    return (name or "") in TOOL_ROLES


def names_of(role: str) -> tuple[str, ...]:
    """The canonical names of one role, in table order."""
    return tuple(n for n, r in TOOL_ROLES.items() if r == role)


def writes(name: str) -> bool:
    """The call writes the paths it names (a write or a delete)."""
    return role_of(name) in (WRITE, DELETE)


def payload_key(name: str, tool_input) -> str:
    """The input key the tool's payload was found under — the first of the
    kind's keys holding a non-empty string — or ``""``."""
    if not isinstance(tool_input, dict):
        return ""
    for key in PAYLOAD_KEYS.get(payload_of(name), ()):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return key
    return ""


def payload_value(name: str, tool_input) -> str:
    """The tool's payload as a string — the value under ``payload_key`` —
    or ``""`` (no payload, no such key, not a string)."""
    key = payload_key(name, tool_input)
    return tool_input[key] if key else ""


def carries_command(tool_input) -> bool:
    """Whether an input carries a command under any of the keys a shell tool
    uses — the fail-closed test for an undeclared tool."""
    if not isinstance(tool_input, dict):
        return False
    return any(isinstance(tool_input.get(k), str) and tool_input.get(k) for k in SHELL_PAYLOAD_KEYS)
