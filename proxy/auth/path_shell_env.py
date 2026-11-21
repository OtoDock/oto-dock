"""The shell commands that print a session's own token.

A session's environment holds its own tokens (``PROXY_API_KEY``, the session
token; ``OTO_CLI_TOKEN``, git's; ``OTO_MCP_FETCH_TOKEN``), and the plain
ways of printing an environment sat in the read tier, which runs with nobody
asked. The command gate (``auth/path_shell.py``) raises the forms read here
to the tier that asks: a card in Ask, the engine's reviewer in Auto, a run in
Don't Ask.

A guard, never the boundary. A name the shell builds when the command runs
(``x=PROXY_API_KEY; eval "echo \\$$x"``) passes with no card. So do an
interpreter that reads its own environment, an encoded command and a
program the session wrote itself, though an interpreter asks anyway, as code
it runs. The boundary is where the token is accepted
(``middleware._session_refusal``).

Stdlib only, text in and a yes or no out.
"""

import re
import shlex

#: The names a session's own tokens ride its environment under.
TOKEN_ENV_NAMES = ("PROXY_API_KEY", "OTO_CLI_TOKEN", "OTO_MCP_FETCH_TOKEN")
_NAMES = "|".join(TOKEN_ENV_NAMES)

# ``$NAME``, ``${NAME``, ``${#NAME``, and an indirect expansion (``${!v}``,
# ``${!PROXY_*}``), whose name is known only when the command runs. A name
# with no expansion is text (``grep -rn PROXY_API_KEY src/`` stays a read).
_EXPANSION_RE = re.compile(rf"\$\{{?#?(?:{_NAMES})(?![A-Za-z0-9_])|\$\{{![A-Za-z_]")
# A process's environment file, whatever names the process: a pid, ``self``,
# ``$$``, a glob, a thread's.
_PROC_ENVIRON_RE = re.compile(r"/proc/\S*environ")
# The same file named from inside its folder (``cd /proc/self && cat
# environ``): a bare ``environ`` word in a command that names ``/proc/``.
_PROC = "/proc/"
_BARE_ENVIRON_RE = re.compile(r"""(?:^|[\s;&|<>'"(])(?:\./)?environ(?=$|[\s;&|<>'")])""")
# A folder change into ``/proc``: from there the file needs neither its
# folder's name (``cd /proc && cat self/environ``) nor its own in full
# (``cd /proc/self`` then ``cat envi*``).
_PROC_CD_RE = re.compile(r"""(?:^|[\s;&|(])(?:cd|pushd)\s+['"]?/proc(?=$|[/\s;&|'")])""")
# The builtins that list every variable with its value when they are given
# no name; ``set`` does with no argument at all.
_LISTING_BUILTINS = frozenset({"export", "declare", "typeset", "readonly", "local"})
# Those of them that print the value of a name they are given (``-p``).
_PRINTING_BUILTINS = frozenset({"declare", "typeset"})
_SET = "set"
_PRINTENV = "printenv"
_PS = "ps"
_JQ = "jq"
_AWK_FAMILY = frozenset({"awk", "gawk", "mawk", "nawk"})
# jq's ``env`` and ``$ENV`` (``.env`` is a field of the input).
_JQ_ENV_RE = re.compile(r"(?<![.\w$])env\b|\$ENV\b")
_AWK_ENVIRON = "ENVIRON"
# Function listings print no variable.
_FUNCTION_FLAGS = frozenset("fF")
# ``ps`` options that take a value, which is no BSD letter: the short ones
# (the last letter of a cluster) and the long ones written without ``=``.
_PS_VALUE_LETTERS = frozenset("CGgpqstUuoOk")
_PS_VALUE_LONG = frozenset({
    "--pid", "--ppid", "--sid", "--tty", "--user", "--User", "--group", "--Group",
    "--format", "--sort", "--cols", "--columns", "--width", "--rows", "--lines", "--quick-pid",
})


# A redirect among a command's words: ``> f``, ``>f``, ``2>&1``, ``< f``.
_REDIRECT_RE = re.compile(r"^\d*(?:&>>?|>>?&?|<<?<?)(.*)$")


def _words(segment: str) -> list[str]:
    """The command's words: leading assignments and redirects dropped, so
    ``set > vars.txt`` reads as ``set``."""
    try:
        words = shlex.split(segment)
    except ValueError:
        return []
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words = words[1:]
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        m = _REDIRECT_RE.match(word)
        if m:
            skip = not m.group(1)  # a bare operator takes the next word
            continue
        out.append(word)
    return out


def _names_a_token(args: list[str]) -> bool:
    return any(arg in TOKEN_ENV_NAMES for arg in args)


def _computed_name(args: list[str]) -> bool:
    """An operand the shell fills in when the command runs (``printenv $n``,
    ``declare -p "$1"``): it may be a token's name. An assignment's value
    (``n=$x``) is no name."""
    return any(("$" in arg or "`" in arg) and "=" not in arg for arg in args)


def _lists_variables(name: str, args: list[str]) -> bool:
    """``set`` alone, and ``export`` / ``declare`` / ``typeset`` /
    ``readonly`` / ``local`` with no name to act on (``-p`` and ``-x``
    included), print every variable with its value; with a token's name
    they print that one. ``set -e``, ``export NAME``, ``export X=1`` and
    ``declare -A map`` print nothing."""
    if name == _SET:
        return not args
    flags = [a for a in args if a[:1] in "-+"]
    operands = [a for a in args if a[:1] not in "-+"]
    if _names_a_token(operands):
        return True
    functions_only = any(_FUNCTION_FLAGS & set(flag[1:]) for flag in flags)
    if name in _PRINTING_BUILTINS and _computed_name(operands) and not functions_only:
        return True
    if operands:
        return False
    return not functions_only


def _ps_shows_environments(args: list[str]) -> bool:
    """``ps``'s BSD ``e`` letter prints each process's environment after its
    command (``ps e``, ``ps auxe``, ``ps eww``, ``ps -e e``). BSD letters
    carry no dash, wherever they stand; ``-e`` is every process, not an
    environment, and an option's value (``-u peter``) is no letter."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg.startswith("--"):
            skip = arg in _PS_VALUE_LONG
        elif arg.startswith("-"):
            skip = arg[-1:] in _PS_VALUE_LETTERS
        elif arg.isalpha() and "e" in arg:
            return True
    return False


def reveals_session_token(segment: str) -> bool:
    """Whether one Bash segment, its shell structure peeled, prints the
    session's own token or the environment that holds it."""
    if _EXPANSION_RE.search(segment) or _PROC_ENVIRON_RE.search(segment):
        return True
    words = _words(segment)
    if not words:
        return False
    name, args = words[0].rsplit("/", 1)[-1], words[1:]
    if name == _PRINTENV:
        operands = [a for a in args if not a.startswith("-")]
        return not operands or _names_a_token(operands) or _computed_name(operands)
    if name == _SET or name in _LISTING_BUILTINS:
        return _lists_variables(name, args)
    if name == _PS:
        return _ps_shows_environments(args)
    if name == _JQ:
        return bool(_JQ_ENV_RE.search(segment))
    if name in _AWK_FAMILY:
        return _AWK_ENVIRON in segment
    return False


def command_reveals_session_token(command: str) -> bool:
    """The same question of a whole command, before it is split: an
    expansion of a token's name anywhere in it (a here-document's body is no
    segment), a folder change into ``/proc``, and the environment file named
    from inside its folder."""
    if _EXPANSION_RE.search(command) or _PROC_CD_RE.search(command):
        return True
    return _PROC in command and bool(_BARE_ENVIRON_RE.search(command))


# PowerShell: the ``Env:`` drive read as a drive (``gci env:``, ``Get-Item
# Env:NAME``, ``dir env:P*``; ``$env:PATH`` reads one variable and stays a
# read), ``$env:`` with a token's name, and the .NET readers
# (``GetEnvironmentVariables``, a process's ``EnvironmentVariables``).
_PS_ENV_DRIVE_RE = re.compile(r"(?<![$\w{])env:", re.IGNORECASE)
_PS_ENV_TOKEN_RE = re.compile(rf"\$\{{?env:(?:{_NAMES})(?![A-Za-z0-9_])", re.IGNORECASE)
_PS_ENV_READER = "environmentvariable"


def ps_reveals_session_token(segment: str) -> bool:
    """``reveals_session_token`` for one PowerShell segment."""
    return bool(_PS_ENV_DRIVE_RE.search(segment) or _PS_ENV_TOKEN_RE.search(segment)
                or _PS_ENV_READER in segment.lower())
