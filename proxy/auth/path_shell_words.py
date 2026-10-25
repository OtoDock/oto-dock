"""Shell words as the shell reads them, for the command gate's floor.

The catastrophe floor (``auth/path_shell.py``) reads command text. The shell
removes quotes, resolves escapes, decodes ``$'…'`` and expands brace groups
before a command runs, so a root written through any of them (``$'/'``,
``""/``, ``r''m``, ``/{,}``) is the bare root to the shell and a different
string to a pattern. ``words_view`` writes a command's words as their values,
so the floor can read the form the shell runs. The view is a second reading:
the raw text is always read too, and a view that cannot be built is no view.

Also here: the recursive delete's matcher (``RootDelete``), which reads the
flag run token by token, the awk program reader (``awk_runs_commands``),
the remote operand of ``rsync`` and ``scp`` (``remote_operand``), and
``rsync``'s arguments as rsync reads them (``rsync_words``: the remote shells
it runs, its ``--rsync-path`` commands, its operands).
"""

import posixpath
import re
import shlex
from typing import NamedTuple

from auth.path_shell_subst import backtick_end, subst_end

# Characters the view writes back inside single quotes: a value holding one
# would read as structure (a separator, a quote, an expansion) if written
# bare. A glob or tilde character that was quoted in the source is quoted
# again (``_GLOB``), so a literal ``*`` never reads as a glob.
_SPECIAL = frozenset(" \t\n'\"\\$`;&|<>()#")
_GLOB = frozenset("*?[]{}~")
_OPERATOR = frozenset(";&|<>()")
_BLANK = frozenset(" \t")
_MAX_BRACE_WORDS = 16

_ANSI_NAMED = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
               "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
_HEX = "0123456789abcdefABCDEF"


class _Unreadable(Exception):
    """The text is not a complete shell form (an open quote)."""


def _ansi_c(s: str, i: int) -> tuple[str, int]:
    """``s[i]`` follows ``$'``. Return the decoded value and the index past
    the closing quote. The value ends at its first NUL, as in bash, where
    ``$'/\\0x'`` is ``/``."""
    out: list[str] = []
    n = len(s)
    while i < n:
        c = s[i]
        if c == "'":
            value = "".join(out)
            return value.split("\0", 1)[0], i + 1
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = s[i + 1]
        i += 2
        if e in _ANSI_NAMED:
            out.append(_ANSI_NAMED[e])
        elif e in "01234567":
            j = i
            while j < n and j < i + 2 and s[j] in "01234567":
                j += 1
            out.append(chr(int(s[i - 1:j], 8) & 0xFF))
            i = j
        elif e in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[e]
            j = i
            while j < n and j < i + width and s[j] in _HEX:
                j += 1
            if j == i:
                out.append("\\" + e)
            else:
                code = int(s[i:j], 16)
                out.append(chr(code) if code <= 0x10FFFF else "�")
                i = j
        elif e == "c" and i < n:
            out.append(chr(ord(s[i]) & 0x1F))
            i += 1
        else:
            out.append("\\" + e)
    raise _Unreadable


def _expansion_end(s: str, i: int) -> int:
    """``s[i]`` is an unquoted or double-quoted ``$`` that does not open
    ``$'`` or ``$"``. Return the index past the expansion, copied verbatim
    by the caller: a substitution, ``${…}`` or a parameter name."""
    n = len(s)
    if i + 1 < n and s[i + 1] == "(":
        return subst_end(s, i)[0]
    if i + 1 < n and s[i + 1] == "{":
        j = s.find("}", i + 2)
        return n if j < 0 else j + 1
    j = i + 1
    if j < n and (s[j].isalnum() or s[j] == "_"):
        while j < n and (s[j].isalnum() or s[j] == "_"):
            j += 1
        return j
    return min(j + 1, n)


def _double_quoted(s: str, i: int, word: list[tuple[str, bool]]) -> int:
    """``s[i]`` follows an opening ``"``. Append the quoted value to
    ``word`` (every character quoted) and return the index past the
    closing quote."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            return i + 1
        if c == "\\" and i + 1 < n:
            e = s[i + 1]
            if e == "\n":
                i += 2
                continue
            if e in '$`"\\':
                word.append((e, True))
                i += 2
                continue
            word.append(("\\", True))
            i += 1
            continue
        if c == "$" and i + 1 < n and s[i + 1] not in "\"'":
            end = _expansion_end(s, i)
            word.extend((ch, True) for ch in s[i:end])
            i = end
            continue
        if c == "`":
            end = backtick_end(s, i)[0]
            word.extend((ch, True) for ch in s[i:end])
            i = end
            continue
        word.append((c, True))
        i += 1
    raise _Unreadable


def _brace_words(word: list[tuple[str, bool]]) -> list[list[tuple[str, bool]]]:
    """The words a word's unquoted, one-level brace groups with a comma
    expand into, as bash expands them before the command runs (``/{,}`` is
    ``/ /``, ``{/,}{,}`` is ``/ /``): each group in turn, while the words
    stay within ``_MAX_BRACE_WORDS`` (past it, the expansion so far)."""
    words = [word]
    while True:
        out: list[list[tuple[str, bool]]] = []
        for w in words:
            out.extend(_brace_once(w))
        if len(out) == len(words) or len(out) > _MAX_BRACE_WORDS:
            return words
        words = out


def _brace_once(word: list[tuple[str, bool]]) -> list[list[tuple[str, bool]]]:
    """The words a word's first expandable brace group gives; the word
    itself when it has none (a nested group, a group with no comma, more
    than ``_MAX_BRACE_WORDS`` words)."""
    opens = [k for k, (c, q) in enumerate(word) if c == "{" and not q]
    for start in opens:
        depth, commas, end = 0, [], -1
        for k in range(start, len(word)):
            c, q = word[k]
            if q:
                continue
            if c == "{":
                depth += 1
                if depth > 1:
                    break
            elif c == "}":
                end = k
                break
            elif c == "," and depth == 1:
                commas.append(k)
        if end < 0 or not commas or len(commas) + 1 > _MAX_BRACE_WORDS:
            continue
        bounds = [start, *commas, end]
        head, tail = word[:start], word[end + 1:]
        return [head + word[a + 1:b] + tail for a, b in zip(bounds, bounds[1:], strict=False)]
    return [word]


def _written(word: list[tuple[str, bool]]) -> str:
    """One word's value as the view writes it: bare, or single-quoted when it
    holds a character that would read as structure or a quoted glob."""
    value = "".join(c for c, _ in word)
    if not value:
        return "''"
    if any(c in _SPECIAL for c in value) or any(q and c in _GLOB for c, q in word):
        return "'" + value.replace("'", "'\\''") + "'"
    return value


def words_view(text: str) -> str | None:
    """``text`` with every shell word written as its value; None when the
    text is not a complete shell form. Operators, blanks, newlines and
    comments stay where they are; a substitution, a parameter expansion and a
    nested brace group are copied verbatim (their value is known only when
    the command runs)."""
    out: list[str] = []
    word: list[tuple[str, bool]] | None = None
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal word
        if word is None:
            return
        expanded = _brace_words(word)
        if len(expanded) > 1:
            out.append(" ".join(_written(w) for w in expanded if w))
        else:
            out.append(_written(word))
        word = None

    try:
        while i < n:
            c = text[i]
            if word is None and c == "#" and (not out or out[-1][-1:] in (" ", "\t", "\n", ";", "&", "|", "(")):
                end = text.find("\n", i)
                end = n if end < 0 else end
                out.append(text[i:end])
                i = end
                continue
            if c in _BLANK or c == "\n":
                flush()
                out.append(c)
                i += 1
                continue
            if c in _OPERATOR:
                if c in "<>" and i + 1 < n and text[i + 1] == "(":
                    end = subst_end(text, i)[0]
                    word = (word or []) + [(ch, True) for ch in text[i:end]]
                    i = end
                    continue
                flush()
                out.append(c)
                i += 1
                continue
            if word is None:
                word = []
            if c == "\\":
                if i + 1 < n and text[i + 1] == "\n":
                    i += 2
                    continue
                if i + 1 < n:
                    word.append((text[i + 1], True))
                i += 2
                continue
            if c == "'":
                end = text.find("'", i + 1)
                if end < 0:
                    raise _Unreadable
                word.extend((ch, True) for ch in text[i + 1:end])
                i = end + 1
                continue
            if c == '"':
                i = _double_quoted(text, i + 1, word)
                continue
            if c == "$" and i + 1 < n and text[i + 1] == "'":
                value, i = _ansi_c(text, i + 2)
                word.extend((ch, True) for ch in value)
                continue
            if c == "$" and i + 1 < n and text[i + 1] == '"':
                i = _double_quoted(text, i + 2, word)
                continue
            if c == "$":
                end = _expansion_end(text, i)
                word.extend((ch, True) for ch in text[i:end])
                i = end
                continue
            if c == "`":
                end = backtick_end(text, i)[0]
                word.extend((ch, True) for ch in text[i:end])
                i = end
                continue
            word.append((c, False))
            i += 1
        flush()
    except _Unreadable:
        return None
    return "".join(out)


def has_shell_quoting(text: str) -> bool:
    """``text`` holds something the view would change: a quote, an escape
    or a brace group."""
    return any(c in text for c in "'\"\\{")


# --- the recursive delete of the root or the home ---------------------------

_RM_START = re.compile(r"\brm(?=[ \t])")
_RM_TOKEN = re.compile(r"[ \t]+([^ \t\n;&|]{1,1024})")
_RM_ROOT = re.compile(r"(['\"]?)(?:/|~/?)(?:\*[^ \t\n;&|'\"]*)?\1")
_RM_MAX_TOKENS = 33


def _root_operand(token: str) -> bool:
    """``token`` names the root or the home, or a glob right under them,
    written as it is or with repeated slashes and dot segments (``//``,
    ``/./*``, ``/..``: the same directory to the kernel). Only a word that
    starts at the root or the home folds: ``./~`` is a file named ``~``."""
    if _RM_ROOT.fullmatch(token):
        return True
    quote = token[0] if len(token) > 1 and token[0] in "'\"" and token[-1] == token[0] else ""
    body = token[1:-1] if quote else token
    if "/" not in body or not body.startswith(("/", "~")):
        return False
    folded = posixpath.normpath(re.sub(r"/+", "/", body))
    return folded != body and bool(_RM_ROOT.fullmatch(folded))


def _long_flag(token: str, name: str) -> bool:
    """``token`` is ``--<name>`` or an abbreviation GNU rm accepts for it
    (``--rec``; one letter is unambiguous for both names rm takes here)."""
    body = token[2:].split("=", 1)[0]
    return bool(body) and name.startswith(body)


class RootDelete:
    """A recursive, forced ``rm`` of the root or the home: the recursive and
    force flags anywhere in the flag run, in one cluster or apart, short or
    long (``rm -rf /``, ``rm -r -f /``, ``rm --recursive -f ~``, ``rm -v -rf
    /``), and an operand that is the root, the home, or a glob right under
    them, quoted or not, before or after ``--``. The operand ends at a blank,
    a separator or the very end of the text: a newline ends the command's
    line, and the raw text the floor reads also holds heredoc data (the
    multi-line forms are read per segment). Each command is one bounded
    pass: the gate runs on the event loop."""

    def search(self, text: str) -> bool | None:
        if "rm" not in text:
            return None
        for start in _RM_START.finditer(text):
            if self._command_at(text, start.end()):
                return True
        return None

    @staticmethod
    def _command_at(text: str, pos: int) -> bool:
        recursive = force = root = False
        options_done = False
        for _ in range(_RM_MAX_TOKENS):
            m = _RM_TOKEN.match(text, pos)
            if m is None:
                break
            token, pos = m.group(1), m.end()
            if token.startswith("#"):
                break
            ends_here = pos == len(text) or text[pos] in " \t;&|"
            if not options_done and token == "--":
                options_done = True
            elif not options_done and token.startswith("--"):
                recursive = recursive or _long_flag(token, "recursive")
                force = force or _long_flag(token, "force")
            elif not options_done and token.startswith("-") and len(token) > 1:
                letters = token[1:]
                recursive = recursive or "r" in letters or "R" in letters
                force = force or "f" in letters
            elif ends_here and _root_operand(token):
                root = True
            if pos < len(text) and text[pos] in "\n;&|":
                break
        return recursive and force and root


# --- awk ---------------------------------------------------------------------

# awk's long options by what they take (gawk accepts any unambiguous prefix,
# ``--fil=``, so a long option counts as every name it is a prefix of; its
# ``-W name[=value]`` is the same long option). The program from a file:
# ``--file``, ``--exec``; program text: ``--source``; code from elsewhere:
# ``--include`` (an awk file), ``--load`` (a native extension); a file awk
# writes itself: ``--dump-variables``, ``--pretty-print``, ``--profile``,
# ``--debug``; a value: ``--assign``, ``--field-separator``.
_AWK_PROGRAM_LONG = ("file", "exec")
_AWK_SOURCE_LONG = ("source",)
_AWK_CODE_LONG = ("include", "load", "dump-variables", "pretty-print", "profile", "debug")
_AWK_VALUE_LONG = ("assign", "field-separator")
# Short options, read as getopt reads a cluster (``-bf p.awk`` is ``-b -f
# p.awk``): the letters before the first one that takes a value are plain
# switches. ``-f``/``-E`` the program from a file, ``-i``/``-l`` code from
# elsewhere, ``-d``/``-o``/``-p``/``-D`` a file awk writes (their value only
# in the cluster itself), ``-e`` program text, ``-v``/``-F``/``-W`` a value
# (the rest of the cluster, or the next argument).
_AWK_CODE_LETTERS = frozenset("fEildopD")
_AWK_VALUE_LETTERS = frozenset("eEfFilvW")
_AWK_OPTIONAL_LETTERS = frozenset("dDop")
# A call that starts a command or code the gate does not see: ``system(``,
# ``getline``, ``@load``, ``@include``, read on the whole program
# (deliberately broad: in a string too), and gawk's indirect call
# ``@name(`` (``f = "system"; @f("id")``), read on the code alone so an
# address in a string still reads.
_AWK_RUNS = re.compile(r"\bsystem[ \t]*\(|\bgetline\b|@load\b|@include\b")
_AWK_INDIRECT_CALL = re.compile(r"@[A-Za-z_]\w*[ \t]*\(")


def _awk_long(arg: str, names: tuple[str, ...]) -> tuple[bool, bool]:
    """(the long option ``arg`` is one of ``names``, its value is the next
    argument)."""
    body, eq, _ = arg[2:].partition("=")
    if body and any(name.startswith(body) for name in names):
        return True, not eq
    return False, False


def _awk_cluster(arg: str) -> tuple[str, str]:
    """A single-dash argument read as a getopt cluster: the first letter
    that takes a value (or writes a file) and the rest of the cluster after
    it ("" when its value is the next argument); ("", "") when every letter
    is a plain switch."""
    for i, ch in enumerate(arg[1:], start=2):
        if ch in _AWK_VALUE_LETTERS or ch in _AWK_OPTIONAL_LETTERS:
            return ch, arg[i:]
    return "", ""


def _awk_w_kind(value: str) -> str:
    """What a ``-W name[=value]`` names: "code" (a program file, code from
    elsewhere, a file awk writes, mawk's ``-W exec``), "source" (program
    text), "" otherwise."""
    as_long = "--" + value
    if _awk_long(as_long, _AWK_PROGRAM_LONG + _AWK_CODE_LONG)[0]:
        return "code"
    if _awk_long(as_long, _AWK_SOURCE_LONG)[0]:
        return "source"
    return ""


def awk_option(arg: str) -> tuple[bool, bool]:
    """For the path extraction: (``arg`` supplies the program, so every
    positional is an input file; its value is the next argument)."""
    if arg.startswith("--"):
        for names, program in ((_AWK_PROGRAM_LONG + _AWK_SOURCE_LONG, True),
                               (_AWK_CODE_LONG + _AWK_VALUE_LONG, False)):
            hit, takes = _awk_long(arg, names)
            if hit:
                return program, takes
        return False, False
    letter, rest = _awk_cluster(arg)
    if letter in ("f", "E", "e"):
        return True, not rest
    if letter in _AWK_VALUE_LETTERS:
        return False, not rest
    return False, False


def _awk_code(program: str) -> str:
    """The program's code outside its string and regex literals and its
    comments: the text a pipe is looked for in. A ``/`` opens a regex where
    an operand cannot end: at the start, after an operator, a brace, an
    opening parenthesis, a comma, a semicolon or a newline. After ``+`` or ``-`` it
    is read as a division (``i++ / 2``): a regex read as code only costs a
    prompt, code read as a regex would hide a pipe."""
    out: list[str] = []
    i, n = 0, len(program)
    prev = ""
    while i < n:
        c = program[i]
        if c == '"':
            j = i + 1
            while j < n and program[j] != '"':
                j += 2 if program[j] == "\\" else 1
            i = j + 1
            prev = '"'
            continue
        if c == "/" and (prev == "" or prev in "(,{};!~&|=:?\n*%<>^"):
            j = i + 1
            while j < n and program[j] not in "/\n":
                if program[j] == "[":
                    k = program.find("]", j + 2)
                    j = n if k < 0 else k + 1
                    continue
                j += 2 if program[j] == "\\" else 1
            i = j + 1
            prev = "/"
            continue
        if c == "#":
            j = program.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(c)
        if not c.isspace() or c == "\n":
            prev = c
        i += 1
    return "".join(out)


def awk_runs_commands(segment: str) -> bool:
    """An awk command that can start another program, run code the gate
    does not see, or write a file of its own: its program calls
    ``system(``, uses ``getline`` (a command's output, or a file the path
    check never reads: deliberately broad), pipes to or from a command (a
    ``|`` in its code, ``||`` aside), loads or includes code, calls a
    function by name (gawk's ``@name(``), or an option, a cluster's letter
    included (``-bf p.awk``), gives it code from a file or an extension, or
    names a file it writes."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return True
    while tokens and "=" in tokens[0] and tokens[0].split("=", 1)[0].isidentifier():
        tokens = tokens[1:]
    args = tokens[1:]
    programs: list[str] = []
    positional: list[str] = []
    from_flag = False
    k = 0
    while k < len(args):
        arg = args[k]
        k += 1
        if arg == "--":
            positional.extend(args[k:])
            break
        if not arg.startswith("-") or arg == "-":
            positional.append(arg)
            continue
        if arg.startswith("--"):
            if _awk_long(arg, _AWK_PROGRAM_LONG + _AWK_CODE_LONG)[0]:
                return True
            hit, takes = _awk_long(arg, _AWK_SOURCE_LONG)
            if hit:
                from_flag = True
                if takes:
                    programs.append(args[k] if k < len(args) else "")
                    k += 1
                else:
                    programs.append(arg.split("=", 1)[1])
                continue
            hit, takes = _awk_long(arg, _AWK_VALUE_LONG)
            if hit and takes:
                k += 1
            continue
        letter, value = _awk_cluster(arg)
        if letter in _AWK_CODE_LETTERS:
            return True
        if letter in _AWK_VALUE_LETTERS and not value:
            value = args[k] if k < len(args) else ""
            k += 1
        if letter == "e":
            from_flag = True
            programs.append(value)
        elif letter == "W":
            kind = _awk_w_kind(value)
            if kind == "code":
                return True
            if kind == "source":
                from_flag = True
                programs.append(value.split("=", 1)[1] if "=" in value else "")
    if not from_flag and positional:
        programs.append(positional[0])
    for program in programs:
        if _AWK_RUNS.search(program):
            return True
        code = _awk_code(program)
        if _AWK_INDIRECT_CALL.search(code) or re.search(r"(?<!\|)\|(?!\|)", code):
            return True
    return False


# --- rsync and scp -----------------------------------------------------------

def remote_operand(arg: str) -> bool:
    """An ``rsync``/``scp`` operand that names another host, never a local
    path: ``rsync://…``, a bracketed host followed by ``:`` (``[::1]:p``,
    ``user@[::1]:p``), or a ``:`` at index 1 or later before the first ``/``
    (``host:p``, ``user@host:``, ``host::module``). A one-letter host before
    nothing, ``/`` or ``\\`` is a Windows drive (local); a leading ``:``,
    ``./a:b`` and ``dir/a:b`` are local."""
    if arg.lower().startswith("rsync://"):
        return True
    bracket = arg.find("[")
    if bracket >= 0 and (bracket == 0 or arg[bracket - 1] == "@"):
        close = arg.find("]", bracket)
        return close > 0 and arg[close + 1:close + 2] == ":"
    colon = arg.find(":")
    if colon < 1:
        return False
    slash = arg.find("/")
    if 0 <= slash < colon:
        return False
    return not (colon == 1 and arg[0].isalpha() and arg[2:3] in ("", "/", "\\"))


# rsync's short options that take a value, in a getopt cluster (``-avze
# ssh``): -B --block-size, -e --rsh, -f --filter, -M --remote-option,
# -T --temp-dir, -@ --modify-window. A letter after the first of them is
# part of that option's value. rsync takes no abbreviated long option.
_RSYNC_VALUE_LETTERS = frozenset("BefMT@")
_RSYNC_RSH = "--rsh"
# The remote shell rsync runs when none is named: naming it adds nothing.
_RSYNC_DEFAULT_RSH = "ssh"
# The command rsync runs on the other host in place of its own.
_RSYNC_PATH = "--rsync-path"
# rsync's long options whose value may be the next word.
_RSYNC_LONG_VALUES = frozenset({
    "--address", "--backup-dir", "--block-size", "--bwlimit", "--checksum-choice",
    "--checksum-seed", "--chmod", "--chown", "--compare-dest", "--compress-choice",
    "--compress-level", "--contimeout", "--copy-as", "--copy-dest", "--debug",
    "--early-input", "--exclude", "--exclude-from", "--files-from", "--filter",
    "--groupmap", "--iconv", "--include", "--include-from", "--info", "--link-dest",
    "--log-file", "--log-file-format", "--max-alloc", "--max-delete", "--max-size",
    "--min-size", "--modify-window", "--only-write-batch", "--out-format", "--outbuf",
    "--partial-dir", "--password-file", "--port", "--protocol", "--read-batch",
    "--remote-option", "--skip-compress", "--sockopts", "--stderr", "--stop-after",
    "--stop-at", "--suffix", "--temp-dir", "--timeout", "--usermap", "--write-batch",
})


def rsync_shell_option(arg: str) -> tuple[bool, str | None]:
    """Whether one rsync argument names the remote shell (``-e``, ``--rsh``,
    a cluster whose first value-taking letter is ``e``), with its value
    when the argument carries it (``-essh``, ``--rsh=ssh``), None when the
    value is the next word."""
    if arg == _RSYNC_RSH:
        return True, None
    if arg.startswith(_RSYNC_RSH + "="):
        return True, arg[len(_RSYNC_RSH) + 1:]
    if arg.startswith("--") or not arg.startswith("-"):
        return False, None
    for i, letter in enumerate(arg[1:], 1):
        if letter in _RSYNC_VALUE_LETTERS:
            return letter == "e", (arg[i + 1:] or None)
    return False, None


class RsyncWords(NamedTuple):
    """An ``rsync`` segment's arguments as rsync reads them: the remote
    shells it names, its ``--rsync-path`` commands and its operands."""
    shells: list[str]
    rsync_paths: list[str]
    operands: list[str]


def rsync_words(segment: str) -> RsyncWords:
    """Read an ``rsync`` segment (popt: options anywhere before ``--``; a
    long option's value fused or the next word). A long option this table
    does not know takes no value here, so its value reads as an operand: a
    stricter answer, never a looser one."""
    words = RsyncWords([], [], [])
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return words
    while tokens and "=" in tokens[0] and tokens[0].split("=", 1)[0].isidentifier():
        name, assigned = tokens.pop(0).split("=", 1)
        if name == "RSYNC_RSH":
            words.shells.append(assigned)
    k, done = 1, False
    while k < len(tokens):
        arg = tokens[k]
        k += 1
        if done or not arg.startswith("-") or arg == "-":
            words.operands.append(arg)
            continue
        if arg == "--":
            done = True
            continue
        is_shell, value = rsync_shell_option(arg)
        name, eq, fused = arg.partition("=")
        if is_shell or name == _RSYNC_PATH:
            if not is_shell:
                value = fused if eq else None
            if value is None:
                value, k = (tokens[k] if k < len(tokens) else ""), k + 1
            (words.shells if is_shell else words.rsync_paths).append(value)
        elif name in _RSYNC_LONG_VALUES and not eq:
            k += 1
        elif not arg.startswith("--"):
            letters = arg[1:]
            for i, letter in enumerate(letters):
                if letter in _RSYNC_VALUE_LETTERS:
                    k += 0 if letters[i + 1:] else 1
                    break
    return words


def rsync_remote_shells(segment: str) -> list[str]:
    """The remote shells an ``rsync`` segment names (``-e CMD``,
    ``--rsh=CMD``, a leading ``RSYNC_RSH=CMD``): rsync runs each as a
    command, so each is checked as one, never as a path. A bare ``ssh`` is
    the shell rsync runs anyway and is no extra command."""
    return [shell for shell in rsync_words(segment).shells
            if shell.strip() not in ("", _RSYNC_DEFAULT_RSH)]


def rsync_reaches_host(segment: str) -> bool:
    """Whether an ``rsync`` copies to or from another host (an operand on
    one): scp's gate, where a local copy keeps the edit tier."""
    return any(remote_operand(arg) for arg in rsync_words(segment).operands)


# scp's options that take a value (``-c -D -F -i -J -l -o -P -S -X``).
SCP_VALUE_LETTERS = frozenset("cDFiJloPSX")
# What a shell reads as more than one word, or as an expansion, in a path.
_SHELL_SEPARATORS = frozenset(";|&$`\n")
_SCP_SCHEME = "scp://"
_RSYNC_SCHEME = "rsync://"


def scp_operands(segment: str) -> list[str]:
    """An ``scp`` segment's operands (getopt: a value letter takes the rest
    of its cluster or the next word, ``--`` ends the options)."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return []
    while tokens and "=" in tokens[0] and tokens[0].split("=", 1)[0].isidentifier():
        tokens.pop(0)
    operands: list[str] = []
    k, done = 1, False
    while k < len(tokens):
        arg = tokens[k]
        k += 1
        if done or not arg.startswith("-") or arg == "-":
            operands.append(arg)
        elif arg == "--":
            done = True
        else:
            for i, letter in enumerate(arg[1:], 1):
                if letter in SCP_VALUE_LETTERS:
                    k += 0 if arg[i + 1:] else 1
                    break
    return operands


def _remote_path(arg: str) -> str | None:
    """The path of a remote ``scp``/``rsync`` operand, as the far side gets
    it; None for an rsync server operand (``host::module``, ``rsync://``),
    which no shell reads."""
    low = arg.lower()
    if low.startswith(_RSYNC_SCHEME):
        return None
    if low.startswith(_SCP_SCHEME):
        tail = arg[len(_SCP_SCHEME):]
        return tail[tail.find("/") + 1:] if "/" in tail else ""
    close = arg.find("]") if arg.find("[") >= 0 else -1
    rest = arg[close + 2:] if close >= 0 else arg[arg.find(":") + 1:]
    return None if rest.startswith(":") else rest


def remote_shell_paths(cmd_name: str, segment: str) -> list[str]:
    """The remote paths of an ``scp`` or ``rsync`` that a shell on the far
    side may read as a command line: legacy scp (``-O``) and rsync's
    ``--old-args`` hand the path to the far side's shell, so a separator or
    an expansion in it runs there. Each is judged as the other host's
    command; a path with none of them is a file name either way."""
    if cmd_name == "scp":
        operands = scp_operands(segment)
    elif cmd_name == "rsync":
        operands = rsync_words(segment).operands
    else:
        return []
    paths = (_remote_path(arg) for arg in operands if remote_operand(arg))
    return [path for path in paths if path and any(c in _SHELL_SEPARATORS for c in path)]


def identity_scope(path: str) -> str:
    """The read path an scp identity (``-i``) is checked as: the folder that
    holds its ``.ssh`` directory, or the file itself when it is outside one.
    Running scp uses the key without disclosing it, as ssh does, so the key
    is judged for whose folder it sits in (another user's key stays
    refused), never by the rule that refuses every ``.ssh`` file a tool
    reads."""
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part.lower() == ".ssh":
            return "/".join(parts[:i]) or ("/" if path.startswith("/") else ".")
    return path
