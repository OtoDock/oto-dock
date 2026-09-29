"""What a turn touched, in the words a check's condition uses (CHECKS.md
"Conditions"): the kind of a written file from its extension, the events a
shell command or a tool call stands for. One table, tested; the dashboard's
``lib/fileTypes.ts`` keeps its own for icons.
"""

from __future__ import annotations

import re

KINDS = ("code", "document", "spreadsheet", "presentation", "image", "video",
         "audio", "data", "text", "other")
# The events a condition may name; ``tool:<name>`` and ``commands`` are
# open-ended.
EVENTS = ("commit", "push", "build", "test", "render", "publish")

_EXT_KIND: dict[str, str] = {}
for _ext in ("py pyi ts tsx js jsx mjs cjs go rs java kt kts c h cc cpp hpp cs swift rb php "
             "pl pm lua r scala sh bash zsh fish ps1 psm1 bat cmd sql vue svelte astro dart "
             "ex exs erl hs ml mli clj cljs elm nim zig m mm groovy gradle tf hcl proto "
             "graphql gql ipynb").split():
    _EXT_KIND[_ext] = "code"
for _ext in "md markdown txt rst adoc tex".split():
    _EXT_KIND[_ext] = "text"
for _ext in "pdf doc docx odt rtf pages epub".split():
    _EXT_KIND[_ext] = "document"
for _ext in "xls xlsx ods csv tsv numbers".split():
    _EXT_KIND[_ext] = "spreadsheet"
for _ext in "ppt pptx odp key".split():
    _EXT_KIND[_ext] = "presentation"
for _ext in "jpg jpeg png gif svg webp bmp tif tiff heic avif psd ai".split():
    _EXT_KIND[_ext] = "image"
for _ext in "mp4 m4v mov webm mkv avi mpg mpeg".split():
    _EXT_KIND[_ext] = "video"
for _ext in "mp3 m4a aac wav ogg oga opus flac aiff".split():
    _EXT_KIND[_ext] = "audio"
for _ext in "json jsonl yaml yml toml xml ini cfg conf env parquet sqlite db".split():
    _EXT_KIND[_ext] = "data"
# Build outputs and configuration by NAME, not extension.
_NAME_KIND = {"dockerfile": "code", "makefile": "code", "cmakelists.txt": "code",
              "package.json": "data", "pyproject.toml": "data", "cargo.toml": "data"}


def classify_path(path: str) -> str:
    """The kind of a file by its name; ``other`` when unknown."""
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if not name:
        return "other"
    if name in _NAME_KIND:
        return _NAME_KIND[name]
    if "." not in name:
        return "other"
    return _EXT_KIND.get(name.rsplit(".", 1)[-1], "other")


# Shell commands → events. Each pattern is matched against the whole command
# text (``re.search``), so a ``cd x && git commit`` counts; the word
# boundaries keep ``git commit-tree`` and ``pytest-cov`` from matching.
_COMMAND_EVENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ``git [options] commit`` within one pipeline segment; ``commit-tree``
    # and ``push-x`` are not the words.
    ("commit", re.compile(r"\bgit\b[^|;&\n]*?\scommit(?![\w-])")),
    ("push", re.compile(r"\bgit\b[^|;&\n]*?\spush(?![\w-])")),
    ("build", re.compile(
        r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b|\bmake\b|\bcargo\s+build\b|"
        r"\bgo\s+build\b|\btsc\b|\bvite\s+build\b|\bdocker\s+(?:buildx\s+)?build\b|"
        r"\bgradle\w*\s+(?:build|assemble)\b|\bmvn\s+(?:package|install|compile)\b|"
        r"\bpython3?\s+-m\s+build\b")),
    ("test", re.compile(
        r"\bpytest\b|\bvitest\b|\bjest\b|\bmocha\b|\bcargo\s+test\b|\bgo\s+test\b|"
        r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|\bpython3?\s+-m\s+(?:pytest|unittest)\b|"
        r"\bphpunit\b|\brspec\b|\bmvn\s+test\b|\bgradle\w*\s+test\b")),
    ("render", re.compile(r"\bffmpeg\b|\bblender\b|\bmagick\b|\bconvert\s+\S+\.(?:png|jpg|jpeg|webp)\b")),
    ("publish", re.compile(
        r"\bnpm\s+publish\b|\bgh\s+release\s+create\b|\btwine\s+upload\b|\bcargo\s+publish\b|"
        r"\bgit\b[^|;&\n]*?\spush(?![\w-])[^|;&\n]*--tags\b|\bdocker\s+push\b")),
)

# MCP tools that stand for an event on their own (the tool name is on the
# record on every placement).
_TOOL_EVENTS: dict[str, str] = {
    "mcp__video-tools__render_composition": "render",
    "mcp__video-tools__render_motion_clip": "render",
    "mcp__video-tools__render_still": "render",
    "mcp__video-tools__edit_video": "render",
    "mcp__video-gen-mcp__generate_video": "render",
}


def classify_command(command: str) -> set[str]:
    """The events one shell command stands for (empty for a read)."""
    text = command or ""
    if not text.strip():
        return set()
    return {event for event, pat in _COMMAND_EVENTS if pat.search(text)}


def classify_tool(tool_name: str) -> set[str]:
    """The events a tool call stands for: ``tool:<name>`` for every MCP tool
    (a condition may name one) plus the known renderers."""
    out: set[str] = set()
    name = tool_name or ""
    if name.startswith("mcp__"):
        out.add(f"tool:{name}")
        known = _TOOL_EVENTS.get(name)
        if known:
            out.add(known)
    return out


def glob_pattern(pattern: str) -> str:
    """A tree glob as an RE2 pattern (``patterns``: a person's glob never
    meets a backtracking engine): ``*`` and ``?`` never cross a ``/``,
    ``**`` is any depth (``a/**/b`` also matches ``a/b``)."""
    import re2
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1
                out.append("(?:.*/)?")
            else:
                out.append(".*")
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re2.escape(ch))
        i += 1
    return "^" + "".join(out) + "$"


def matches_glob(relative: str, pattern: str) -> bool:
    """A tree glob (``**`` any depth) against a tree-relative path."""
    rel = (relative or "").replace("\\", "/").strip("/")
    pat = (pattern or "").replace("\\", "/").strip("/")
    if not pat:
        return False
    from services.checks import patterns
    return patterns.search(glob_pattern(pat), rel)
