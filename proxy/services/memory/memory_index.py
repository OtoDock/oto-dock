"""The memory index: what a scope's topic files say about themselves.

``MEMORY.md`` is generated server output, never written by an agent: one
entry per topic file with its heading, a summary line, its size, its entry
count, the newest date in its text and its ``##`` sections with their line
numbers, so a session can read one section with ``view_range`` instead of a
whole topic and can see which topic holds a subject before it writes. The
prompt renders the same entries live for the topics it does not inline
(``configuration.prompts._render_memory_scope``). Pure code over the files;
no LLM is involved anywhere.

The scanning primitives live here (``iter_topic_files``, the index names and
caps) and ``memory_file`` imports them, never the reverse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

INDEX_FILENAME = "MEMORY.md"
ALLOWED_EXTENSIONS = {".md", ".txt"}

INDEX_MAX_LINES = 200                 # Claude Code parity
INDEX_MAX_BYTES = 25 * 1024
INDEX_SUMMARY_MAX_CHARS = 200         # the heading's cut
SUMMARY_LINE_MAX_CHARS = 300          # a longer line under the heading is no summary
SECTION_TITLE_MAX_CHARS = 80
MAX_SECTIONS = 8
STALE_AFTER_DAYS = 90
# The most of a topic the index reads: the 64 KB per-file cap plus a
# margin. A giant dropped into a memory folder by hand is listed by its
# size and never read whole; its sections past the cut go unlisted.
TOPIC_READ_CAP_BYTES = 80 * 1024

# The header carries a version: an index written by an older builder is
# regenerated once (``memory_file.index_is_stale`` reads the first line).
INDEX_HEADER = (
    "# Memory index v2 (generated from the topic files: edit those, never this file)\n"
)

_DATE_RE = re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])")
_BULLET_RE = re.compile(r"^\s*[-*]\s+\S")
_RULE_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$|^\s*={3,}\s*$")
_SECTION_RE = re.compile(r"^##\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# A line that cannot be the summary: a heading, a bullet, a numbered item, a
# quote, a table row, a fence or a rule.
_NOT_SUMMARY_RE = re.compile(r"^\s*(#|[-*]\s|\d+[.)]\s|>|\||```|~~~)")


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def iter_topic_files(root: Path) -> list[Path]:
    """All topic files in a scope (recursive), excluding the index, hidden
    files/dirs, and non-text extensions. Sorted by relpath for stability."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root)
        if any(seg.startswith(".") for seg in rel.parts):
            continue
        if p.name == INDEX_FILENAME and len(rel.parts) == 1:
            continue
        if p.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        out.append(p)
    return out


def scope_total_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in iter_topic_files(root))


@dataclass(frozen=True)
class TopicFacts:
    rel: str
    heading: str
    summary: str
    size: int
    entries: int
    newest: date | None
    stale: bool
    sections: tuple[tuple[int, str], ...]


def _cut(text: str, limit: int) -> str:
    return text[: limit - 1] + "…" if len(text) > limit else text


def newest_date(text: str, *, today: date) -> date | None:
    """The largest real ``YYYY-MM-DD`` in the text that is not later than
    tomorrow: a deadline or an expiry written in a topic never makes it the
    freshest."""
    latest = today + timedelta(days=1)
    newest: date | None = None
    for m in _DATE_RE.finditer(text):
        try:
            found = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if found <= latest and (newest is None or found > newest):
            newest = found
    return newest


def has_date(text: str) -> bool:
    return any(
        _real_date(m) for m in _DATE_RE.finditer(text)
    )


def _real_date(m: re.Match[str]) -> bool:
    try:
        date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return True


def topic_facts(path: Path, root: Path, *, today: date) -> TopicFacts:
    """One topic's index facts. Lines are numbered as ``op_view`` numbers
    them (``str.splitlines``), so a section's ``L12`` is a ``view_range``
    start."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            raw = f.read(TOPIC_READ_CAP_BYTES)
    except OSError:
        raw, size = b"", 0
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    heading = "(empty)"
    heading_idx = -1
    for i, line in enumerate(lines):
        s = line.strip().lstrip("#").strip()
        if s:
            heading = _cut(s, INDEX_SUMMARY_MAX_CHARS)
            heading_idx = i
            break
    summary = ""
    for line in lines[heading_idx + 1:] if heading_idx >= 0 else []:
        s = line.strip()
        if not s:
            continue
        if (
            not _NOT_SUMMARY_RE.match(line) and not _RULE_RE.match(line)
            and len(s) <= SUMMARY_LINE_MAX_CHARS
        ):
            summary = s
        break
    entries = 0
    sections: list[tuple[int, str]] = []
    fence = ""   # the marker that opened the fence, so ~~~ never closes ```
    for i, line in enumerate(lines, start=1):
        m = _FENCE_RE.match(line)
        if m and (not fence or m.group(1) == fence):
            fence = "" if fence else m.group(1)
            continue
        if fence:
            continue
        if _BULLET_RE.match(line) and not _RULE_RE.match(line):
            entries += 1
        m = _SECTION_RE.match(line)
        if m and i - 1 != heading_idx and len(sections) < MAX_SECTIONS:
            sections.append((i, _cut(m.group(1), SECTION_TITLE_MAX_CHARS)))
    newest = newest_date(text, today=today)
    return TopicFacts(
        rel=path.relative_to(root).as_posix(),
        heading=heading,
        summary=summary,
        size=size,
        entries=entries,
        newest=newest,
        stale=newest is not None and (today - newest).days > STALE_AFTER_DAYS,
        sections=tuple(sections),
    )


def scope_facts(root: Path, *, today: date) -> list[TopicFacts]:
    return [topic_facts(p, root, today=today) for p in iter_topic_files(root)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _size_text(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{(n + 512) // 1024} KB"


def entry_lines(f: TopicFacts, *, detail: bool) -> list[str]:
    """One topic's index entry: the heading line with its facts, then (with
    ``detail``) the summary and the sections."""
    count = f"{f.entries} entr{'y' if f.entries == 1 else 'ies'}"
    if f.newest is None:
        when = "undated"
    else:
        when = f"newest {f.newest.isoformat()}" + (", stale" if f.stale else "")
    lines = [f"- {f.rel}: {f.heading} ({_size_text(f.size)}, {count}, {when})"]
    if detail:
        if f.summary:
            lines.append(f"  {f.summary}")
        if f.sections:
            lines.append("  " + " ".join(f"· L{n} {title}" for n, title in f.sections))
    return lines


def _fits(lines: list[str]) -> bool:
    body = "\n".join(lines) + "\n"
    return len(lines) <= INDEX_MAX_LINES and len(body.encode("utf-8")) <= INDEX_MAX_BYTES


def render_capped(facts: list[TopicFacts], *, header: str = "") -> str:
    """The entries within the index caps: with summaries and sections while
    they fit, the heading lines alone when they do not, and as the last
    resort the heading lines cut with a line counting the rest. Same files
    on the same day, same text."""
    head = [header.rstrip("\n")] if header else []
    for detail in (True, False):
        lines = list(head)
        for f in facts:
            lines.extend(entry_lines(f, detail=detail))
        if _fits(lines):
            return "\n".join(lines) + "\n"
    body = "\n".join(head) + ("\n" if head else "")
    for i, f in enumerate(facts):
        candidate = body + entry_lines(f, detail=False)[0] + "\n"
        if (
            len(head) + i + 1 > INDEX_MAX_LINES
            or len(candidate.encode("utf-8")) > INDEX_MAX_BYTES
        ):
            omitted = len(facts) - i
            body += (
                f"… ({omitted} more topics not indexed. The index is over its "
                "budget: consolidate topic files.)\n"
            )
            break
        body = candidate
    return body


def build_index_content(root: Path, *, today: date | None = None) -> str:
    """Deterministic index body for a scope: pure code, no LLM, ever."""
    return render_capped(
        scope_facts(root, today=today or today_utc()), header=INDEX_HEADER,
    )
