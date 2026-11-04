"""A stored user row as a person wrote it, and the default title taken from it.

``clean_user_text`` drops what the platform wraps around a row (the
injected preludes, the Claude TUI's paste tags, the delegation wrappers, a
leading system reminder) and answers None for a row nobody typed (a worker's
report envelope, a command record). It is the twin of the dashboard's
``cleanUserMessageText`` (``dashboard/src/lib/transcriptCleanup.ts``): both
read ``proxy/tests/fixtures/chat_wrappers.json`` case by case. Every default
title (the send path, the two transcript tailers, a task chat's stamp) and
the title generator's input go through it, by way of ``written_text``,
which also cuts the platform's attachments block.

Linear on any input: the send path runs it on the event loop for every
message a person sends, and ``re`` holds the GIL. No pattern here searches
with an unbounded run ahead of its literal, and a block that can repeat is
removed by a ``str.find`` scan (``remove_tag_blocks``), never by a lazy
any-character run searched from every position.
"""

import re

# The injected preludes: the "[Current time: …]" stamp, the viewer focus
# line, a task run's parallel-activity line and a concluded meeting's note.
# Start-anchored exact shapes only; a satellite single-lines a
# launch-argument prompt, so a prelude may end in spaces instead of a line
# break.
PRELUDE_RE = re.compile(
    r"^\[(?:(?:Current time: |The user is looking at the app )[^\]\n]{1,200}"
    r"|Parallel activity — [^\n]{0,2000}?\(peek_session / get_task_result\)\."
    r'|Meeting "[^\n]{0,4000}? you have only this note\.)\]'
    r"(?:[ \t]*(?:\r?\n+|\Z)|[ \t]+)"
)
# The same shape matched at an index (``match(text, pos)``): ``^`` holds only
# at the string's start, so stacked preludes are walked by index and the
# text is sliced once.
_PRELUDE_AT_RE = re.compile(PRELUDE_RE.pattern[1:])

# An app action's framed prompt header (``ws/artifact_interactions.
# frame_text``): an action-started chat is titled "App — Label". The older
# "mini-app" spelling still parses: transcripts written before the rename
# replay through here.
APP_ACTION_HEADER_RE = re.compile(
    r'^\[action from (?:mini-)?app "(.{1,200}?)" — (.{1,80}?)\]'
)

# The image marks the Claude TUI journals ahead of a paste whose photo paths
# it took in as images (``[Image #1] [Image #2]``): a leading run is dropped
# so the paste and the preludes behind it are found. Matched at the start
# only (``match``), never searched.
_IMAGE_MARKS_RE = re.compile(r"(?:\[Image #\d{1,6}\]\s*)+")

# The platform's block naming a message's photos and files, which
# ``ws/dashboard_chat_support._process_attachments`` appends to the text the
# engine reads ("The user has attached 2 image(s). Read and analyze them …").
# A reader keeps it (a live terminal's photo row is that line, and the block
# is the only trace of its photos there); a title and the title model's input
# are cut before it (``written_text``).
_ATTACHMENTS_BLOCK_RE = re.compile(r"The user has attached \d{1,6} (?:image|file)\(s\)")

# The Claude TUI's paste tags: only a LEADING open tag with a trailing close
# tag is the TUI's. The TUI repeats the paste's id on the close tag too. The
# close is searched without the whitespace ahead of it (that run is
# stripped after), its attributes bounded.
_PASTED_OPEN_RE = re.compile(r"^\s*<pasted_content\b[^>]*>\s*")
_PASTED_CLOSE_RE = re.compile(r"</pasted_content\b[^>]{0,200}>\s*\Z")

_SYSTEM_REMINDER_TAGS = ("system-reminder",)

# The delegation MCP's two wrappers around a worker's task, outer first; only
# a LEADING, CLOSED one is the platform's.
_DELEGATED_WORK_RE = re.compile(r"^\s*\[DELEGATED_WORK\][\s\S]*?\[/DELEGATED_WORK\]\s*")
_TASK_OUTPUT_DIR_RE = re.compile(r"^\s*\[TASK_OUTPUT_DIR\][\s\S]*?\[/TASK_OUTPUT_DIR\]\s*")

# The typed copy of a worker's report: the envelope's header line, or the
# line a report-less result opens with.
_RESULT_CALLBACK_PREFIXES = ("[BACKGROUND_TASK_RESULT]", "[The worker ended without a report:")

# Claude Code's record of a local slash command: user rows wrapped in these
# tags.
COMMAND_TAGS = ("command-name", "command-message", "command-args", "local-command-stdout")
COMMAND_TAG_START_RE = re.compile(rf"^<(?:{'|'.join(COMMAND_TAGS)})>")

_TITLE_WORDS = 6
_TITLE_CHARS = 48


def remove_tag_blocks(text: str, tags: tuple[str, ...]) -> str:
    r"""``text`` with every ``<tag>…</tag>`` block of the given tags removed,
    each open ending at its first close: what ``re.sub(r"<(a|b)>[\s\S]*?</\1>",
    "", text)`` answers, in one pass. An open with no close after it ends
    that tag's scan (a later open cannot find a close either)."""
    opens = {t: f"<{t}>" for t in tags}
    closes = {t: f"</{t}>" for t in tags}
    next_open = {t: text.find(opens[t]) for t in tags}
    out: list[str] = []
    kept = pos = 0
    while True:
        live = [(at, t) for t, at in next_open.items() if at >= 0]
        if not live:
            break
        at, tag = min(live)
        end = text.find(closes[tag], at + len(opens[tag]))
        if end < 0:
            next_open[tag] = -1
            continue
        out.append(text[kept:at])
        kept = pos = end + len(closes[tag])
        for t, nxt in next_open.items():
            if 0 <= nxt < pos:
                next_open[t] = text.find(opens[t], pos)
    out.append(text[kept:])
    return "".join(out)


def _strip_preludes(text: str) -> str:
    pos = 0
    while True:  # stacked preludes (the time stamp plus the focus line)
        m = _PRELUDE_AT_RE.match(text, pos)
        if m is None or m.end() == pos:
            return text[pos:] if pos else text
        pos = m.end()


def _strip_pasted_tags(text: str) -> str:
    opened = _PASTED_OPEN_RE.match(text)
    if opened is None or _PASTED_CLOSE_RE.search(text) is None:
        return text
    rest = text[opened.end():]
    closed = _PASTED_CLOSE_RE.search(rest)
    return rest[:closed.start()].rstrip() if closed else rest


def _strip_wrappers(text: str) -> str:
    return _TASK_OUTPUT_DIR_RE.sub("", _DELEGATED_WORK_RE.sub("", text, count=1), count=1)


def clean_user_text(text: str | None) -> str | None:
    """A stored user row's text as a reader shows it, or None when nothing a
    person wrote remains (the row is not drawn, and titles nothing). A row
    that was empty to begin with stays empty."""
    raw = text or ""
    marks = _IMAGE_MARKS_RE.match(raw)
    stripped = _strip_preludes(_strip_pasted_tags(raw[marks.end():] if marks else raw))
    # A leading system-reminder block (prepended to the person's text); a
    # prelude that sat behind it becomes leading.
    if stripped.lstrip().startswith("<system-reminder>"):
        stripped = _strip_preludes(remove_tag_blocks(stripped, _SYSTEM_REMINDER_TAGS).lstrip())
    # The wrappers sit behind the preludes; a prelude behind them (the
    # parallel-activity line of a task's first prompt) becomes leading.
    stripped = _strip_preludes(_strip_wrappers(stripped))
    trimmed = stripped.strip()
    if not trimmed:
        return None if raw.strip() else stripped
    if trimmed.startswith(_RESULT_CALLBACK_PREFIXES):
        return None
    if COMMAND_TAG_START_RE.match(trimmed):
        return remove_tag_blocks(trimmed, COMMAND_TAGS).strip() or None
    if trimmed.startswith("## Context Usage") and "**Tokens:**" in trimmed:
        return None
    if trimmed.startswith("<task-notification>") and "</task-notification>" in trimmed:
        return None
    return stripped


def written_text(text: str | None) -> str:
    """What a person wrote in a stored user row: ``clean_user_text`` with the
    platform's attachments block cut. "" when nothing a person wrote remains
    (a photo alone, a row nobody typed)."""
    cleaned = clean_user_text(text)
    if not cleaned:
        return ""
    block = _ATTACHMENTS_BLOCK_RE.search(cleaned)
    return cleaned[:block.start()] if block else cleaned


def title_from_prompt(text: str | None) -> str:
    """The default title of a chat from its first user row: the words a
    person wrote, at most six of them or 48 characters, an ellipsis when cut;
    an app action's framed prompt reads "App — Label". The platform's
    attachments block is never part of it. "" when the row holds nothing a
    person wrote (a photo alone, too: callers then leave the chat untitled)."""
    cleaned = written_text(text)
    if not cleaned:
        return ""
    m = APP_ACTION_HEADER_RE.match(cleaned)
    if m:
        cleaned = f"{m.group(1)} — {m.group(2)}"
    words = cleaned.split()
    if not words:
        return ""
    title = " ".join(words[:_TITLE_WORDS])
    cut = len(words) > _TITLE_WORDS
    if len(title) > _TITLE_CHARS:
        title = title[:_TITLE_CHARS].rstrip()
        cut = True
    return title + ("…" if cut else "")

