"""git commands that only read, for the gate's read-only profiles.

``git`` is edit tier in the shell classifier as a family (``path_shell``): a
push, a commit or a checkout changes things. Two of the gate's stages need
some of it anyway. A check's judge reads a repository's state (status, diff,
log, …: ``read_only_git``), and Plan runs git's version check alone
(``is_git_version``). The command is read token by token: a repository's
config or a flag can make a reading subcommand run a program (a pager, a diff
or text conversion driver) or write a file, and ``branch``, ``remote``,
``tag``, ``stash`` and ``worktree`` change things in most of their forms.
"""

from __future__ import annotations

import re
import shlex

_GIT = "git"
_GLOBAL_WITH_VALUE = frozenset({"-C", "--git-dir", "--work-tree"})
_READ_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "ls-tree",
    "describe", "shortlog", "grep", "cat-file",
})
# The forms of these that only list ("" is the subcommand alone).
_LISTING_FORMS = {
    "branch": frozenset({"", "-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "-l",
                         "--show-current", "--verbose", "--no-color"}),
    "remote": frozenset({"", "-v", "--verbose"}),
    "stash": frozenset({"list"}),
    "tag": frozenset({"-l", "--list"}),
    "worktree": frozenset({"list"}),
}
# Long options of the reading subcommands that write a file, run a program or
# read outside the repository. git accepts an unambiguous abbreviation of a
# long option (``--open`` is ``--open-files-in-pager``, ``--textc`` is
# ``--textconv``), so a name is compared by prefix both ways.
_UNSAFE_LONG = ("--output", "--open-files-in-pager", "--ext-diff", "--textconv",
                "--filters", "--no-index", "--exec")
# ``-O<pager>`` (grep) runs a program, and short options bundle
# (``-iO<pager>`` is ``-i -O<pager>``), so a single-dash token is read letter
# by letter for it; a token led by ``-c`` stays refused, as before.
_PAGER_SHORT = "O"
_LED_SHORT = "c"
_SHELL_JOINERS = frozenset("\n\r|;&<>`$")
_VERSION_RE = re.compile(r"[ \t]*git[ \t]+(?:--version|version)[ \t]*")


def is_git_version(command: str) -> bool:
    """Whether ``command`` is git's version check and nothing else."""
    return bool(command) and _VERSION_RE.fullmatch(command) is not None


def read_only_git(command: str) -> bool:
    """Whether ``command`` is one git invocation that only reads."""
    if not command or any(ch in _SHELL_JOINERS for ch in command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or tokens[0] != _GIT:
        return False
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        flag = tokens[i]
        if flag in _GLOBAL_WITH_VALUE and i + 1 < len(tokens):
            i += 2
        elif flag == "--no-pager" or flag.split("=", 1)[0] in ("--git-dir", "--work-tree"):
            i += 1
        else:
            return False
    if i >= len(tokens):
        return False
    sub, rest = tokens[i], tokens[i + 1:]
    if sub in _LISTING_FORMS:
        forms = _LISTING_FORMS[sub]
        return all(tok in forms for tok in rest) if rest else "" in forms
    if sub not in _READ_SUBCOMMANDS:
        return False
    return not any(_unsafe_option(tok) for tok in rest)


def _unsafe_option(token: str) -> bool:
    """Whether ``token`` names an option that writes a file, runs a program
    or reads outside the repository. A bare ``--`` is no option; the tokens
    after it are still read (a path that looks like an option is refused,
    the safe side)."""
    if token == "--":
        return False
    if token.startswith("--"):
        name = token.split("=", 1)[0]
        return any(u.startswith(name) or name.startswith(u) for u in _UNSAFE_LONG)
    if token.startswith("-") and len(token) > 1:
        return token[1] == _LED_SHORT or _PAGER_SHORT in token[1:]
    return False
