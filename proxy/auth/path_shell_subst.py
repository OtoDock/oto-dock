"""Command-substitution scanner for the shell command gate.

A ``$( … )``, ``<( … )``, ``>( … )`` or backtick substitution restarts the
quoting context inside it, so neither the segment splitter nor shlex can see
through one from the outside: a ``|`` inside it belongs to the inner
command, a ``'`` inside ``"…"`` is literal there, and a ``"`` inside a
single-quoted span inside the substitution is literal too. The scanner
finds where each substitution ends and lifts it out into a placeholder
token, so the outer command is analysed with the substitution's text
removed and the inner command is classified on its own.

Placeholders are ordinary word characters: they survive shlex, they never
match a command tier (an outer command that IS a substitution stays
unknown) and they never resolve as a path (the gate skips any path token
carrying one).
"""

PLACEHOLDER_PREFIX = "__OTO_SUBST_"


def has_placeholder(text: str) -> bool:
    return PLACEHOLDER_PREFIX in text


def _placeholder(n: int) -> str:
    return f"{PLACEHOLDER_PREFIX}{n}__"


def backtick_end(s: str, i: int) -> tuple[int, bool]:
    """``s[i]`` is an opening backtick. Return ``(end, closed)``: the index
    just past the closing backtick, or ``len(s)`` when there is none.
    Quotes do not protect a backtick inside the old-style form; only a
    backslash does."""
    j, n = i + 1, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1, True
        j += 1
    return n, False


def subst_end(s: str, i: int) -> tuple[int, bool]:
    """``s[i:i + 2]`` opens a ``$(``, ``<(`` or ``>(`` substitution. Return
    ``(end, closed)``: the index just past the matching ``)``, or ``len(s)``
    when the parentheses never balance (the caller then treats the rest of
    the string as the inner command, which is never a silent allow).

    Tracks the quoting context INSIDE the substitution: parentheses inside
    quotes do not count, a nested ``$(`` or backtick is consumed whole, and
    ``$((…))`` arithmetic rides the same depth count."""
    j, n = i + 2, len(s)
    depth = 1
    in_s = in_d = False
    while j < n:
        c = s[j]
        if c == "\\" and not in_s:
            j += 2
            continue
        if in_s:
            if c == "'":
                in_s = False
            j += 1
            continue
        if c == "'" and not in_d:
            in_s = True
            j += 1
            continue
        if c == '"':
            in_d = not in_d
            j += 1
            continue
        if c == "`":
            j, _ = backtick_end(s, j)
            continue
        if s[j:j + 2] == "$(" or (not in_d and s[j:j + 2] in ("<(", ">(")):
            j, _ = subst_end(s, j)
            continue
        if in_d:
            j += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1, True
        j += 1
    return n, False


def lift_substitutions(s: str, *, quotes_are_literal: bool = False) -> tuple[str, list[str]]:
    """Replace every substitution in ``s`` with a placeholder token.

    Returns ``(lifted, inners)``: the outer text with placeholders, and the
    inner command strings in order. The OUTER quoting context is tracked
    (a substitution inside single quotes is literal, ``$(`` and backticks
    still open inside double quotes, process substitutions do not) unless
    ``quotes_are_literal`` — the heredoc-body rule, where quotes are data
    and only ``$(`` and backticks expand."""
    out: list[str] = []
    inners: list[str] = []
    i, n = 0, len(s)
    in_s = in_d = False
    while i < n:
        c = s[i]
        if c == "\\" and not in_s and i + 1 < n:
            out.append(s[i:i + 2])
            i += 2
            continue
        if not quotes_are_literal:
            if c == "'" and not in_d:
                in_s = not in_s
                out.append(c)
                i += 1
                continue
            if c == '"' and not in_s:
                in_d = not in_d
                out.append(c)
                i += 1
                continue
            if in_s:
                out.append(c)
                i += 1
                continue
        opens_paren = s[i:i + 2] == "$(" or (
            not in_d and not quotes_are_literal and s[i:i + 2] in ("<(", ">(")
        )
        if opens_paren:
            end, closed = subst_end(s, i)
            inners.append(s[i + 2:end - 1] if closed else s[i + 2:end])
            out.append(_placeholder(len(inners) - 1))
            i = end
            continue
        if c == "`":
            end, closed = backtick_end(s, i)
            inners.append(s[i + 1:end - 1] if closed else s[i + 1:end])
            out.append(_placeholder(len(inners) - 1))
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out), inners


def heredoc_line_commands(line: str) -> list[str]:
    """The commands an expanding heredoc body line runs: each ``$(…)`` or
    backtick substitution in it, re-wrapped as ``$(…)`` so the gate
    classifies it like any other substitution (inner checked, tier at
    least ``ask``). Quotes are data inside a heredoc body."""
    _, inners = lift_substitutions(line, quotes_are_literal=True)
    return ["$(" + inner + ")" for inner in inners if inner.strip()]
