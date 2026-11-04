"""Find and replace in a Word document, run by run, and template tokens.

Matching sees one paragraph's visible text at a time and never crosses
paragraphs. A paragraph's text is the runs it owns: those directly under
``w:p`` and inside its inline containers (hyperlinks, tracked insertions and
moves, smart tags, custom XML, simple fields, bidi wrappers, inline content
controls). Deleted text, field instructions and text box paragraphs are not
part of it; a text box's paragraphs are searched as paragraphs of their own.

Constraints:

- The replacement goes into the run where the match starts and takes that
  run's formatting. No run is ever split into two, so text outside a match
  keeps its runs and properties. Markers between runs (bookmarks, comment
  ranges, proofing marks, permissions) are never moved or removed.
- A match that crosses the edge of an inline container or a complex field
  character is skipped and counted, never edited.
- A text box is stored twice (``mc:Choice`` and its VML ``mc:Fallback``):
  both copies are edited the same way and only the first is counted.
- Headers and footers are reached through the sections' references, never
  through python-docx's section accessors, which add a header part to a
  section that has none.
- Parts python-docx holds as bytes (footnotes, endnotes) are parsed once per
  call and written back only when changed: an untouched part keeps its
  exact bytes.
"""

import re
import time
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from formats.docx import annotations
from tokens import token_pattern

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_W_P = _W + "p"
_W_R = _W + "r"
_W_RPR = _W + "rPr"
_W_T = _W + "t"
_W_SDT = _W + "sdt"
_W_SDT_CONTENT = _W + "sdtContent"
_W_FLD_CHAR = _W + "fldChar"
_W_FLD_CHAR_TYPE = _W + "fldCharType"
_W_SECT_PR = _W + "sectPr"
_W_TYPE = _W + "type"
_W_VAL = _W + "val"
_PLACEHOLDER_PATH = f"{_W}sdtPr/{_W}showingPlcHdr"
_DATA_BINDING_PATH = f"{_W}sdtPr/{_W}dataBinding"
_MC_CHOICE = _MC + "Choice"
_MC_FALLBACK = _MC + "Fallback"

# Inline wrappers whose runs are this paragraph's visible text; an inline
# w:sdt is one too, through its w:sdtContent.
_CONTAINERS = frozenset(_W + tag for tag in (
    "hyperlink", "ins", "moveTo", "smartTag", "customXml", "fldSimple", "dir", "bdo",
))
_ATOM_TEXT = {_W + "tab": "\t", _W + "br": "\n", _W + "cr": "\n", _W + "noBreakHyphen": "‑"}

_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_RT_FOOTNOTES = _RT + "footnotes"
_RT_ENDNOTES = _RT + "endnotes"
_RT_COMMENTS = _RT + "comments"
_RT_SETTINGS = _RT + "settings"
_NOTE_SEPARATORS = frozenset({"separator", "continuationSeparator", "continuationNotice"})

_SCOPE_KINDS = {
    "all": ("body", "headers", "footers", "notes"),
    "body": ("body",),
    "headers": ("headers",),
    "footers": ("footers",),
    "notes": ("notes",),
    "comments": ("comments",),
}
_REPLACEMENT_BREAKS = re.compile(r"(\r\n|[\r\n\t])")
_NOT_STORABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
_LEADING_FLAGS = re.compile(r"\(\?[aiLmsux]+\)")


# A word goes on through its combining marks: "cafe" + U+0301 is one word.
_WORD = r"\w\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
MAX_REGEX_LENGTH = 1000
SEARCH_BUDGET_S = 20.0


def _ambiguous(items: Any) -> bool:
    """Whether a parsed regex can match the same text in more than one way:
    an inner repeat of variable length, or an alternation."""
    from re import _constants as c

    for op, av in items:
        if op is c.BRANCH:
            return True
        if op in (c.MAX_REPEAT, c.MIN_REPEAT):
            low, high, inner = av
            if low != high or _ambiguous(inner):
                return True
        elif op is c.SUBPATTERN:
            if _ambiguous(av[3]):
                return True
        elif op in (c.ASSERT, c.ASSERT_NOT):
            if _ambiguous(av[1]):
                return True
    return False


def _runaway(items: Any) -> bool:
    """Whether a parsed regex repeats a group that can match its text in
    more than one way: the shape that backtracks exponentially, (a+)+ or
    (a|aa)*. Possessive repeats and atomic groups never backtrack."""
    from re import _constants as c

    for op, av in items:
        if op in (c.MAX_REPEAT, c.MIN_REPEAT):
            low, high, inner = av
            if high > 1 and _ambiguous(inner):
                return True
            if _runaway(inner):
                return True
        elif op is c.SUBPATTERN:
            if _runaway(av[3]):
                return True
        elif op is c.BRANCH:
            if any(_runaway(branch) for branch in av[1]):
                return True
        elif op in (c.ASSERT, c.ASSERT_NOT):
            if _runaway(av[1]):
                return True
    return False


def _check_regex(source: str) -> None:
    from re import _parser

    if len(source) > MAX_REGEX_LENGTH:
        raise ValueError(f"The regex is {len(source):,} characters long; the limit is {MAX_REGEX_LENGTH:,}. "
                         "Search plain text (regex: false) or a shorter pattern.")
    try:
        parsed = _parser.parse(source)
    except re.error as exc:
        raise ValueError(f"The regex does not compile: {exc}.") from None
    if _runaway(parsed.data):
        raise ValueError("The regex repeats a group that can match the same text in more than one way, such "
                         "as (a+)+ or (a|aa)*; on a long paragraph it would run for minutes. Repeat a single "
                         "character class instead, such as (\\w+ ?) becoming [\\w ]+, or search plain text.")


def compile_pattern(find: str, *, match_case: bool = True, whole_word: bool = False,
                    regex: bool = False) -> "re.Pattern[str]":
    """The pattern for ``find``: literal unless ``regex``, case-folded
    (Unicode, so Greek final sigma folds) unless ``match_case``, bounded by
    letters, digits and combining marks when ``whole_word``. Literal text is
    found in its composed and decomposed forms. Raises ValueError for an
    empty find, or a regex that does not compile or could run away."""
    if not find:
        raise ValueError("The text to find is empty: give the text to look for.")
    find = unicodedata.normalize("NFC", find)
    if regex:
        _check_regex(find)
        source = find
    else:
        forms = dict.fromkeys((find, unicodedata.normalize("NFD", find)))
        source = "|".join(re.escape(form) for form in forms)
    if whole_word:
        flags = _LEADING_FLAGS.match(source) if regex else None
        head = flags.group(0) if flags else ""
        source = rf"{head}(?<![{_WORD}])(?:{source[len(head):]})(?![{_WORD}])"
    elif not regex and "|" in source:
        source = f"(?:{source})"
    try:
        return re.compile(source, 0 if match_case else re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"The regex does not compile: {exc}.") from None


@dataclass
class Hit:
    """One paragraph that changed or held a skipped match."""

    paragraph: Any
    count: int
    skipped: int
    scope: str
    part: Any


@dataclass
class ReplaceResult:
    """What ``replace_all`` did. Text box Fallback copies are edited but never
    counted; ``by_scope`` lists only scopes with replacements."""

    hits: list[Hit] = field(default_factory=list)
    total: int = 0
    skipped: int = 0
    placeholder_cleared: int = 0
    data_bound: int = 0
    changed_parts: list[Any] = field(default_factory=list)
    by_scope: dict[str, int] = field(default_factory=dict)
    # Why tracked matches were left as they are ("another author's insertion").
    skipped_because: list[str] = field(default_factory=list)


@dataclass
class TemplateResult:
    """What ``fill_template`` did. Tokens are reported as written."""

    filled: dict[str, int] = field(default_factory=dict)
    unresolved: list[tuple[Any, str, str]] = field(default_factory=list)
    unused: list[str] = field(default_factory=list)
    skipped: list[tuple[Any, str, str]] = field(default_factory=list)
    changed_parts: list[Any] = field(default_factory=list)


@dataclass
class _Atom:
    element: Any
    run: Any
    start: int
    segment: int
    in_field: bool = False


_MARKERS = frozenset(_W + tag for tag in (
    "pPr", "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd", "proofErr",
    "permStart", "permEnd", "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
    "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart", "customXmlDelRangeEnd",
))


# Run content that draws nothing and holds no text: neither an atom nor an
# edge. Every other non-text child (a note or comment reference, a drawing,
# a symbol, an embedded object) is an edge, so no edit moves or deletes it.
_RUN_NEUTRAL = frozenset(_W + tag for tag in ("rPr", "lastRenderedPageBreak", "softHyphen"))


class _Flat:
    """One paragraph's visible text as atoms (a ``w:t``, tab, break or
    no-break hyphen), each tagged with a segment that changes at every
    container edge, every field character and every run child that shows
    something other than text."""

    def __init__(self, p: Any) -> None:
        self.atoms: list[_Atom] = []
        self.owner: list[int] = []
        self._pieces: list[str] = []
        self._segment = 0
        self._fields: list[bool] = []
        self._walk(p)
        self.text = "".join(self._pieces)

    def _walk(self, parent: Any) -> None:
        for child in parent:
            tag = child.tag
            if tag == _W_R:
                self._run(child)
            elif tag in _CONTAINERS or tag == _W_SDT:
                inner = child.find(_W_SDT_CONTENT) if tag == _W_SDT else child
                self._segment += 1
                if inner is not None:
                    self._walk(inner)
                self._segment += 1
            elif tag not in _MARKERS:
                # An equation, a deletion, ruby text: content a match must not
                # bridge, since the edit would leave it stranded mid-word.
                self._segment += 1

    def _run(self, run: Any) -> None:
        for child in run:
            tag = child.tag
            if tag == _W_FLD_CHAR:
                self._field_char(child.get(_W_FLD_CHAR_TYPE))
                continue
            # Inside any open field's instruction nothing is displayed.
            if not all(self._fields):
                continue
            if tag != _W_T and tag not in _ATOM_TEXT:
                if tag not in _RUN_NEUTRAL:
                    self._segment += 1
                continue
            text = (child.text or "") if tag == _W_T else _ATOM_TEXT[tag]
            if text:
                self.atoms.append(_Atom(child, run, len(self.owner), self._segment, bool(self._fields)))
                self.owner.extend([len(self.atoms) - 1] * len(text))
                self._pieces.append(text)

    def _field_char(self, kind: str | None) -> None:
        self._segment += 1
        if kind == annotations.FIELD_BEGIN:
            self._fields.append(False)
        elif kind == annotations.FIELD_SEPARATE and self._fields:
            self._fields[-1] = True
        elif kind == annotations.FIELD_END and self._fields:
            self._fields.pop()

    def one_segment(self, start: int, end: int) -> bool:
        return self.atoms[self.owner[start]].segment == self.atoms[self.owner[end - 1]].segment


def _set_text(t: Any, text: str) -> None:
    t.text = text
    if text != text.strip():
        t.set(_XML_SPACE, "preserve")


def _drop(element: Any) -> None:
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def _nodes(run: Any, items: list[str]) -> list[Any]:
    """Run content for alternating text and break items (``\\t`` a tab,
    anything else a line break); empty text makes no node."""
    nodes = []
    for index, item in enumerate(items):
        if index % 2:
            nodes.append(run.makeelement(_W + ("tab" if item == "\t" else "br")))
        elif item:
            t = run.makeelement(_W_T)
            _set_text(t, item)
            nodes.append(t)
    return nodes


def _replace_span(flat: _Flat, start: int, end: int, text: str) -> None:
    first, last = flat.owner[start], flat.owner[end - 1]
    covered = flat.atoms[first:last + 1]
    head = covered[0]
    items = _REPLACEMENT_BREAKS.split(text)
    if head.element.tag == _W_T:
        current = head.element.text or ""
        items[0] = current[:start - head.start] + items[0]
        if first == last:
            items[-1] += current[end - head.start:]
        anchor = head.element
        for node in _nodes(head.run, ["", *items[1:]]):
            anchor.addnext(node)
            anchor = node
        if items[0]:
            _set_text(head.element, items[0])
        else:
            _drop(head.element)
    else:
        for node in _nodes(head.run, items):
            head.element.addprevious(node)
        _drop(head.element)
    for atom in covered[1:-1]:
        _drop(atom.element)
    if last != first:
        tail = covered[-1]
        rest = (tail.element.text or "")[end - tail.start:] if tail.element.tag == _W_T else ""
        if rest:
            _set_text(tail.element, rest)
        else:
            _drop(tail.element)
    for run in {id(a.run): a.run for a in covered}.values():
        if all(child.tag == _W_RPR for child in run):
            _drop(run)


def _edit(flat: _Flat, edits: list[tuple[int, int, str]], tracked: Any = None) -> tuple[int, int]:
    """Apply ``edits`` (start, end, text) from last to first, as tracked
    changes when ``tracked`` (a ``revisions.Revision``) is given. Returns
    (content controls whose placeholder flag was cleared, edits inside a
    data-bound content control)."""
    controls: dict[int, Any] = {}
    bound = 0
    for start, _end, _text in edits:
        sdts = list(flat.atoms[flat.owner[start]].run.iterancestors(_W_SDT))
        controls.update((id(sdt), sdt) for sdt in sdts)
        bound += any(sdt.find(_DATA_BINDING_PATH) is not None for sdt in sdts)
    for start, end, text in reversed(edits):
        if tracked is not None:
            tracked.apply(flat, start, end, text)
        else:
            _replace_span(flat, start, end, text)
    cleared = 0
    for sdt in controls.values():
        flag = sdt.find(_PLACEHOLDER_PATH)
        if flag is not None:
            _drop(flag)
            cleared += 1
    return cleared, bound


class _Roots:
    """Root elements of story parts for one call. A part python-docx holds
    as bytes is parsed once and written back by ``save`` only if listed."""

    def __init__(self) -> None:
        self._parsed: dict[int, tuple[Any, Any]] = {}

    def get(self, part: Any) -> Any | None:
        element = getattr(part, "element", None)
        if element is not None:
            return element
        key = id(part)
        if key not in self._parsed:
            from docx.oxml import parse_xml

            try:
                root = parse_xml(part.blob)
            except etree.XMLSyntaxError:
                root = None
            self._parsed[key] = (part, root)
        return self._parsed[key][1]

    def save(self, parts: Iterable[Any]) -> None:
        for part in parts:
            entry = self._parsed.get(id(part))
            if entry is not None and entry[1] is not None:
                part._blob = etree.tostring(
                    entry[1], xml_declaration=True, encoding="UTF-8", standalone=True)


def _related(doc: Any, reltype: str) -> list[Any]:
    parts: list[Any] = []
    for rel in doc.part.rels.values():
        if rel.reltype == reltype and not rel.is_external and rel.target_part not in parts:
            parts.append(rel.target_part)
    return parts


def _section_parts(doc: Any, reference: str) -> list[Any]:
    """Header or footer parts in section order, each once."""
    related = doc.part.related_parts
    parts: list[Any] = []
    for sect in doc.element.body.iter(_W_SECT_PR):
        for ref in sect.findall(_W + reference):
            part = related.get(ref.get(_R_ID))
            if part is not None and part not in parts:
                parts.append(part)
    return parts


def _stories(doc: Any, kind: str, roots: _Roots) -> Iterator[tuple[str, Any, Any, list[Any]]]:
    """(scope name, part, root, elements whose paragraphs are searched)."""
    if kind == "body":
        yield "body", doc.part, doc.part.element, [doc.element.body]
        return
    if kind in ("headers", "footers"):
        name = kind[:-1]
        for part in _section_parts(doc, name + "Reference"):
            root = roots.get(part)
            if root is not None:
                yield name, part, root, [root]
        return
    if kind == "comments":
        sources = (("comment", _RT_COMMENTS),)
    else:
        sources = (("footnote", _RT_FOOTNOTES), ("endnote", _RT_ENDNOTES))
    for name, reltype in sources:
        for part in _related(doc, reltype):
            root = roots.get(part)
            if root is None:
                continue
            if name == "comment":
                yield name, part, root, [root]
            else:
                notes = [n for n in root.iterchildren(_W + name)
                         if n.get(_W_TYPE) not in _NOTE_SEPARATORS]
                yield name, part, root, notes


def _in_fallback(p: Any) -> bool:
    return next(p.iterancestors(_MC_FALLBACK), None) is not None


def _kinds(scope: str) -> tuple[str, ...]:
    kinds = _SCOPE_KINDS.get(scope)
    if kinds is None:
        raise ValueError(f'The scope "{scope}" is unknown: use one of {", ".join(_SCOPE_KINDS)}.')
    return kinds


def _iter_paragraphs(doc: Any, scope: str, roots: _Roots) -> Iterator[tuple[str, Any, Any, bool]]:
    for kind in _kinds(scope):
        for name, part, _root, containers in _stories(doc, kind, roots):
            for container in containers:
                for p in list(container.iter(_W_P)):
                    yield name, part, p, _in_fallback(p)


def iter_scope_paragraphs(doc: Any, scope: str = "all") -> Iterator[tuple[str, Any, Any, bool]]:
    """(scope name, part, ``w:p``, is a text box Fallback copy) for every
    paragraph of ``scope``, in document order: "all" is the body, headers,
    footers, footnotes and endnotes; "body", "headers", "footers", "notes"
    and "comments" select one kind ("comments" is never part of "all").

    Footnote and endnote paragraphs come from a fresh parse of the part's
    bytes: editing them changes the file only through ``replace_all`` or
    ``fill_template``."""
    yield from _iter_paragraphs(doc, scope, _Roots())


def _twin(p: Any, side: str, other: str) -> Any | None:
    """The paragraph at the same place in the other copy of a text box."""
    holder = next(p.iterancestors(side), None)
    alternate = holder.getparent() if holder is not None else None
    twin_holder = alternate.find(other) if alternate is not None else None
    if twin_holder is None:
        return None
    ours, theirs = list(holder.iter(_W_P)), list(twin_holder.iter(_W_P))
    return theirs[ours.index(p)] if len(ours) == len(theirs) else None


def _entries(doc: Any, scope: str, paragraphs: Iterable[Any] | None,
             roots: _Roots) -> list[tuple[str, Any, Any, bool]]:
    _kinds(scope)
    if paragraphs is None:
        return list(_iter_paragraphs(doc, scope, roots))
    owners: dict[Any, tuple[str, Any]] = {}
    for kind in ("body", "headers", "footers", "comments"):
        for name, part, root, _containers in _stories(doc, kind, roots):
            owners[root] = (name, part)
    entries: list[tuple[str, Any, Any, bool]] = []
    seen: set[int] = set()
    for p in paragraphs:
        for q in (p, _twin(p, _MC_CHOICE, _MC_FALLBACK)):
            if q is None or id(q) in seen:
                continue
            owner = owners.get(q.getroottree().getroot())
            if owner is None:
                raise ValueError("A paragraph to search is not in this document's body, "
                                 "headers, footers or comments.")
            seen.add(id(q))
            entries.append((owner[0], owner[1], q, _in_fallback(q)))
    return entries


def _check_storable(text: str, what: str) -> None:
    bad = _NOT_STORABLE.search(text)
    if bad:
        raise ValueError(f"{what} holds the character U+{ord(bad.group(0)):04X}, which a Word "
                         "file cannot store: remove it.")


def _add_part(parts: list[Any], part: Any) -> None:
    if not any(part is known for known in parts):
        parts.append(part)


def replace_all(doc: Any, pattern: "re.Pattern[str]", replacement: str, *,
                regex_template: bool = False, scope: str = "all", limit: int | None = None,
                paragraphs: Iterable[Any] | None = None, tracked: Any = None) -> ReplaceResult:
    """Replace every match of ``pattern`` in ``scope`` (or in ``paragraphs``
    only, plus their text box Fallback copies), at most ``limit`` in document
    order. ``replacement`` is literal text unless ``regex_template``, when it
    is a ``match.expand`` template; ``\\n`` in it becomes a line break and
    ``\\t`` a tab. With ``tracked`` (a ``revisions.Revision``) each edit is
    a tracked change, and a match it cannot record is skipped with its
    reason. Nothing changes when a ValueError is raised."""
    _check_storable(replacement, "The replacement")
    if regex_template:
        try:
            pattern.sub(replacement, "")
        except (re.error, IndexError) as exc:
            raise ValueError(f"The replacement template does not expand: {exc}.") from None
    if limit is not None and limit < 0:
        raise ValueError("The limit is negative: give 0 or more.")
    roots = _Roots()
    plans = []
    remaining = limit
    counted: dict[Any, int] = {}
    why_skipped: set[str] = set()
    started = time.monotonic()
    for name, part, p, fallback in _entries(doc, scope, paragraphs, roots):
        if time.monotonic() - started > SEARCH_BUDGET_S:
            raise ValueError(f"The search ran longer than {SEARCH_BUDGET_S:g} seconds. Give a simpler pattern, "
                             "plain text (regex: false), or a target to search one part of the document.")
        cap = remaining
        if fallback and limit is not None:
            cap = counted.get(_twin(p, _MC_FALLBACK, _MC_CHOICE), remaining)
        if cap == 0:
            continue
        flat = _Flat(p)
        edits: list[tuple[int, int, str]] = []
        skipped = 0
        for match in pattern.finditer(flat.text):
            if match.start() == match.end():
                continue
            if not flat.one_segment(match.start(), match.end()):
                skipped += 1
                continue
            if tracked is not None:
                reason = tracked.skip(flat, match.start(), match.end())
                if reason is not None:
                    skipped += 1
                    if not fallback:
                        why_skipped.add(reason)
                    continue
            if cap is not None and len(edits) >= cap:
                break
            text = replacement
            if regex_template:
                text = match.expand(replacement)
                _check_storable(text, "The expanded replacement")
            edits.append((match.start(), match.end(), text))
        if not fallback:
            counted[p] = len(edits)
            if remaining is not None:
                remaining -= len(edits)
        if edits or (skipped and not fallback):
            plans.append((name, part, p, fallback, flat, edits, skipped))

    result = ReplaceResult()
    for name, part, p, fallback, flat, edits, skipped in plans:
        cleared = bound = 0
        if edits:
            cleared, bound = _edit(flat, edits, tracked)
            _add_part(result.changed_parts, part)
        if fallback:
            continue
        result.hits.append(Hit(p, len(edits), skipped, name, part))
        result.total += len(edits)
        result.skipped += skipped
        result.placeholder_cleared += cleared
        result.data_bound += bound
        if edits:
            result.by_scope[name] = result.by_scope.get(name, 0) + len(edits)
    result.skipped_because = sorted(why_skipped)
    roots.save(result.changed_parts)
    return result


_OFF_VALUES = ("0", "false", "off")


def tracking_is_on(doc: Any) -> bool:
    """Whether the document records edits as tracked changes
    (``w:trackRevisions`` in its settings)."""
    roots = _Roots()
    for part in _related(doc, _RT_SETTINGS):
        settings = roots.get(part)
        track = settings.find(_W + "trackRevisions") if settings is not None else None
        if track is not None:
            return track.get(_W_VAL, "true").lower() not in _OFF_VALUES
    return False


def fill_template(doc: Any, data: Mapping[str, str], *,
                  delimiters: tuple[str, str] = ("{{", "}}"),
                  scope: str = "all") -> TemplateResult:
    """Replace each token (opening delimiter, optional spaces, a key, optional
    spaces, closing delimiter) whose key is in ``data`` with its value, in one
    pass: a value that holds a token is never filled again. Keys match exactly
    after trimming; ``\\n`` in a value is a line break and ``\\t`` a tab. A
    token with no value stays and is reported in ``unresolved``; a token
    crossing a container edge or a field character stays and is reported in
    ``skipped``. Nothing changes when a ValueError is raised."""
    pattern = token_pattern(delimiters)
    for key, value in data.items():
        if not isinstance(value, str):
            raise ValueError(f'The value of "{key}" is not text: give it as a string.')
        _check_storable(value, f'The value of "{key}"')
    roots = _Roots()
    result = TemplateResult()
    counts: dict[str, int] = {}
    crossing: set[str] = set()
    plans = []
    for name, part, p, fallback in _entries(doc, scope, None, roots):
        flat = _Flat(p)
        edits: list[tuple[int, int, str]] = []
        for match in pattern.finditer(flat.text):
            key = match.group("key").strip()
            if not key:
                continue
            report = None
            if key not in data:
                report = result.unresolved
            elif not flat.one_segment(match.start(), match.end()):
                report = result.skipped
                crossing.add(key)
            else:
                edits.append((match.start(), match.end(), data[key]))
                if not fallback:
                    counts[key] = counts.get(key, 0) + 1
            if report is not None and not fallback:
                report.append((p, name, match.group(0)))
        if edits:
            plans.append((part, flat, edits))
    for part, flat, edits in plans:
        _edit(flat, edits)
        _add_part(result.changed_parts, part)
    roots.save(result.changed_parts)
    result.filled = {key: counts[key] for key in data if key in counts}
    result.unused = [key for key in data if key not in counts and key not in crossing]
    return result
