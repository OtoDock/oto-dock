#!/usr/bin/env python3
"""The writing rules a program can check, on every file OtoDock writes for people.

WRITING.md says what each rule is for. The words, patterns and caps are in
rules.json beside this file, the same for every repository. A repository's
.prose.json names its files, how each is read and which extras apply. A
folder with its own .prose.json is judged by that file alone.

Usage:
  prose_check.py REPO                          every rule, every config found
  prose_check.py REPO --file PATH ...          these files, no baseline
  prose_check.py REPO --strict --file PATH ... warnings fail too
  prose_check.py --tree DIR                    a folder that is no repository
  prose_check.py REPO --words                  each file's words against its budget
  prose_check.py REPO --write-budgets          the budgets from today's files
  prose_check.py REPO --write-baseline         write a missing baseline, else lower it
  prose_check.py --self-test                   every rule fires on its example
--no-nested makes a nested .prose.json a configuration error (a public run:
no folder of the public tree may exempt itself). Exit 1 on a finding, 2 on a
usage or configuration error, 0 otherwise. A warning is a line that starts
with WARN and never changes the exit. Standard library only, Python 3.10 or
later.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import html
import itertools
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES_FILE = HERE / "rules.json"
CONFIG_NAME = ".prose.json"
BASELINE_NAME = ".prose-baseline.json"
RULES_PAGE = "WRITING.md"
MARKDOWN = (".md", ".mdx", ".markdown")
READ_KINDS = ("markdown", "hash-comments", "json-fields")
LEVELS = ("fail", "warn")
# The base rules a group may skip, with a reason.
BASE_RULES = ("dash", "semicolon", "long-sentence", "symbol", "version", "long-paragraph")
EXTRAS = ("present-tense", "plain-words", "model-names", "screen-parts", "budget", "table-rows",
          "front-matter", "placeholder")
# The rules that read text: an allowed phrase or a block between markers
# lifts one of them.
TEXT_RULES = BASE_RULES + EXTRAS[:4]
# What a baseline may hold: the text rules that fail. A budget, a row cap, a
# front matter flag, a stale exception or claim fails at once, never held.
HELD_RULES = tuple(rule for rule in TEXT_RULES if rule != "long-paragraph")
# A group's keys that belong to an extra.
EXTRA_KEYS = {"budgets": "budget", "total": "budget", "total_excludes": "budget",
              "max_table_rows": "table-rows", "front_matter": "front-matter", "placeholder": "placeholder"}
CONFIG_KEYS = {"_comment", "groups", "between", "excluded", "allowed"}
BETWEEN_KEYS = {"file", "from", "to", "skip"}
ALLOWED_KEYS = {"phrase", "rule", "reason"}
DETAIL = 110


class ConfigError(Exception):
    """A config, the rule set or the command line cannot be read as written: exit 2."""


# --- the rule set -----------------------------------------------------------------


def _words_re(words: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + r")s?\b", re.IGNORECASE)


def _strict_json(path: Path):
    """A JSON file, refused when a key appears twice in one object."""
    def pairs(items: list[tuple]) -> dict:
        keys = [key for key, _ in items]
        twice = sorted({key for key in keys if keys.count(key) > 1})
        if twice:
            raise ConfigError(f"{path}: the key(s) {twice} appear twice in one object")
        return dict(items)
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _ending(words: list[str]) -> re.Pattern:
    """Any of the words, then white space, at the end of the text read so far."""
    return re.compile(r"(?:\b(?:" + "|".join(words) + r")\s+)$", re.IGNORECASE)


class Rules:
    """The rule set of rules.json, its patterns compiled."""

    def __init__(self, data: dict) -> None:
        words = data["words"]
        version = data["version"]
        names = sorted({re.escape(n) for n in data["models"]["names"]} | set(data["models"]["patterns"]),
                       key=len, reverse=True)
        self.data = data
        self.cap = int(data["sentence"]["cap"])
        self.warn = int(data["sentence"]["warn"])
        self.dash = re.compile(data["dash"])
        self.pictographs = re.compile(data["symbols"]["pictographs"])
        self.arrows = re.compile(data["symbols"]["arrows"])
        self.plain = _words_re(words["plain"])
        self.parts = _words_re(data["screen_parts"])
        self.models = re.compile(r"(?<![\w-])(?:" + "|".join(names) + r")(?![\w-])")
        self.phrases = list(data["present_tense"]["phrases"])
        self.used_to_after = frozenset(w.lower() for w in data["present_tense"]["used_to_after"])
        self.other_software = frozenset(version["other_software"])
        self.products = frozenset(version["products"])
        self.own_lower = frozenset(n.lower() for n in version["products"] + version["own_names"])
        self.address_words = frozenset(w.lower() for w in version["address_words"])
        self.before = _ending(version["before"] + [re.escape(p) + "(?:['\u2019]s)?" for p in version["products"]])
        self.after = re.compile(r"\s+(?:" + "|".join(version["after"]) + r")\b", re.IGNORECASE)
        self.not_after = _ending(version["not_after"])
        self.licences = re.compile(r"(?:\b(?:" + "|".join(version["licences"]) + r")\s+)$")
        self.units = re.compile(r"\s?(?:" + "|".join(version["units"]) + r")(?![A-Za-z])")
        self.release_floor = re.compile(version["release_floor"])
        self.commands = frozenset(data["samples"]["commands"])


def load_rules(path: Path = RULES_FILE) -> Rules:
    try:
        return Rules(_strict_json(path))
    except (ValueError, KeyError, TypeError, re.error) as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# --- reading a file ------------------------------------------------------------------


@dataclass
class Text:
    """Reading text, each character with its offset in the file."""
    s: str
    pos: list[int]

    @classmethod
    def at(cls, s: str, offset: int) -> Text:
        return cls(s, list(range(offset, offset + len(s))))

    def join(self, other: Text) -> Text:
        glue = self.pos[-1] + 1 if self.pos else (other.pos[0] if other.pos else 0)
        return Text(self.s + " " + other.s, self.pos + [glue] + other.pos)

    def strip(self) -> Text:
        start = len(self.s) - len(self.s.lstrip())
        end = len(self.s.rstrip())
        return Text(self.s[start:end], self.pos[start:end])

    def span(self, start: int, end: int) -> tuple[int, int]:
        if not self.pos:
            return 0, 0
        end = max(start + 1, end)
        return self.pos[min(start, len(self.pos) - 1)], self.pos[min(end, len(self.pos)) - 1]


def _sub(text: Text, pattern: re.Pattern, repl) -> Text:
    """re.sub that keeps the offsets: repl gives a group to keep (int) or a
    string, whose characters take the match's first offset."""
    parts: list[str] = []
    pos: list[int] = []
    last = 0
    for match in pattern.finditer(text.s):
        if match.start() == match.end():
            continue
        parts.append(text.s[last:match.start()])
        pos.extend(text.pos[last:match.start()])
        kept = repl(match)
        if isinstance(kept, int):
            a, b = match.span(kept)
            parts.append(text.s[a:b])
            pos.extend(text.pos[a:b])
        else:
            parts.append(kept)
            pos.extend([text.pos[match.start()]] * len(kept))
        last = match.end()
    parts.append(text.s[last:])
    pos.extend(text.pos[last:])
    return Text("".join(parts), pos)


@dataclass
class Block:
    kind: str          # para, item, heading, cell
    text: Text


@dataclass
class Code:
    text: Text
    kind: str = "block"   # block (a fence or an indented line, a sample line) or span


@dataclass
class Page:
    source: str
    front: str = ""
    blocks: list[Block] = field(default_factory=list)
    code: list[Code] = field(default_factory=list)
    table_rows: int = 0
    matched: set[str] = field(default_factory=set)    # the json field paths that found a text
    _starts: list[int] = field(default_factory=list)

    def words(self) -> int:
        return sum(len(block.text.s.split()) for block in self.blocks)

    def line(self, offset: int) -> int:
        if not self._starts:
            self._starts = [0] + [i + 1 for i, ch in enumerate(self.source) if ch == "\n"]
        return bisect.bisect_right(self._starts, offset)


_CODE_OR_COMMENT = re.compile(r"(`+)(.+?)\1|<!--.*?-->", re.S)
_INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.S)
_TAG = re.compile(r"<(?:[A-Za-z][\w:.-]*(?:\s[^<>]*)?/?|/[A-Za-z][\w:.-]*\s*|!(?!--)[^<>]*)>")
_AUTOLINK = re.compile(r"<(https?://[^<>\s]+)>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REFLINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_URL = re.compile(r"https?://\S+")
_HEADING_ID = re.compile(r"\{#[^}]*\}")
_ENTITY = re.compile(r"&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z]+);")
_BACKSLASH = re.compile(r"\\")
_STRONG = re.compile(r"\*\*|__")
_EMPHASIS = re.compile(r"(?<![\w*])\*(?=\S)|(?<=\S)\*(?![\w*])")


def inline(text: Text, page: Page) -> Text:
    """Reading text of a block: code set apart and comments gone, whichever
    opens first, tags gone before the addresses, a link's text kept and its
    target gone."""
    def code_or_comment(match: re.Match) -> str:
        if match.group(1) is None:
            return " "
        a, b = match.span(2)
        page.code.append(Code(Text(text.s[a:b], text.pos[a:b]), "span"))
        return "CODE"

    text = _sub(text, _CODE_OR_COMMENT, code_or_comment)
    text = _sub(text, _AUTOLINK, lambda m: "URL")
    text = _sub(text, _TAG, lambda m: " ")
    text = _sub(text, _IMAGE, lambda m: 1)
    text = _sub(text, _LINK, lambda m: 1)
    text = _sub(text, _REFLINK, lambda m: 1)
    text = _sub(text, _URL, lambda m: "URL")
    text = _sub(text, _HEADING_ID, lambda m: " ")
    text = _sub(text, _ENTITY, lambda m: html.unescape(m.group(0)))
    text = _sub(text, _BACKSLASH, lambda m: "")
    text = _sub(text, _STRONG, lambda m: "")
    text = _sub(text, _EMPHASIS, lambda m: "")
    return text.strip()


def _add(page: Page, kind: str, parts: list[Text]) -> None:
    """One block of reading text, its parts joined before the inline rules read it."""
    joined = parts[0]
    for part in parts[1:]:
        joined = joined.join(part)
    text = inline(joined, page)
    if text.s:
        page.blocks.append(Block(kind, text))


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_QUOTE = re.compile(r"^(?:[ ]{0,3}>[ ]?)+")
_ES_LINE = re.compile(r"^(?:import\s+(?:[\w*{][^'\"]*\sfrom\s+)?['\"][^'\"]+['\"];?\s*$"
                      r"|export\s+(?:default|const|function|let|var|class|async)\b)")
_MDX_LINE = re.compile(r"^(?:import|export)\s")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_DIRECTIVE = re.compile(r"^\s*:::\s*\w*\s*")
_BREAK = re.compile(r"^\s*(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,}|={3,})\s*$")
_LINK_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:\s*\S")
_HTML_BLOCK = re.compile(r"^\s*</?[A-Za-z][\w-]*(?:\s|>|/>|$)")
_CELL = re.compile(r"(?<!\\)\|")


def read_markdown(source: str, mdx: bool = False) -> Page:
    """A page as blocks: one per paragraph, list item, table cell or heading,
    a paragraph's wrapped lines joined before the inline rules read it. A quote
    is read as its content, fences and indented blocks are code, text inside an
    HTML comment is not judged."""
    page = Page(source)
    lines = source.split("\n")
    offsets = [0, *itertools.accumulate(len(raw) + 1 for raw in lines)]
    start = 0
    if lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                page.front = "\n".join(lines[1:index])
                start = index + 1
                break
    para: list[Text] = []
    para_kind = "para"
    state = {"fence": "", "fence_quoted": False, "comment": False, "blank": True, "list": False, "html": False,
             "code": False}

    def flush() -> None:
        nonlocal para, para_kind
        if para:
            _add(page, para_kind, para)
        para, para_kind = [], "para"

    for index in range(start, len(lines)):
        raw, offset = lines[index], offsets[index]
        if state["comment"]:
            end = raw.find("-->")
            if end < 0:
                continue
            state["comment"] = False
            offset += end + 3
            raw = raw[end + 3:]
            if not raw.strip():
                continue
        if not state["fence"]:
            # A comment opens only outside a code span.
            probe = _INLINE_CODE.sub(lambda m: "x" * len(m.group(0)), raw)
            opening = probe.find("<!--")
            if opening >= 0 and probe.find("-->", opening) < 0:
                state["comment"] = True
                raw = raw[:opening]
                if not raw.strip():
                    flush()
                    continue
        quote = _QUOTE.match(raw)
        if quote and (not state["fence"] or state["fence_quoted"]):
            offset += quote.end()
            raw = raw[quote.end():]
        if state["fence"]:
            close = _FENCE_CLOSE.match(raw)
            if close and close.group(1)[0] == state["fence"][0] and len(close.group(1)) >= len(state["fence"]):
                state["fence"] = ""
            else:
                page.code.append(Code(Text.at(raw, offset)))
            continue
        fence = _FENCE.match(raw)
        if fence:
            flush()
            state["fence"] = fence.group(1)
            state["fence_quoted"] = bool(quote)
            continue
        if not raw.strip():
            flush()
            state.update(blank=True, html=False)
            continue
        after_blank = state["blank"]
        state["blank"] = False
        indent = len(raw) - len(raw.lstrip(" \t"))
        expanded = len(raw[:indent].expandtabs(4))
        if expanded >= 4 and (after_blank or state["code"]) and not state["list"] and not state["html"] \
                and not para:
            page.code.append(Code(Text.at(raw, offset)))
            state["code"] = True
            continue
        state["code"] = False
        if _ES_LINE.match(raw.strip()) or (mdx and _MDX_LINE.match(raw)):
            flush()
            continue
        if _HTML_BLOCK.match(raw) and after_blank:
            state["html"] = True
        directive = _DIRECTIVE.match(raw)
        if directive:
            flush()
            _add(page, "heading", [Text.at(raw[directive.end():], offset + directive.end())])
            continue
        if raw.lstrip().startswith("|"):
            flush()
            state["list"] = False
            if _TABLE_RULE.match(raw):
                page.table_rows -= 1  # the header row above the rule is not a row of data
                continue
            page.table_rows += 1
            cells = Text.at(raw, offset).strip()
            body, base = cells.s, cells.pos[0]
            edges = [-1] + [m.start() for m in _CELL.finditer(body)] + [len(body)]
            for a, b in zip(edges, edges[1:], strict=False):
                cell = Text.at(body[a + 1:b], base + a + 1)
                if cell.s.strip():
                    _add(page, "cell", [cell.strip()])
            continue
        head = _HEADING.match(raw)
        if head:
            flush()
            state["list"] = False
            _add(page, "heading", [Text.at(raw[head.end():], offset + head.end())])
            continue
        if _BREAK.match(raw) or _LINK_DEFINITION.match(raw):
            flush()
            continue
        item = _ITEM.match(raw)
        if item:
            flush()
            state["list"] = True
            para_kind = "item"
            para.append(Text.at(raw[item.end():], offset + item.end()))
            continue
        if indent == 0 and after_blank:
            state["list"] = False
        line = Text.at(raw, offset).strip()
        if line.s:
            para.append(line)
    flush()
    return page


_RULE_LINE = re.compile(r"^[\s─-╿=#*~_-]{3,}$")
_TITLED_RULE = re.compile(r"^[─-╿=-]{2,}\s+(.+?)\s*[─-╿=-]*\s*$")
_KEY_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


def _env_sample(body: str, rules: Rules) -> bool:
    """Whether an indented comment line is a sample, not prose: a key line, a
    shell line, a nested comment or data."""
    line = body.strip()
    first = line.split(" ", 1)[0]
    return bool(_KEY_LINE.match(line)) or line.startswith(("$", "#", "{", "[", "./", "/")) or first in rules.commands


def read_hash_comments(source: str, rules: Rules | None = None) -> Page:
    """The comments of an env file: a commented key line or an indented
    sample is code, a rule line with a name is a heading, a blank or a key
    line ends a paragraph, an indented line of prose continues it."""
    rules = rules or load_rules()
    page = Page(source)
    para: list[Text] = []

    def flush() -> None:
        nonlocal para
        if para:
            _add(page, "para", para)
        para = []

    offset = 0
    for raw in source.split("\n"):
        here = offset
        offset += len(raw) + 1
        lead = len(raw) - len(raw.lstrip())
        body = raw[lead:]
        if not body.startswith("#"):
            flush()
            if _KEY_LINE.match(body):
                page.code.append(Code(Text.at(body, here + lead)))
            continue
        start = here + lead + 1
        body = body[1:]
        if body.startswith(" "):
            body, start = body[1:], start + 1
        if not body.strip() or _RULE_LINE.match(body):
            flush()
            continue
        titled = _TITLED_RULE.match(body)
        if titled and titled.group(1):
            flush()
            _add(page, "heading", [Text.at(titled.group(1), start + titled.start(1))])
            continue
        if _KEY_LINE.match(body) or (body.startswith((" ", "\t")) and _env_sample(body, rules)):
            flush()
            page.code.append(Code(Text.at(body, start)))
            continue
        para.append(Text.at(body, start).strip())
    flush()
    return page


_JSON_WS = re.compile(r"[ \t\n\r]*")


def json_strings(source: str) -> list[tuple[str, str, int]]:
    """(path, value, offset of the opening quote) of every string in a JSON
    text, the path written a.b[].c."""
    found: list[tuple[str, str, int]] = []
    decoder = json.JSONDecoder()

    def skip(at: int) -> int:
        return _JSON_WS.match(source, at).end()

    def value(at: int, path: str) -> int:
        at = skip(at)
        ch = source[at:at + 1]
        if ch in ("{", "["):
            closer = "}" if ch == "{" else "]"
            at = skip(at + 1)
            if source[at:at + 1] == closer:
                return at + 1
            while True:
                if ch == "{":
                    key, at = json.decoder.scanstring(source, skip(at) + 1)
                    at = skip(at)
                    if source[at:at + 1] != ":":
                        raise ValueError(f"expected ':' at {at}")
                    at = skip(value(at + 1, f"{path}.{key}" if path else key))
                else:
                    at = skip(value(at, path + "[]"))
                if source[at:at + 1] == ",":
                    at += 1
                    continue
                if source[at:at + 1] == closer:
                    return at + 1
                raise ValueError(f"expected ',' or {closer!r} at {at}")
        if ch == '"':
            text, end = json.decoder.scanstring(source, at + 1)
            found.append((path, text, at))
            return end
        return decoder.raw_decode(source, at)[1]

    value(0, "")
    return found


def _path_re(pattern: str) -> re.Pattern:
    return re.compile("^" + re.escape(pattern).replace(r"\*", r"[^.\[]+") + "$")


def read_json_fields(source: str, fields: list[str]) -> Page:
    """Named text fields of a JSON file, each field one block."""
    page = Page(source)
    wanted = [(name, _path_re(name)) for name in fields]
    for path, value, offset in json_strings(source):
        names = [name for name, pattern in wanted if pattern.match(path)]
        if names and value.strip():
            page.matched.update(names)
            _add(page, "para", [Text.at(value, offset + 1)])
    return page


# --- the rules on one page --------------------------------------------------------


@dataclass
class Finding:
    file: str
    line: int
    rule: str
    detail: str
    span: tuple[int, int] = (0, 0)
    block: int = 0     # the first line of the block the finding sits in
    warn: bool = False

    def __str__(self) -> str:
        return f"{'WARN  ' if self.warn else ''}{self.file}:{self.line}  {self.rule}  {self.detail}"


_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]*]*\s+(?=[A-Za-z0-9\"'“‘(*\[`])")
_abbreviations: list[re.Pattern] = []


def split_sentences(text: str) -> list[tuple[int, int]]:
    """The (start, end) of each sentence: after a full stop, a question or an
    exclamation mark (and any closing quote or bracket) and white space, before
    a letter, a digit, a quote or a bracket. Never after an abbreviation of
    the rule set's list."""
    if not _abbreviations:
        names = _strict_json(RULES_FILE)["sentence"]["abbreviations"]
        # As written: "Ms." is an abbreviation, "200 ms." ends a sentence.
        _abbreviations.append(re.compile(r"(?:^|(?<=[\s(]))(?:" + "|".join(re.escape(n) for n in names) + r")$"))
    abbreviation = _abbreviations[0]
    spans = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        if abbreviation.search(text[start:match.start() + 1]):
            continue
        spans.append((start, match.end()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


_NUMBER = re.compile(r"(?<![\w.$/@:=#-])(v?)(\d+)\.(\d+)((?:\.(?:\d+|x))*)(?![\w]|\.\d)")
_CODE_NUMBER = re.compile(r"(?<![\d.])v?(\d+)\.\d+\.(\d+)(?![\d.])")
_PIN_NAME = re.compile(r"([\w./@-]*)(?:==|~=|>=|<=|[<>@:^~])\s*v?$")
_ENV_PIN = re.compile(r"\b([A-Za-z][A-Za-z0-9]*)_VERSION\s*=\s*['\"]?v?$")
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")
_TO_NUMBER = re.compile(r"\s+to\s+v?\d+(?:\.\d+)*")


def _other_software(rules: Rules, before: str) -> bool:
    """Whether a number follows another program's name within four words,
    with no product of ours, no other number, no joining word (since, from,
    before, until, to, and, or) and no shell operator or pin in between."""
    words = [w.strip(".,:;()\"'`*") for w in before.split()][-5:]
    for index in range(len(words) - 1, -1, -1):
        word = words[index]
        if re.sub(r"['’]s$", "", word) in rules.products or re.match(r"v?\d", word):
            return False
        if word.lower() in ("since", "from", "before", "until", "to", "and", "or", "&&", "||", "|", ";"):
            return False
        if "=" in word or "@" in word:
            return False
        if word in rules.other_software:
            return True
    return False


@dataclass
class Group:
    name: str
    use: str
    extras: frozenset[str]
    files: list[str]
    read: str = "markdown"
    fields: list[str] = field(default_factory=list)
    level: str = "fail"
    skip: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    total: int | None = None
    total_excludes: list[str] = field(default_factory=list)
    max_table_rows: dict = field(default_factory=dict)
    front_matter: dict = field(default_factory=dict)
    placeholder: str = ""
    phrases: list[str] = field(default_factory=list)
    release_floor: bool = False


def check_page(rel: str, page: Page, group: Group, rules: Rules, allowed: list[dict] | None = None,
               between: list[tuple[int, int, object]] | None = None) -> list[Finding]:
    """Every rule that reads one page's text: the base rules, the group's
    extras, its skips and the file's allowed phrases."""
    found: list[Finding] = []
    extras = group.extras
    phrases = re.compile(r"\b(?:" + "|".join(rules.phrases + group.phrases) + r")\b", re.IGNORECASE)
    checks = [(pattern, rule) for pattern, rule in ((phrases, "present-tense"), (rules.plain, "plain-words"),
                                                    (rules.parts, "screen-parts"), (rules.models, "model-names"))
              if rule in extras]

    def add(text: Text, start: int, end: int, rule: str, detail: str, first: int, warn: bool = False) -> None:
        span = text.span(start, end)
        found.append(Finding(rel, page.line(span[0]), rule, detail[:DETAIL], span, first, warn))

    def near(s: str, start: int, end: int) -> str:
        """The fault with some text either side."""
        a, b = max(0, start - 50), min(len(s), end + 50)
        return ("..." if a else "") + s[a:b] + ("..." if b < len(s) else "")

    for block in page.blocks:
        text = block.text
        s = text.s
        first = page.line(text.pos[0]) if text.pos else 0
        sentences = split_sentences(s)
        for a, b in sentences:
            count = len(s[a:b].split())
            if count > rules.warn:
                add(text, a, b, "long-sentence", f"{count} words: {s[a:b].strip()}", first, warn=count <= rules.cap)
        words = len(s.split())
        limits = rules.data["paragraph"]
        if block.kind != "heading" and (words > limits["words"] or len(sentences) > limits["sentences"]):
            add(text, 0, len(s), "long-paragraph", f"{words} words, {len(sentences)} sentences: {s}", first, warn=True)
        for match in rules.dash.finditer(s):
            add(text, match.start(), match.end(), "dash", near(s, match.start(), match.end()), first)
        for index, ch in enumerate(s):
            if ch == ";" and not _GREEK.match(s[:index].rstrip()[-1:] or " "):
                add(text, index, index + 1, "semicolon", near(s, index, index + 1), first)
        for pattern in (rules.pictographs, rules.arrows):
            for match in pattern.finditer(s):
                add(text, match.start(), match.end(), "symbol",
                    f"{match.group(0)!r} in: {near(s, match.start(), match.end())}", first)
        for match in _NUMBER.finditer(s):
            here = next((a for a, b in sentences if a <= match.start() < b), 0)
            if _prose_version(rules, group, s, match, here):
                add(text, match.start(), match.end(), "version",
                    f"{match.group(0)!r} in: {near(s, match.start(), match.end())}", first)
        for pattern, rule in checks:
            for match in pattern.finditer(s):
                if rule == "present-tense" and match.group(0).lower() == "used to":
                    prior = s[:match.start()].split()
                    if prior and prior[-1].strip(",.").lower() in rules.used_to_after:
                        continue
                add(text, match.start(), match.end(), rule,
                    f"{match.group(0)!r} in: {near(s, match.start(), match.end())}", first)
    for code in page.code:
        text = code.text
        s = text.s
        first = page.line(text.pos[0]) if text.pos else 0
        if "placeholder" in extras and group.placeholder and group.placeholder in s:
            at = s.index(group.placeholder)
            add(text, at, at + len(group.placeholder), "version", f"the release placeholder in a sample: {s.strip()}",
                first)
        for match in _CODE_NUMBER.finditer(s):
            if _sample_passes(rules, s[:match.start()], match):
                continue
            add(text, match.start(), match.end(), "version", f"{match.group(0)!r} in a sample: {s.strip()}", first)
        if "model-names" in extras:
            for match in rules.models.finditer(s):
                add(text, match.start(), match.end(), "model-names", f"{match.group(0)!r} in a sample: {s.strip()}",
                    first)
    found = [f for f in found if f.rule not in group.skip]
    if between:
        found = [f for f in found if not any(a <= f.line <= b and (skip == "all" or f.rule in skip)
                                             for a, b, skip in between)]
    if allowed:
        found = _lift_allowed(page, found, allowed)
        folded_source = _fold(page.source)[0]
        for entry in allowed:
            if _fold(entry["phrase"])[0] not in folded_source:
                found.append(Finding(rel, 0, "stale-exception",
                                     f"allowed for {entry['rule']}, and the file no longer has: {entry['phrase']}"))
    return found


def _sample_passes(rules: Rules, before: str, match: re.Match) -> bool:
    """A three-part number in a sample that is no version of ours: a date, an
    address part, another program's pin (its name alone decides) or number."""
    if int(match.group(1)) > 99 or len(match.group(2)) == 4:
        return True
    pin = _PIN_NAME.search(before)
    if pin:
        return "otodock" not in pin.group(1).lower()
    env = _ENV_PIN.search(before)
    if env:
        return env.group(1).lower() in {name.lower() for name in rules.other_software}
    return _other_software(rules, before)


def _prose_version(rules: Rules, group: Group, s: str, match: re.Match, sentence: int = 0) -> bool:
    """Whether a number in prose is a version: a three-part number, or a
    two-part one after v, version, release, a product of ours or since, from,
    before, until, older than, newer than, or before "and later" and its kin.
    Never a licence, an address or a part of one, a date, a section, a measure
    or another program's number."""
    before = s[:match.start()]
    parts = 2 + match.group(4).count(".")
    first = int(match.group(2))
    tail = match.group(4).rsplit(".", 1)[-1] if parts >= 3 else ""
    if parts >= 4 or first > 99 or re.match(r"/\d", s[match.end():match.end() + 2]):
        return False
    if (tail == "x" and first >= 10) or (len(tail) == 4 and tail.isdigit()):
        return False
    if any(word.strip(".,:;()").lower() in rules.address_words for word in before.split()[-3:]):
        return False
    if rules.not_after.search(before) or rules.licences.search(before):
        return False
    if rules.units.match(s, match.end()):
        return False
    to = _TO_NUMBER.match(s, match.end())
    if to and rules.units.match(s, to.end()):
        return False
    if group.release_floor and rules.release_floor.search(s[max(0, match.start() - 8):match.end() + 9]):
        return False
    # One of our names right before the number makes it ours, whatever
    # program the sentence named earlier ("the Windows satellite 0.5.160").
    named = re.sub(r"['’]s$", "", before.split()[-1].strip("(\"'*,")) if before.split() else ""
    if named.lower() in rules.own_lower:
        return True
    if _other_software(rules, s[sentence:match.start()]):
        return False
    if parts >= 3 or match.group(1) or rules.before.search(before):
        return True
    if not rules.after.match(s, match.end()):
        return False
    # "Blender 5.1 or newer" is another program's floor; "(1.7 and later)" and
    # "Satellite 0.5 or later" are ours.
    name = before.rstrip().split(" ")[-1].strip("(\"'*") if before.strip() else ""
    return not (name[:1].isupper() and name.lower() not in rules.own_lower)


def _fold(text: str) -> tuple[str, list[int]]:
    """White space folded to one space, with each kept character's offset."""
    out: list[str] = []
    pos: list[int] = []
    gap = False
    for index, ch in enumerate(text):
        if ch.isspace():
            if not gap and out:
                out.append(" ")
                pos.append(index)
            gap = True
            continue
        gap = False
        out.append(ch)
        pos.append(index)
    return "".join(out).rstrip(), pos


def _lift_allowed(page: Page, found: list[Finding], allowed: list[dict]) -> list[Finding]:
    """An allowed phrase lifts its rule's findings whose source span lies
    inside one of its occurrences, white space folded."""
    folded, pos = _fold(page.source)
    spans: dict[str, list[tuple[int, int]]] = {}
    for entry in allowed:
        phrase = _fold(entry["phrase"])[0]
        start = folded.find(phrase)
        while phrase and start >= 0:
            spans.setdefault(entry["rule"], []).append((pos[start], pos[start + len(phrase) - 1]))
            start = folded.find(phrase, start + 1)
    source = page.source

    def end(finding: Finding) -> int:
        """A sentence's span may run over the white space after its full stop."""
        last = finding.span[1]
        while last > finding.span[0] and source[min(last, len(source) - 1)].isspace():
            last -= 1
        return last

    return [f for f in found
            if not any(a <= f.span[0] and end(f) <= b for a, b in spans.get(f.rule, ()))]


def read_page(source: str, rel: str, group: Group, rules: Rules | None = None) -> Page:
    if group.read == "hash-comments":
        return read_hash_comments(source, rules)
    if group.read == "json-fields":
        return read_json_fields(source, group.fields)
    return read_markdown(source, mdx=rel.lower().endswith(".mdx"))


# --- configs, claims and baselines ----------------------------------------------


_GLOB = {"**/": r"(?:.*/)?", "**": r".*", "*": r"[^/]*", "?": r"[^/]"}


def glob_re(pattern: str) -> re.Pattern:
    """* inside one path segment, ** any number of segments (none too)."""
    parts = re.split(r"(\*\*/|\*\*|\*|\?)", pattern)
    return re.compile("^" + "".join(_GLOB.get(part, re.escape(part)) for part in parts) + "$")


def _literal(pattern: str) -> bool:
    return not any(ch in pattern for ch in "*?")


def _is_markdown(path: str) -> bool:
    return path.lower().endswith(MARKDOWN)


@dataclass
class Config:
    folder: str                       # relative to the root, "" for the root
    path: Path
    data: dict
    groups: list[Group]
    excluded: dict
    allowed: dict
    between: list[dict]
    baseline: dict

    def rel(self, path: str) -> str:
        return path[len(self.folder) + 1:] if self.folder else path

    def full(self, rel: str) -> str:
        return f"{self.folder}/{rel}" if self.folder else rel


def _need(cond: bool, where: Path, message: str) -> None:
    if not cond:
        raise ConfigError(f"{where}: {message}")


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _texts(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _reasons(value, names: tuple) -> bool:
    return isinstance(value, dict) and set(value) <= set(names) and all(_text(v) for v in value.values())


def _counts(value) -> bool:
    return isinstance(value, dict) and all(_count(v) for v in value.values())


def _front(value) -> bool:
    return value == {} or (isinstance(value, dict) and set(value) == {"flag", "files"} and _text(value["flag"])
                           and _texts(value["files"]))


# Each optional key of a group: its default, the test of its value, and the
# shape the error names.
GROUP_TYPES = {
    "extras": ([], lambda v: _texts(v) and set(v) <= set(EXTRAS), f"a list of {EXTRAS}"),
    "read": ("markdown", lambda v: isinstance(v, str) and v in READ_KINDS, f"one of {READ_KINDS}"),
    "fields": ([], _texts, "a list of paths"),
    "level": ("fail", lambda v: isinstance(v, str) and v in LEVELS, "fail or warn"),
    "reason": (None, lambda v: v is None or _text(v), "a sentence"),
    "skip": ({}, lambda v: _reasons(v, BASE_RULES), "{base rule: reason}"),
    "budgets": ({}, _counts, "{file: words}"),
    "total": (None, lambda v: v is None or _count(v), "a number of words"),
    "total_excludes": ([], _texts, "a list of path prefixes"),
    "max_table_rows": ({}, _counts, "{file: rows}"),
    "front_matter": ({}, _front, "{flag, files}"),
    "placeholder": ("", lambda v: isinstance(v, str), "a string"),
    "phrases": ([], _texts, "a list of patterns"),
    "release_floor": (False, lambda v: isinstance(v, bool), "true or false"),
}


GROUP_KEYS = {"name", "use", "files"} | set(GROUP_TYPES)


def _group(raw, path: Path, rules: Rules, names: set) -> Group:
    """One group of a config, each key's type checked."""
    _need(isinstance(raw, dict), path, "a group is an object")
    name = raw.get("name")
    _need(_text(name) and name not in names, path, f"a group needs a unique name: {name!r}")
    names.add(name)
    where = f"group {name!r}"
    _need(not set(raw) - GROUP_KEYS, path, f"{where}: unknown key(s) {sorted(set(raw) - GROUP_KEYS)}")
    use = raw.get("use", "developer")
    _need(isinstance(use, str) and use in rules.data["sets"], path, f"{where}: use: one of {sorted(rules.data['sets'])}")
    files = raw.get("files")
    _need(_texts(files) and files, path, f"{where}: files: a list of paths and patterns")
    values = {}
    for key, (default, valid, shape) in GROUP_TYPES.items():
        values[key] = raw.get(key, default)
        _need(valid(values[key]), path, f"{where}: {key}: {shape}")
    extras = frozenset(rules.data["sets"][use]) | set(values.pop("extras"))
    for key, extra in EXTRA_KEYS.items():
        _need(key not in raw or extra in extras, path, f"{where}: {key} needs the extra {extra!r}")
    _need(bool(values["fields"]) == (values["read"] == "json-fields"), path,
          f"{where}: fields: for json-fields alone, and needed there")
    _need(values["level"] == "fail" or values["reason"], path, f"{where}: a warn level needs a reason")
    values.pop("reason")
    for phrase in values["phrases"]:
        try:
            re.compile(phrase)
        except re.error as exc:
            raise ConfigError(f"{path}: {where}: phrase {phrase!r}: {exc}") from exc
    return Group(name=name, use=use, extras=extras, files=files, **values)


def load_config(path: Path, folder: str, rules: Rules) -> Config:
    data = _strict_json(path)
    _need(isinstance(data, dict), path, "not an object")
    _need(not set(data) - CONFIG_KEYS, path, f"unknown key(s) {sorted(set(data) - CONFIG_KEYS)}")
    _need(_text(data.get("_comment", "x")), path, "_comment: a sentence")
    raw_groups = data.get("groups", [])
    _need(isinstance(raw_groups, list), path, "groups: a list")
    names: set = set()
    groups = [_group(raw, path, rules, names) for raw in raw_groups]
    excluded = data.get("excluded", {})
    _need(isinstance(excluded, dict) and all(_text(v) for v in excluded.values()), path,
          "excluded: {path or pattern: reason}")
    allowed = data.get("allowed", {})
    _need(isinstance(allowed, dict), path, "allowed: {file: [entries]}")
    for rel, entries in allowed.items():
        _need(isinstance(entries, list), path, f"allowed {rel}: a list of entries")
        for entry in entries:
            _need(isinstance(entry, dict) and set(entry) == ALLOWED_KEYS and all(_text(entry[k]) for k in ALLOWED_KEYS),
                  path, f"allowed {rel}: an entry is {{phrase, rule, reason}}")
            _need(entry["rule"] in TEXT_RULES, path, f"allowed {rel}: rule {entry['rule']!r} is none of {TEXT_RULES}")
    between = data.get("between", [])
    _need(isinstance(between, list), path, "between: a list")
    for entry in between:
        _need(isinstance(entry, dict) and set(entry) == BETWEEN_KEYS
              and all(_text(entry[k]) for k in ("file", "from", "to")), path, "between: [{file, from, to, skip}]")
        _need(entry["skip"] == "all" or (entry["skip"] and _reasons(entry["skip"], TEXT_RULES)), path,
              f"between {entry['file']}: skip is \"all\" or {{rule: reason}}")
    baseline_path = path.parent / BASELINE_NAME
    baseline = _strict_json(baseline_path) if baseline_path.is_file() else {}
    _need(isinstance(baseline, dict) and all(
        isinstance(v, dict) and set(v) <= set(HELD_RULES)
        and all(_count(n) and n > 0 for n in v.values()) for v in baseline.values()),
        baseline_path, f"a baseline is {{file: {{rule: count}}}}, its rules among {HELD_RULES}")
    return Config(folder, path, data, groups, excluded, allowed, between, baseline)


def list_files(root: Path, tree: bool) -> list[str]:
    """Every file under the root, relative: the files git tracks in a
    repository, every file in a folder that is none."""
    if not tree:
        try:
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ConfigError(f"{root}: git ls-files failed ({exc}); use --tree for a folder that is no repository") from exc
        return sorted(p for p in out.decode("utf-8").split("\0") if p and os.path.lexists(root / p))
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", "node_modules", "__pycache__", "venv", ".venv"))
        for name in filenames:
            found.append((Path(dirpath) / name).relative_to(root).as_posix())
    return sorted(found)


@dataclass
class Claim:
    config: Config
    group: Group | None
    excluded: str | None = None


@dataclass
class Tree:
    root: Path
    files: list[str]
    configs: list[Config]
    claims: dict[str, Claim]
    unused: list[tuple[Config, Group, str]]   # group patterns that match no file


def load_tree(root: Path, rules: Rules, tree: bool = False, nested: bool = True) -> Tree:
    """The files, every config and each file's claim."""
    files = list_files(root, tree)
    configs = []
    for path in files:
        if Path(path).name != CONFIG_NAME or (root / path).is_symlink():
            continue
        folder = str(Path(path).parent) if "/" in path else ""
        _need(nested or not folder, root / path, "a nested .prose.json in a public run (--no-nested)")
        configs.append(load_config(root / path, folder, rules))
    _need(any(not c.folder for c in configs), root, f"no {CONFIG_NAME} at the root")
    claims, unused = resolve(files, configs)
    return Tree(root, files, configs, claims, unused)


def resolve(files: list[str], configs: list[Config]) -> tuple[dict[str, Claim], list]:
    """Each file's owner and group. A literal path beats a pattern, an
    exclusion beats a pattern; two literal claims, or two groups' patterns,
    of one file are a configuration error."""
    folders = sorted(configs, key=lambda c: len(c.folder), reverse=True)
    for config in configs:
        nested = [c.folder for c in configs if c is not config and (not config.folder or
                                                                   c.folder.startswith(config.folder + "/"))]
        for group in config.groups:
            for name in group.files:
                _need(not any(config.full(name).startswith(n + "/") for n in nested), config.path,
                      f"{name} lies in a folder with its own {CONFIG_NAME}")
    claims: dict[str, Claim] = {}
    used: set[tuple[int, str]] = set()
    for path in files:
        owner = next(c for c in folders if not c.folder or path.startswith(c.folder + "/"))
        rel = owner.rel(path)
        literal = [g for g in owner.groups if rel in g.files]
        _need(len(literal) + (rel in owner.excluded) <= 1, owner.path, f"{rel} is named twice (claims and exclusions)")
        if literal:
            claims[path] = Claim(owner, literal[0])
            continue
        if rel in owner.excluded:
            claims[path] = Claim(owner, None, owner.excluded[rel])
            continue
        out = [r for p, r in owner.excluded.items() if not _literal(p) and glob_re(p).match(rel)]
        if out:
            claims[path] = Claim(owner, None, out[0])
            continue
        matched = []
        for group in owner.groups:
            hits = [p for p in group.files if not _literal(p) and glob_re(p).match(rel)]
            if hits:
                matched.append(group)
                used.update((id(group), p) for p in hits)
        _need(len(matched) <= 1, owner.path, f"{rel} matches the patterns of {[g.name for g in matched]}")
        if matched:
            claims[path] = Claim(owner, matched[0])
    unused = [(c, g, p) for c in configs for g in c.groups for p in g.files
              if not _literal(p) and (id(g), p) not in used]
    return claims, unused


def stale_claims(tree: Tree) -> list[Finding]:
    """Literal claims of a file that is gone or excluded, and group patterns
    that match no file."""
    found = []
    for config in tree.configs:
        for group in config.groups:
            for name in group.files:
                claim = tree.claims.get(config.full(name))
                if _literal(name) and (claim is None or claim.group is not group):
                    found.append(Finding(config.full(name), 0, "stale-claim",
                                         f"group {group.name!r} names a file that is gone"))
    for config, group, pattern in tree.unused:
        found.append(Finding(config.full(pattern), 0, "stale-claim", f"group {group.name!r}: the pattern matches no file"))
    return found


@dataclass
class Report:
    failures: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    words: dict[str, int] = field(default_factory=dict)
    counts: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)  # config folder -> file -> rule -> n
    advice: int = 0   # sentences past twenty words and long paragraphs of a full run


def _between(path: str, rel: str, source: str, config: Config, report: Report) -> list[tuple[int, int, object]]:
    spans = []
    lines = [line.strip() for line in source.split("\n")]
    for entry in config.between:
        if entry["file"] != rel:
            continue
        a = next((n + 1 for n, line in enumerate(lines) if line == entry["from"]), None)
        b = next((n + 1 for n, line in enumerate(lines) if a and n + 1 > a and line == entry["to"]), None)
        if a is None or b is None:
            report.failures.append(Finding(path, 0, "stale-exception",
                                           f"between: the markers {entry['from']!r} and {entry['to']!r} are missing"))
            continue
        spans.append((a, b, entry["skip"]))
    return spans


def check_tree(tree: Tree, rules: Rules, only: list[str] | None = None, strict: bool = False) -> Report:
    report = Report()
    full_run = only is None
    if full_run:
        report.failures += stale_claims(tree)
    per_file: dict[str, list[Finding]] = {}
    pages: dict[str, Page] = {}
    matched: dict[int, set[str]] = {}
    for path in tree.files if full_run else only:
        claim = tree.claims.get(path)
        symlink = (tree.root / path).is_symlink()
        if claim is None:
            if _is_markdown(path) and not symlink:
                report.failures.append(Finding(path, 0, "unclaimed",
                                               f"no {CONFIG_NAME} claims or excludes this file: add it to a group"))
            elif not full_run:
                report.notes.append(f"{path}: no config claims it, and it is no markdown page")
            continue
        if claim.group is None:
            if not full_run:
                report.notes.append(f"{path}: excluded ({claim.excluded})")
            continue
        if symlink:
            report.failures.append(Finding(path, 0, "symlink", "a symlink is never read: claim the file it points at"))
            continue
        config, group = claim.config, claim.group
        rel = config.rel(path)
        try:
            source = (tree.root / path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConfigError(f"{path}: cannot be read ({exc.strerror})") from exc
        try:
            page = read_page(source, rel, group, rules)
        except ValueError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        pages[path] = page
        matched.setdefault(id(group), set()).update(page.matched)
        report.words[path] = page.words()
        found = check_page(path, page, group, rules, config.allowed.get(rel),
                           _between(path, rel, source, config, report)) + _group_rules(path, rel, page, group)
        per_file[path] = [f for f in found if f.warn or f.rule in HELD_RULES]
        report.failures += [f for f in found if not f.warn and f.rule not in HELD_RULES]
    if full_run:
        for config in tree.configs:
            for rel in list(config.allowed) + [e["file"] for e in config.between]:
                if config.full(rel) not in pages:
                    report.failures.append(Finding(config.full(rel), 0, "stale-exception",
                                                   f"{CONFIG_NAME} has an exception for a file it does not judge"))
            for group in config.groups:
                _group_totals(tree, config, group, pages, report)
                for name in group.fields:
                    if name not in matched.get(id(group), set()):
                        report.failures.append(Finding(config.full(CONFIG_NAME), 0, "stale-claim",
                                                       f"group {group.name!r}: the field {name!r} matches no text"))
    _apply_baselines(tree, per_file, report, full_run, strict)
    return report


def _group_rules(path: str, rel: str, page: Page, group: Group) -> list[Finding]:
    found = []
    if "budget" in group.extras:
        words = page.words()
        budget = group.budgets.get(rel)
        if budget is None:
            found.append(Finding(path, 0, "no-budget", f"{words} words and no budget"))
        elif words > budget:
            found.append(Finding(path, 0, "over-budget",
                                 f"{words} words, budget {budget}: cut first, raise it only for a new feature"))
    if "table-rows" in group.extras:
        cap = group.max_table_rows.get(rel)
        if cap is not None and page.table_rows > cap:
            found.append(Finding(path, 0, "table-rows", f"{page.table_rows} table rows, cap {cap}"))
    if "front-matter" in group.extras and group.front_matter:
        flag = group.front_matter["flag"]
        listed = rel in group.front_matter["files"]
        carries = re.search(rf"^{re.escape(flag)}:\s*true\s*$", page.front, re.MULTILINE) is not None
        if listed != carries:
            found.append(Finding(path, 0, "front-matter", f"must carry {flag}: true" if listed
                                 else f"carries {flag}: true and is not listed under front_matter"))
    return found


def _members(tree: Tree, config: Config, group: Group) -> list[str]:
    return [path for path, claim in tree.claims.items() if claim.config is config and claim.group is group]


def _group_totals(tree: Tree, config: Config, group: Group, pages: dict[str, Page], report: Report) -> None:
    if "budget" not in group.extras:
        return
    mine = {config.rel(path): pages[path] for path in _members(tree, config, group) if path in pages}
    for rel in group.budgets:
        if rel not in mine:
            report.failures.append(Finding(config.full(rel), 0, "stale-budget", "a budget for a file that is gone"))
    if group.total is not None:
        total = sum(page.words() for rel, page in mine.items()
                    if not any(rel.startswith(prefix) for prefix in group.total_excludes))
        report.notes.append(f"{group.name}: {len(mine)} files, {total} words against a total of {group.total}")
        if total > group.total:
            report.failures.append(Finding(f"({group.name})", 0, "over-budget", f"{total} words, total {group.total}"))


def _apply_baselines(tree: Tree, per_file: dict[str, list[Finding]], report: Report, use_baseline: bool,
                     strict: bool) -> None:
    for path, found in per_file.items():
        claim = tree.claims[path]
        config, group = claim.config, claim.group
        rel = config.rel(path)
        counts = Counter(f.rule for f in found if not f.warn)
        report.counts.setdefault(config.folder, {})[rel] = dict(counts)
        base = config.baseline.get(rel, {}) if use_baseline else {}
        warn_level = group.level == "warn" and not strict
        risen: set[str] = set()
        for finding in found:
            if finding.warn:
                # Advice: a sentence past twenty words, a long paragraph. A
                # full run counts it in one line, --file lists it.
                if strict:
                    finding.warn = False
                    report.failures.append(finding)
                elif use_baseline:
                    report.advice += 1
                else:
                    report.warnings.append(finding)
                continue
            count, allowed = counts[finding.rule], base.get(finding.rule, 0)
            if count <= allowed:
                continue
            if allowed:
                finding.detail = finding.detail[:DETAIL - 22] + f"  ({count}, baseline {allowed})"
            if warn_level and allowed:
                # No program can tell which finding is the new one, so a
                # waiting file's rise is one line, not every finding it has.
                if finding.rule not in risen:
                    risen.add(finding.rule)
                    report.warnings.append(Finding(path, 0, "baseline", f"{finding.rule}: {count}, the baseline holds "
                                                   f"{allowed}: a new line breaks the rule, --file lists them", warn=True))
            elif warn_level:
                finding.warn = True
                report.warnings.append(finding)
            else:
                report.failures.append(finding)
        for rule, allowed in sorted(base.items()):
            if counts[rule] < allowed:
                report.warnings.append(Finding(path, 0, "baseline", f"{rule}: the baseline holds {allowed}, the file "
                                               f"has {counts[rule]}: --write-baseline lowers it", warn=True))
    if use_baseline:
        for config in tree.configs:
            judged = report.counts.get(config.folder, {})
            for rel in config.baseline:
                if rel not in judged:
                    report.failures.append(Finding(config.full(rel), 0, "stale-baseline",
                                                   f"{BASELINE_NAME} has an entry for a file nobody claims"))


# --- modes ------------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_baselines(tree: Tree, rules: Rules) -> int:
    """A missing baseline is written whole; an existing one is only lowered.
    A count above its record is refused (raise it by hand, with the reason in
    the commit message)."""
    report = check_tree(tree, rules)
    refused = 0
    for config in sorted(tree.configs, key=lambda c: c.folder):
        counts = {rel: dict(sorted(c.items())) for rel, c in sorted(report.counts.get(config.folder, {}).items()) if c}
        path = config.path.parent / BASELINE_NAME
        if path.is_file():
            for rel, now in counts.items():
                for rule, count in now.items():
                    if count > config.baseline.get(rel, {}).get(rule, 0):
                        print(f"{config.full(rel)}: {rule} {count} is above its record "
                              f"{config.baseline.get(rel, {}).get(rule, 0)}: not raised")
                        refused += 1
            lowered: dict[str, dict[str, int]] = {}
            for rel, old in config.baseline.items():
                now = counts.get(rel, {})
                kept = {rule: min(n, now[rule]) for rule, n in old.items() if now.get(rule)}
                if kept:
                    lowered[rel] = kept
            counts = lowered
        elif not counts:
            continue
        _write_json(path, counts)
        print(f"{path.relative_to(tree.root)}: {sum(sum(c.values()) for c in counts.values())} findings in "
              f"{len(counts)} files")
    return 1 if refused else 0


def show_words(tree: Tree, rules: Rules) -> int:
    report = check_tree(tree, rules)
    for path, words in sorted(report.words.items()):
        claim = tree.claims[path]
        print(f"{words:>6} / {claim.group.budgets.get(claim.config.rel(path), '-'):>6}  {path}")
    for note in report.notes:
        if "against a total" in note:
            print(note)
    return 0


def write_budgets(tree: Tree, rules: Rules) -> int:
    """Budgets from today's files, rounded up with room for a small correction."""
    report = check_tree(tree, rules)
    for config in tree.configs:
        groups = [(raw, group) for raw, group in zip(config.data.get("groups", []), config.groups, strict=True)
                  if "budget" in group.extras]
        for raw, group in groups:
            mine = {config.rel(path): report.words[path] for path in _members(tree, config, group)
                    if path in report.words}
            raw["budgets"] = {rel: int(math.ceil(n * 1.03 / 10.0)) * 10 for rel, n in sorted(mine.items())}
            if group.total is not None:
                total = sum(n for rel, n in mine.items() if not any(rel.startswith(x) for x in group.total_excludes))
                raw["total"] = int(math.ceil(total * 1.02 / 100.0)) * 100
            print(f"{config.full(CONFIG_NAME)}: {group.name}: {len(mine)} budgets, total {raw.get('total', '-')}")
        if groups:
            _write_json(config.path, config.data)
    return 0


def run(tree: Tree, rules: Rules, only: list[str] | None, strict: bool) -> int:
    report = check_tree(tree, rules, only, strict)
    for finding in sorted(report.failures, key=lambda f: (f.file, f.line, f.rule)):
        print(finding)
    for finding in sorted(report.warnings, key=lambda f: (f.file, f.line, f.rule)):
        print(finding)
    for note in report.notes:
        print(note)
    if report.advice:
        print(f"{report.advice} pieces of advice (sentences past twenty words, long paragraphs): --file lists them")
    stamp = f"engine {file_hash(Path(__file__))}, rules {file_hash(RULES_FILE)}"
    if report.failures:
        counts = Counter(f.rule for f in report.failures)
        summary = ", ".join(f"{rule} {n}" for rule, n in sorted(counts.items()))
        print(f"NOTE: {len(report.failures)} writing findings ({summary}) in {len(report.words)} files; "
              f"the rules are in {RULES_PAGE} ({stamp})")
        return 1
    held = sum(sum(sum(c.values()) for c in files.values()) for files in report.counts.values())
    extras = []
    if report.warnings:
        extras.append(f"{len(report.warnings)} warning" + ("s" if len(report.warnings) > 1 else ""))
    if report.advice:
        extras.append(f"{report.advice} pieces of advice")
    if held and only is None:
        extras.append(f"{held} findings held in baselines")
    print(f"NOTE: {len(report.words)} files, {sum(report.words.values())} words follow the writing rules a program "
          f"can check{''.join(', ' + extra for extra in extras)} ({RULES_PAGE}; {stamp})")
    return 0


# --- the self-test ----------------------------------------------------------------


def self_test(rules: Rules) -> int:
    """Each case of the rule set gives what it names: the rules it raises and
    no other that fails, and where it says so the lines, the table rows, the
    blocks and their texts, the code lines, the json fields matched."""
    problems = []
    cases = rules.data.get("self_test", {}).get("cases")
    if not isinstance(cases, list) or not cases:
        problems.append("rules.json has no self_test cases")
        cases = []
    for case in cases:
        use = case.get("use", "reader")
        group = Group(name="t", use=use, extras=frozenset(rules.data["sets"][use]), files=["t.md"],
                      read=case.get("read", "markdown"), fields=case.get("fields", []))
        page = read_page(case["text"], "t.md", group, rules)
        found = [f for f in check_page("t.md", page, group, rules, case.get("allowed")) if not f.warn]
        got = {
            "raises": sorted({f.rule for f in found}),
            "lines": sorted({f.line for f in found}),
            "rows": page.table_rows,
            "blocks": [block.kind for block in page.blocks],
            "texts": [block.text.s for block in page.blocks],
            "code": len(page.code),
            "matched": sorted(page.matched),
        }
        for key, value in got.items():
            want = case.get(key)
            if want is not None and value != (sorted(want) if key in ("raises", "matched") else want):
                problems.append(f"{case['text'][:60]!r}: {key} {value}, the case says {want}")
    spans = split_sentences("One idea. two ideas? Three, e.g. four. 5 more.")
    if spans != [(0, 10), (10, 21), (21, 39), (39, 46)]:
        problems.append(f"the sentence split, got {spans}")
    if not glob_re("docs/**/*.md").match("docs/intro.md") or glob_re("*/README.md").match("a/b/README.md"):
        problems.append("the path patterns")
    for problem in problems:
        print(f"self-test: {problem}")
    return len(problems)


# --- the command line ---------------------------------------------------------------

def _file_paths(root: Path, paths: list[str], tree: Tree) -> list[str]:
    """--file paths relative to the root: any regular file under it, tracked
    or not, so a page just written is judged. A path that does not exist, a
    folder, a symlink and a path outside the root are refused."""
    out = []
    for given in paths:
        path = Path(os.path.abspath(given)) if Path(given).is_absolute() else root / given
        try:
            rel = Path(os.path.normpath(path)).relative_to(root).as_posix()
        except ValueError as exc:
            raise ConfigError(f"--file {given}: not under {root}") from exc
        full = root / rel
        _need(not rel.startswith("..") and not full.is_symlink(), root, f"--file {given}: a symlink or outside the root")
        _need(full.is_file(), root, f"--file {given}: no such file under the root")
        out.append(rel)
    fresh = [rel for rel in out if rel not in tree.claims and rel not in tree.files]
    tree.claims.update(resolve(fresh, tree.configs)[0])
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="prose_check.py", usage=__doc__.split("Usage:", 1)[1].split("--no-nested")[0])
    parser.add_argument("root", nargs="?")
    parser.add_argument("--self-test", action="store_true")
    for flag in ("--tree", "--no-nested", "--strict"):
        parser.add_argument(flag, action="store_true")
    mode = parser.add_mutually_exclusive_group()
    for flag in ("--words", "--write-budgets", "--write-baseline"):
        mode.add_argument(flag, action="store_true")
    mode.add_argument("--file", nargs="+")
    args = parser.parse_args(argv)
    if args.self_test != (args.root is None) or (args.self_test and len(argv) > 1):
        parser.error("give a root, or --self-test alone")
    try:
        rules = load_rules()
        if args.self_test:
            failures = self_test(rules)
            print(f"self-test: {'FAILED' if failures else 'ok'}")
            return 1 if failures else 0
        if self_test(rules):
            print("prose_check: the self-test failed", file=sys.stderr)
            return 2
        root = Path(args.root).resolve()
        tree = load_tree(root, rules, tree=args.tree, nested=not args.no_nested)
        if args.words:
            return show_words(tree, rules)
        if args.write_budgets:
            return write_budgets(tree, rules)
        if args.write_baseline:
            return write_baselines(tree, rules)
        return run(tree, rules, _file_paths(root, args.file, tree) if args.file else None, args.strict)
    except ConfigError as exc:
        print(f"prose_check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
