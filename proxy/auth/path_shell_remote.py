"""What an ``ssh`` in a shell command runs on another host, for the command
gate (``auth/path_shell.py``).

The command floor's host-state patterns (a write to ``/proc`` or ``/sys``,
a kernel module) judge the host a command runs on. The words ``ssh`` sends
to another host run there, so the gate judges them as that host's
(``remote_command``) and leaves them out of the text it judges as this
host's (``local_view``). Everything else stays local: the options and their
values (``-o ProxyCommand=…`` runs here), the destination, the redirections
outside quotes, and a word holding an unquoted or double-quoted
substitution, which this shell expands before ssh sends it.

The parse follows OpenSSH: options before the destination, a bundle's first
letter that takes a value takes the bundle's rest or the next word, ``--``
ends the options, options after the destination are parsed up to the first
word that is not one, and every word from there on is the remote command,
joined with spaces as ssh joins them; with none, the first ``-o
RemoteCommand`` is the remote command (ssh refuses both). ``ssh_call`` returns
the whole invocation (every option with its value, the destination, the
remote words) for the attached-host opening (``auth/path_shell_hosts.py``). A
command whose quoting does not close is not recognised (the shell refuses it
anyway). Only an ``ssh`` the line
runs directly is recognised, after a leading keyword, assignments and the
prefix wrappers; one inside ``bash -c`` or behind ``sudo`` keeps its whole
text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from auth.path_shell_subst import backtick_end, subst_end

# OpenSSH options that take a value (``ssh`` usage; ``P`` since 9.x).
_VALUE_OPTS = frozenset("BbcDEeFIiJLlmOopPQRSWw")
# Wrappers that run the command after them (their options, durations and
# assignments skipped), as the gate's own unwrap reads them.
_PREFIX_WRAPPERS = frozenset({
    "timeout", "nohup", "time", "stdbuf", "nice", "ionice", "setsid", "env", "exec",
    "command", "builtin",
})
# Keywords that take a command on the same line (``do ssh …``, ``{ ssh …``).
_LEADING_KEYWORDS = frozenset({"{", "!", "do", "then", "else", "elif", "if", "while", "until"})
_OPERATOR_CHARS = ";&|()\n"
# The ssh_config keyword whose argument is the remote command.
_REMOTE_COMMAND = "remotecommand"
_BLANKS = " \t"


@dataclass
class _Word:
    start: int
    end: int
    value: str
    expands_here: bool  # an unquoted or double-quoted substitution


@dataclass
class _Op:
    start: int
    end: int


class _Unclosed(ValueError):
    pass


def _read_word(s: str, i: int) -> tuple[_Word, int]:
    start, n = i, len(s)
    out: list[str] = []
    expands = False
    while i < n:
        c = s[i]
        if c in _BLANKS or c in _OPERATOR_CHARS or c in "<>":
            if c in "<>" and s[i + 1:i + 2] == "(":
                end, closed = subst_end(s, i)
                if not closed:
                    raise _Unclosed
                expands = True
                out.append(s[i:end])
                i = end
                continue
            break
        if c == "\\":
            if s[i + 1:i + 2] == "\n":
                i += 2
                continue
            out.append(s[i + 1:i + 2])
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise _Unclosed
            out.append(s[i + 1:j])
            i = j + 1
            continue
        if c == '"':
            i += 1
            while True:
                if i >= n:
                    raise _Unclosed
                d = s[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and s[i + 1:i + 2] in ('"', "\\", "$", "`", "\n"):
                    if s[i + 1] != "\n":
                        out.append(s[i + 1])
                    i += 2
                    continue
                if s[i:i + 2] == "$(":
                    end, closed = subst_end(s, i)
                    if not closed:
                        raise _Unclosed
                    expands = True
                    out.append(s[i:end])
                    i = end
                    continue
                if d == "`":
                    end, closed = backtick_end(s, i)
                    if not closed:
                        raise _Unclosed
                    expands = True
                    out.append(s[i:end])
                    i = end
                    continue
                out.append(d)
                i += 1
            continue
        if s[i:i + 2] == "$(":
            end, closed = subst_end(s, i)
            if not closed:
                raise _Unclosed
            expands = True
            out.append(s[i:end])
            i = end
            continue
        if c == "`":
            end, closed = backtick_end(s, i)
            if not closed:
                raise _Unclosed
            expands = True
            out.append(s[i:end])
            i = end
            continue
        out.append(c)
        i += 1
    return _Word(start, i, "".join(out), expands), i


def _redirection_end(s: str, i: int) -> int:
    """``s[i]`` starts a redirection operator (after any fd digits): the
    index past the operator; the target is the next word."""
    for op in ("<<<", "<<-", "&>>", "<<", "<>", "<&", ">>", ">&", ">|", "&>", "<", ">"):
        if s.startswith(op, i):
            return i + len(op)
    return i + 1


def _commands(s: str) -> list[list[_Word]]:
    """The simple commands of ``s``, each as its words; a redirection and its
    target are dropped (they are this shell's, never a command word)."""
    commands: list[list[_Word]] = []
    words: list[_Word] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in _BLANKS:
            i += 1
            continue
        if c == "\\" and s[i + 1:i + 2] == "\n":
            i += 2
            continue
        if c == "#" and (i == 0 or s[i - 1] in _BLANKS + _OPERATOR_CHARS):
            nl = s.find("\n", i)
            i = n if nl < 0 else nl
            continue
        j = i
        while j < n and s[j].isdigit():
            j += 1
        redir_at = j if j < n and s[j] in "<>" and s[j + 1:j + 2] != "(" else -1
        if redir_at < 0 and s.startswith("&>", i):
            redir_at = i
        if redir_at >= 0:
            i = _redirection_end(s, redir_at)
            while i < n and s[i] in _BLANKS:
                i += 1
            if i < n and s[i] not in _OPERATOR_CHARS:
                _, i = _read_word(s, i)
            continue
        if c in _OPERATOR_CHARS:
            if words:
                commands.append(words)
                words = []
            i += 1
            continue
        word, i = _read_word(s, i)
        words.append(word)
    if words:
        commands.append(words)
    return commands


def _is_assignment(value: str) -> bool:
    name, eq, _ = value.partition("=")
    return bool(eq) and name.isidentifier()


def _parse_options(words: list[_Word], i: int,
                   opts: list[tuple[str, str]]) -> tuple[int, bool]:
    """Read ssh options from ``words[i]`` into ``opts`` (each letter with its
    value, "" for a flag): the index of the first word that is not one, and
    whether ``--`` ended them."""
    while i < len(words):
        w = words[i].value
        if w == "--":
            return i + 1, True
        if not w.startswith("-") or w == "-":
            return i, False
        i += 1
        for k in range(1, len(w)):
            if w[k] in _VALUE_OPTS:
                if k + 1 == len(w):
                    opts.append((w[k], words[i].value if i < len(words) else ""))
                    i += 1   # the value is the next word
                else:
                    opts.append((w[k], w[k + 1:]))
                break
            opts.append((w[k], ""))
    return i, False


@dataclass(frozen=True)
class SshCall:
    """One ``ssh`` as OpenSSH reads it: its options in order (each letter with
    its value, "" for a flag; the ones after the destination too), the
    destination ("" when none) and the words it sends to the other host."""
    options: tuple[tuple[str, str], ...]
    destination: str
    remote: tuple[str, ...]


def _invocation(words: list[_Word]) -> tuple[list[tuple[str, str]], _Word | None, list[_Word]] | None:
    """The options, the destination word and the remote words of an ``ssh``
    command line, or None when the line does not run ssh."""
    i = 0
    while i < len(words) and words[i].value in _LEADING_KEYWORDS:
        i += 1
    while i < len(words) and _is_assignment(words[i].value):
        i += 1
    while i < len(words) and words[i].value.rsplit("/", 1)[-1] in _PREFIX_WRAPPERS:
        name = words[i].value.rsplit("/", 1)[-1]
        i += 1
        while i < len(words):
            v = words[i].value
            if name in ("command", "builtin") and v.startswith("-") and ("v" in v or "V" in v):
                return None
            if v.startswith("-") or _is_assignment(v) or v.replace(".", "", 1).rstrip("smhd").isdigit():
                i += 1
                continue
            break
    if i >= len(words) or words[i].value.rsplit("/", 1)[-1] != "ssh":
        return None
    opts: list[tuple[str, str]] = []
    i, ended = _parse_options(words, i + 1, opts)
    if i >= len(words):
        return opts, None, []
    destination = words[i]
    i += 1
    if not ended:
        i, _ = _parse_options(words, i, opts)
    return opts, destination, words[i:]


def _calls(text: str) -> list[tuple[list[tuple[str, str]], _Word | None, list[_Word]]]:
    try:
        commands = _commands(text)
    except _Unclosed:
        return []
    return [call for call in map(_invocation, commands) if call is not None]


def _sent(text: str) -> list[list[_Word]]:
    return [[w for w in remote if not w.expands_here] for _, _, remote in _calls(text) if remote]


def ssh_call(text: str) -> SshCall | None:
    """The first recognised ``ssh`` of ``text`` as OpenSSH reads it; None
    when ``text`` runs no ssh or its quoting does not close."""
    calls = _calls(text)
    if not calls:
        return None
    opts, destination, remote = calls[0]
    return SshCall(tuple(opts), destination.value if destination else "",
                   tuple(w.value for w in remote if not w.expands_here))


def ssh_keyword(value: str) -> tuple[str, str]:
    """An ``-o`` value as ssh_config reads it: the keyword, lower case, and
    its argument (``Key=Value``, ``Key Value``, ``Key = Value``; one level
    of double quotes around the argument removed)."""
    m = re.match(r"\s*([A-Za-z0-9]*)\s*=?\s*(.*?)\s*$", value, re.DOTALL)
    keyword, arg = (m.group(1), m.group(2)) if m else ("", value)
    if len(arg) >= 2 and arg[0] == arg[-1] == '"':
        arg = arg[1:-1]
    return keyword.lower(), arg


def local_view(text: str) -> str:
    """``text`` with the words every recognised ``ssh`` sends to another
    host blanked out (same length): what runs on this host."""
    spans = [(w.start, w.end) for words in _sent(text) for w in words]
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for k in range(start, end):
            if chars[k] != "\n":
                chars[k] = " "
    return "".join(chars)


def remote_command(text: str) -> str | None:
    """The command the first recognised ``ssh`` of ``text`` (a single
    command) runs on the other host, as ssh joins its words, or the first
    ``-o RemoteCommand`` when it sends none (ssh refuses both); None when
    ``text`` runs no ssh or the ssh sends no command."""
    call = ssh_call(text)
    if call is None:
        return None
    command = " ".join(call.remote)
    if not command:
        given = [arg for letter, value in call.options if letter == "o"
                 for keyword, arg in [ssh_keyword(value)] if keyword == _REMOTE_COMMAND]
        command = given[0] if given else ""
    return command or None
