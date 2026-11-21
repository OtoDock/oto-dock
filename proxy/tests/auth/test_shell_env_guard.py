"""A shell command that prints the session's own token asks
(``auth/path_shell_env.py``).

The plain ways of printing an environment sat in the read tier, so a turn in
Ask printed its session token with nobody asked. The gate raises them to the
tier that asks: a card in Ask, a run in Don't Ask, a refusal in Plan and for
a judge like every command that is not read-only. A guard, never the
boundary: the forms the last test lists pass, and where the token is
accepted is what holds (``tests/api/test_session_refusal.py``).
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from api.hooks.permission import HookPermissionRequest, hook_permission
from auth import path_shell
from auth.path_policy import SecurityContext
from auth.path_shell_env import ps_reveals_session_token, reveals_session_token
from core.session import session_state
from tests.auth.test_floor_corpus import _matrix

_ASK = path_shell._TIER_ORDER["ask"]

PRINTS = [
    "printenv", "printenv -0", "printenv PROXY_API_KEY", "printenv HOME OTO_CLI_TOKEN",
    "/usr/bin/printenv", "X=1 printenv",
    "set", "set > vars.txt", "export", "export -p", "export -p > e.txt", "declare -p", "declare -x",
    "typeset -p", "readonly", "declare -p PROXY_API_KEY",
    "echo $PROXY_API_KEY", 'echo "${PROXY_API_KEY}"', "echo ${PROXY_API_KEY:0:8}",
    "echo ${#PROXY_API_KEY}", "echo $OTO_CLI_TOKEN", "echo $OTO_MCP_FETCH_TOKEN",
    "X=$OTO_CLI_TOKEN", "echo ${!v}", "echo ${!PROXY_*}",
    "ps e", "ps eww", "ps auxe", "ps e -p 1",
    "jq -n env", "jq -n '$ENV.PROXY_API_KEY'", "jq -rn 'env.PATH'",
    "awk 'BEGIN{print ENVIRON[\"PROXY_API_KEY\"]}'",
]
# The same, inside what the gate unwraps: a shell's own command string, a
# substitution, a loop, a wrapper, a pipeline, find's exec clause.
WRAPPED = [
    "bash -c 'printenv'", "sh -c \"echo $PROXY_API_KEY\"", "echo $(printenv)", "echo `export -p`",
    "for i in 1 2; do printenv; done", "if true; then set; fi", "timeout 5 printenv",
    "nohup printenv PROXY_API_KEY", "command printenv", "ls && declare -p", "printenv | grep KEY",
    "find . -maxdepth 0 -exec printenv \\;", "( export -p )", "true; { set; }",
]
# Forms that name the token indirectly: a name handed over in a variable or
# an argument, a here-document whose body expands the name, the environment
# file named from inside its folder or after a folder change into ``/proc``,
# and ``ps``'s BSD letter after a dashed option.
INDIRECT = [
    "n=PROXY_API_KEY; printenv $n",
    "bash -c 'printenv $0' PROXY_API_KEY",
    "cat <<EOF\n$PROXY_API_KEY\nEOF",
    "cat <<EOF\nthe key is ${OTO_CLI_TOKEN}\nEOF",
    "cd /proc/self && cat environ",
    "cd /proc/$$ ; tr '\\0' '\\n' < ./environ",
    "cd /proc && cat self/environ", "cd /proc/self\ncat envi*", "cd /proc/$$ && cat e*n",
    "pushd /proc/self; cat en*", "cd '/proc/self' && cat envi*", "true && cd /proc; cat */envi*",
    "ps -e e", "ps -u peter e", "ps --sort pid ewww",
    'n=PROXY_API_KEY; declare -p "$n"', "typeset -p `echo PROXY_API_KEY`",
]
# A process's environment file: asked where its path may be read at all.
ENVIRON_FILES = [
    "cat /proc/self/environ", "tr '\\0' '\\n' < /proc/$$/environ", "strings /proc/*/environ",
    "cat /proc/thread-self/environ", "xxd /proc/1234/task/1234/environ",
]
READS = [
    "printenv PATH", "printenv HOME USER", "set -e", "set -euo pipefail", "set -o", "set -- a b",
    "export X=1", "export NAME", "export PATH=$PATH:/x", "declare -A map", "declare -i n=1",
    "declare -f", "declare -F", "export -f fn", "readonly X=1", "local x",
    "grep -rn PROXY_API_KEY proxy/", "rg OTO_CLI_TOKEN", "echo PROXY_API_KEY", "echo $PROXY_URL",
    "echo $PROXY_API_KEY_NAME", "ps -e", "ps -ef", "ps aux", "ps axo pid,cmd", "ps -u peter",
    "ps -p 1 -o cmd", "jq .env file.json", "jq '.[] | .environment' f.json", "jq -r .name x.json",
    "awk '{print $1}' f", "cat /proc/cpuinfo", "ls /proc/self", "echo ${HOME}", "echo ${#arr[@]}",
    # Beside the indirect forms: an assignment's value is no name, an
    # option's value is no letter, and ``environ`` away from /proc is a word.
    "declare -i n=$x", "local x=$1", "readonly Y=$HOME/y", "export $n", "ps -eo pid,cmd",
    "ps -C sshd -o pid", "ps --ppid 1", "grep -c environ notes.txt",
    # A function listing prints no variable, whatever names the function.
    'declare -f "$fn"', "typeset -F $name",
]


@pytest.mark.parametrize("cmd", PRINTS + ENVIRON_FILES)
def test_the_reader_names_each_printing_form(cmd):
    assert reveals_session_token(cmd), cmd


@pytest.mark.parametrize("cmd", READS)
def test_the_reader_leaves_ordinary_reads_alone(cmd):
    assert not reveals_session_token(cmd), cmd


@pytest.mark.parametrize("cmd", PRINTS + WRAPPED + ENVIRON_FILES + INDIRECT)
def test_a_printing_command_asks_in_every_context(cmd):
    """Raised to the tier that asks, or refused outright where the context
    refuses the command anyway (a path it may not read): never a read."""
    for name, ctx in _matrix().items():
        d = path_shell._check_bash(cmd, ctx)
        tier = path_shell._TIER_ORDER.get(d.permission_tier or "read", 0)
        assert not d.allowed or tier >= _ASK, (cmd, name, d)


@pytest.mark.parametrize("cmd", [c for c in READS if not c.startswith(("cat /proc", "ls /proc"))])
def test_an_ordinary_read_keeps_the_read_tier(cmd):
    """What prints no token is not raised: a name with no expansion is text,
    and the builtins given a name print nothing."""
    ctx = _matrix()["editor"]
    d = path_shell._check_bash(cmd, ctx)
    assert d.allowed and d.permission_tier == "read", (cmd, d)


def test_bare_env_asked_before_the_guard_and_still_does():
    for cmd in ("env", "env | sort"):
        d = path_shell._check_bash(cmd, _matrix()["editor"])
        assert d.allowed and path_shell._TIER_ORDER[d.permission_tier] >= _ASK, (cmd, d)


def test_an_edit_tier_command_that_expands_the_token_asks():
    """``git`` is edit tier, which Auto runs with nobody asked: with the
    token expanded into it the command is the reviewer's."""
    ctx = _matrix()["editor"]
    assert path_shell._check_bash("git status", ctx).permission_tier == "edit"
    d = path_shell._check_bash("git -c http.extraheader=\"X: $PROXY_API_KEY\" fetch", ctx)
    assert d.allowed and d.permission_tier == "ask"


PS_PRINTS = [
    "gci env:", "Get-ChildItem Env:", "dir env:\\", "Get-Item Env:PROXY_API_KEY", "ls env:P*",
    "Write-Output $env:PROXY_API_KEY", "Write-Output ${env:OTO_CLI_TOKEN}", "$env:proxy_api_key",
    "[Environment]::GetEnvironmentVariables()",
    "(Get-Process -Id $PID).StartInfo.EnvironmentVariables",
]
PS_READS = [
    "Write-Output $env:PATH", "Write-Output ${env:USERPROFILE}", "Get-ChildItem C:\\Users",
    "Select-String PROXY_API_KEY *.py",
]


@pytest.mark.parametrize("cmd", PS_PRINTS)
def test_the_powershell_gate_asks_for_the_same(cmd):
    assert ps_reveals_session_token(cmd), cmd
    for name, ctx in _matrix().items():
        d = path_shell._check_powershell(cmd, ctx)
        tier = path_shell._TIER_ORDER.get(d.permission_tier or "read", 0)
        assert not d.allowed or tier >= _ASK, (cmd, name, d)


@pytest.mark.parametrize("cmd", PS_READS)
def test_the_powershell_gate_leaves_one_named_variable_a_read(cmd):
    assert not ps_reveals_session_token(cmd), cmd


# --- by mode, through the hook ------------------------------------------------

@pytest.fixture
def editors_chat(monkeypatch):
    """A person's headless chat session on the platform's sandbox."""
    sid = "sess-env-guard"
    monkeypatch.setattr("api.hooks.permission.verify_session_match_async",
                        AsyncMock(return_value=None))
    session_state._sessions[sid] = {"client_type": "dashboard"}
    session_state._session_security[sid] = SecurityContext(
        role="editor", username="erin", agent="demo", is_admin_agent=False)
    yield sid
    for store in (session_state._sessions, session_state._session_modes,
                  session_state._session_security, session_state._permission_emitters):
        store.pop(sid, None)


async def _decide(sid: str, command: str) -> dict:
    req = HookPermissionRequest(session_id=sid, tool_name="Bash", tool_input={"command": command})
    return await hook_permission(req, authorization=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["printenv PROXY_API_KEY", "echo $PROXY_API_KEY", "export -p", "set"])
async def test_in_ask_a_printing_command_raises_a_card_and_a_plain_read_runs(editors_chat, cmd):
    session_state.set_session_mode(editors_chat, "ask")
    assert (await _decide(editors_chat, "printenv PATH"))["decision"] == "allow"
    task = asyncio.create_task(_decide(editors_chat, cmd))
    prompt = await asyncio.wait_for(session_state.get_permission_queue(editors_chat).get(), timeout=2)
    assert (prompt["event_type"], prompt["tool_input"]) == ("permission_prompt", {"command": cmd})
    session_state.resolve_permission(prompt["request_id"], False)
    assert (await asyncio.wait_for(task, timeout=2))["decision"] == "deny"


@pytest.mark.asyncio
async def test_dont_ask_runs_it_and_plan_refuses_it(editors_chat):
    session_state.set_session_mode(editors_chat, "dontAsk")
    assert (await _decide(editors_chat, "printenv PROXY_API_KEY"))["decision"] == "allow"
    session_state.set_session_mode(editors_chat, "plan")
    assert (await _decide(editors_chat, "printenv PATH"))["decision"] == "allow"
    refused = await _decide(editors_chat, "printenv PROXY_API_KEY")
    assert refused["decision"] == "deny" and "Plan mode only reads" in refused["reason"]


@pytest.mark.parametrize("cmd", [
    "python3 -c 'import os; print(os.environ)'", "node -e 'console.log(process.env)'",
    "A=PROXY_API_KEY; eval echo \\$$A", 'x=PROXY_API_KEY; eval "echo \\$$x"',
    "cat /tmp/saved-earlier.txt",
])
def test_what_the_guard_does_not_see(cmd):
    """Named so nobody reads the guard as a wall: an interpreter that reads
    its own environment, a name the shell builds at run time (the ``eval``
    form) and a file written before are not printing forms. An
    interpreter asks anyway, as code it runs. The shell forms run with no
    card."""
    from auth.path_shell_env import command_reveals_session_token
    assert not reveals_session_token(cmd), cmd
    assert not command_reveals_session_token(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "cd /project", "cd /procfs-notes", "cd proc", "cd ./proc/self", "cd /home/u/proc",
    'grep -rn "cd /proc" notes/', "ls /proc", "abcd /proc",
])
def test_a_folder_change_elsewhere_is_no_change_into_proc(cmd):
    from auth.path_shell_env import command_reveals_session_token
    assert not command_reveals_session_token(cmd), cmd


def test_the_indirect_forms_ask():
    """Each form through the gate, as an editor."""
    ctx = _matrix()["editor"]
    for cmd in INDIRECT:
        d = path_shell._check_bash(cmd, ctx)
        assert d.allowed and d.permission_tier == "ask", (cmd, d)
    # A name the shell builds through ``eval`` stays a read: no cheap rule
    # reads it.
    d = path_shell._check_bash('x=PROXY_API_KEY; eval "echo \\$$x"', ctx)
    assert d.allowed and d.permission_tier == "read", d
