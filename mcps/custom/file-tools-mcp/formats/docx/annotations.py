"""Footnotes, endnotes and comments: their parts, and the ids their marks
and revisions carry.

python-docx loads ``footnotes.xml`` and ``endnotes.xml`` as bytes. Paragraph
and run objects need a story part (styles, pictures, the document part), so
``NotesPart`` is registered for both content types whenever a package
opens; a notes part that is not well-formed XML still loads as bytes, so
the document opens and its notes are read-only. Comment companions
(``commentsExtended``, ``commentsIds``, ``commentsExtensible``, ``people``)
load as XML parts, so one parsed element per part is what every reader and
writer sees.

Parts are found through the document part's relationships, never through
python-docx's ``doc.comments`` or ``doc.settings``, which create the part
they read. A part is created only by an operation that adds the first note
or comment.
"""

from typing import Any

from lxml import etree

from results import Code, OpError

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID_NS = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX_NS = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
_W = f"{{{W_NS}}}"
W_ID = _W + "id"

_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
RT_FOOTNOTES = _RT + "footnotes"
RT_ENDNOTES = _RT + "endnotes"
RT_COMMENTS = _RT + "comments"
RT_SETTINGS = _RT + "settings"
RT_STYLES = _RT + "styles"
RT_NUMBERING = _RT + "numbering"
RT_HEADER = _RT + "header"
RT_FOOTER = _RT + "footer"
RT_COMMENTS_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
RT_COMMENTS_IDS = "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"
RT_COMMENTS_EXTENSIBLE = "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"
RT_PEOPLE = "http://schemas.microsoft.com/office/2011/relationships/people"

_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml."
CT_FOOTNOTES = _CT + "footnotes+xml"
CT_ENDNOTES = _CT + "endnotes+xml"
CT_COMMENTS = _CT + "comments+xml"
CT_COMMENTS_EXTENDED = _CT + "commentsExtended+xml"
CT_COMMENTS_IDS = _CT + "commentsIds+xml"
CT_COMMENTS_EXTENSIBLE = _CT + "commentsExtensible+xml"
CT_PEOPLE = _CT + "people+xml"

FOOTNOTE = "footnote"
ENDNOTE = "endnote"
COMMENT = "comment"


class NoteKind:
    """What differs between footnotes and endnotes."""

    def __init__(self, name: str, reltype: str, content_type: str, style: str, ref_style: str) -> None:
        self.name = name
        self.reltype = reltype
        self.content_type = content_type
        self.root = _W + f"{name}s"
        self.note = _W + name
        self.reference = _W + f"{name}Reference"
        self.own_mark = _W + f"{name}Ref"
        self.properties = f"{name}Pr"
        self.style = style
        self.ref_style = ref_style
        self.partname = f"/word/{name}s.xml"


NOTE_KINDS = {
    FOOTNOTE: NoteKind(FOOTNOTE, RT_FOOTNOTES, CT_FOOTNOTES, "Footnote Text", "Footnote Reference"),
    ENDNOTE: NoteKind(ENDNOTE, RT_ENDNOTES, CT_ENDNOTES, "Endnote Text", "Endnote Reference"),
}

# A note of these types is the separator line, not a note anyone wrote.
_SEPARATOR_TYPES = frozenset({"separator", "continuationSeparator", "continuationNotice"})
_MAX_ID = 0x7FFFFFFF


def _notes_part_class() -> type:
    from docx.opc.part import Part
    from docx.parts.story import StoryPart

    class NotesPart(StoryPart):
        """A footnotes or endnotes part as a story part; bytes when its XML
        does not parse."""

        @classmethod
        def load(cls, partname: Any, content_type: str, blob: bytes, package: Any) -> Any:
            from docx.oxml.parser import parse_xml

            try:
                element = parse_xml(blob)
            except etree.XMLSyntaxError:
                return Part(partname, content_type, blob, package)
            return cls(partname, content_type, element, package)

    return NotesPart


_CLASSES: list[type] = []


def notes_part_class() -> type:
    if not _CLASSES:
        _CLASSES.append(_notes_part_class())
    return _CLASSES[0]


def register_part_types() -> None:
    """Load notes as story parts and comment companions as XML parts, from
    the next package opened on. Idempotent."""
    from docx.opc.part import PartFactory, XmlPart

    cls = notes_part_class()
    for content_type in (CT_FOOTNOTES, CT_ENDNOTES):
        PartFactory.part_type_for[content_type] = cls
    for content_type in (CT_COMMENTS_EXTENDED, CT_COMMENTS_IDS, CT_COMMENTS_EXTENSIBLE, CT_PEOPLE):
        PartFactory.part_type_for.setdefault(content_type, XmlPart)


def related(doc: Any, reltype: str) -> list[Any]:
    """The parts the document part relates to by ``reltype``, without
    creating any."""
    return [rel.target_part for rel in doc.part.rels.values()
            if not rel.is_external and rel.reltype == reltype]


def first_related(doc: Any, reltype: str) -> Any | None:
    parts = related(doc, reltype)
    return parts[0] if parts else None


def root_of_part(part: Any) -> Any | None:
    """The part's parsed element, or None for a part that stayed bytes."""
    return getattr(part, "element", None)


def is_separator(note: Any) -> bool:
    return note.get(_W + "type") in _SEPARATOR_TYPES


def editable_root(part: Any, what: str) -> Any:
    """The root of a part an operation is about to change, with w14
    declared (so new paragraphs can take ids); refuses a part that stayed
    bytes because its XML does not parse."""
    from formats.docx.ids import ensure_w14_namespace

    if root_of_part(part) is None:
        raise OpError(Code.BAD_VALUE, f"the document's {what} part is damaged (its XML does not parse), so its "
                      f"{what} cannot be changed", hint="Open and save the document in Word or Collabora to "
                      "repair it, then try again.")
    ensure_w14_namespace(part)
    return part.element


def _free_partname(package: Any, preferred: str) -> Any:
    from ooxml.parts import next_partname, template_of

    used = {str(p.partname) for p in package.iter_parts()}
    if preferred not in used:
        return type(next(iter(package.iter_parts())).partname)(preferred)
    return next_partname(package, template_of(preferred))


def _settings_root(doc: Any) -> Any:
    """The settings part's root, created only when the document has none
    (adding the first note needs its note properties there)."""
    return doc.settings.element


def notes_part(doc: Any, kind: str) -> Any | None:
    return first_related(doc, NOTE_KINDS[kind].reltype)


_SEPARATORS = (
    '<w:{name} xmlns:w="{ns}" w:type="separator" w:id="-1"><w:p><w:pPr><w:spacing w:after="0" '
    'w:line="240" w:lineRule="auto"/></w:pPr><w:r><w:separator/></w:r></w:p></w:{name}>',
    '<w:{name} xmlns:w="{ns}" w:type="continuationSeparator" w:id="0"><w:p><w:pPr><w:spacing w:after="0" '
    'w:line="240" w:lineRule="auto"/></w:pPr><w:r><w:continuationSeparator/></w:r></w:p></w:{name}>',
)


def ensure_notes_part(doc: Any, kind: str) -> Any:
    """The footnotes or endnotes part, created as Word writes it when the
    document has none: the separator and continuation separator notes, and
    the note properties in settings naming them."""
    from docx.oxml.parser import parse_xml

    existing = notes_part(doc, kind)
    if existing is not None:
        return existing
    spec = NOTE_KINDS[kind]
    package = doc.part.package
    root = parse_xml(f'<w:{kind}s xmlns:w="{W_NS}"/>'.encode())
    for template in _SEPARATORS:
        root.append(parse_xml(template.format(name=kind, ns=W_NS).encode()))
    part = notes_part_class()(_free_partname(package, spec.partname), spec.content_type, root, package)
    doc.part.relate_to(part, spec.reltype)
    settings = _settings_root(doc)
    if settings.find(_W + spec.properties) is None:
        from ooxml.order import insert_child

        props = parse_xml(f'<w:{spec.properties} xmlns:w="{W_NS}"><w:{kind} w:id="-1"/><w:{kind} w:id="0"/>'
                          f'</w:{spec.properties}>'.encode())
        insert_child(settings, props)
    return part


def comments_part(doc: Any) -> Any | None:
    return first_related(doc, RT_COMMENTS)


def ensure_comments_part(doc: Any) -> Any:
    """The comments part, created empty when the document has none."""
    from docx.opc.constants import CONTENT_TYPE as CT
    from docx.oxml.parser import parse_xml
    from docx.parts.comments import CommentsPart

    existing = comments_part(doc)
    if existing is not None:
        return existing
    package = doc.part.package
    element = parse_xml(f'<w:comments xmlns:w="{W_NS}"/>'.encode())
    part = CommentsPart(_free_partname(package, "/word/comments.xml"), CT.WML_COMMENTS, element, package)
    doc.part.relate_to(part, RT_COMMENTS)
    return part


_COMPANIONS = {
    RT_COMMENTS_EXTENDED: (CT_COMMENTS_EXTENDED, "/word/commentsExtended.xml",
                           f'<w15:commentsEx xmlns:w15="{W15_NS}" xmlns:mc="http://schemas.openxmlformats.org/'
                           f'markup-compatibility/2006" mc:Ignorable="w15"/>'),
}


def ensure_companion(doc: Any, reltype: str) -> Any:
    """A comment companion part (only ``commentsExtended`` is ever
    created), created empty when missing."""
    from docx.opc.part import XmlPart
    from docx.oxml.parser import parse_xml

    existing = first_related(doc, reltype)
    if existing is not None:
        return existing
    content_type, partname, xml = _COMPANIONS[reltype]
    package = doc.part.package
    part = XmlPart(_free_partname(package, partname), content_type, parse_xml(xml.encode()), package)
    doc.part.relate_to(part, reltype)
    return part


def story_parts(doc: Any) -> list[Any]:
    """Every part whose XML may hold annotations or revisions: the
    document, its headers and footers, notes, comments, styles and
    numbering."""
    parts = [doc.part]
    for reltype in (RT_HEADER, RT_FOOTER, RT_FOOTNOTES, RT_ENDNOTES, RT_COMMENTS, RT_STYLES, RT_NUMBERING):
        parts.extend(p for p in related(doc, reltype) if all(p is not q for q in parts))
    return parts


class Ids:
    """Ids for the annotations this call writes: revisions, bookmarks,
    comments and their range marks share ``w:id``. The first ``next``
    reads the largest integer ``w:id`` attribute of every story part and
    hands out from there, so the call never repeats an id and never meets
    an existing one; past 2**31 - 1 it takes the lowest unused instead."""

    def __init__(self, doc: Any) -> None:
        self._doc = doc
        self._used: set[int] | None = None
        self._top = 0

    def _scan(self) -> set[int]:
        used: set[int] = set()
        for part in story_parts(self._doc):
            root = root_of_part(part)
            if root is None:
                continue
            for el in root.iter():
                value = el.get(W_ID) if isinstance(el.tag, str) else None
                if value is not None and value.lstrip("-").isdigit():
                    used.add(int(value))
        return used

    def bump(self, value: int) -> None:
        """Count an id written by other code after the scan."""
        if self._used is None:
            self._used = self._scan()
            self._top = max(self._used, default=-1)
        self._used.add(value)
        self._top = max(self._top, value)

    def next(self) -> str:
        if self._used is None:
            self._used = self._scan()
            self._top = max(self._used, default=-1)
        if self._top < _MAX_ID:
            self._top = max(self._top + 1, 0)
            value = self._top
        else:
            value = next(n for n in range(_MAX_ID) if n not in self._used)
        self._used.add(value)
        return str(value)


def next_note_id(doc: Any, root: Any, kind: str) -> str:
    """The next id among a notes part's notes and every mark of that kind
    in the text (their own id space): a mark whose note is gone keeps its
    id, so a new note never takes it."""
    spec = NOTE_KINDS[kind]
    found = [n.get(W_ID) for n in root.iterchildren(spec.note)]
    for part in story_parts(doc):
        story = root_of_part(part)
        if story is not None:
            found.extend(mark.get(W_ID) for mark in story.iter(spec.reference))
    ids = [int(value) for value in found if (value or "").lstrip("-").isdigit()]
    return str(max([*ids, 0]) + 1)


# ---------------------------------------------------------------------------
# What a removal must keep: range marks lifted, notes and comments deleted
# ---------------------------------------------------------------------------

_PAIRS = {_W + start: _W + end for start, end in (
    ("bookmarkStart", "bookmarkEnd"), ("commentRangeStart", "commentRangeEnd"), ("permStart", "permEnd"),
    ("moveFromRangeStart", "moveFromRangeEnd"), ("moveToRangeStart", "moveToRangeEnd"),
)}
MARK_STARTS = frozenset(_PAIRS)
MARK_ENDS = frozenset(_PAIRS.values())
_PARTNER = {**_PAIRS, **{end: start for start, end in _PAIRS.items()}}
REFERENCES = {_W + "footnoteReference": FOOTNOTE, _W + "endnoteReference": ENDNOTE,
              _W + "commentReference": COMMENT}
# A complex field's characters, w:fldChar's w:fldCharType: the field's code
# runs from begin to separate, its shown value from separate to end.
FIELD_BEGIN = "begin"
FIELD_SEPARATE = "separate"
FIELD_END = "end"
# The run that starts a note's or comment's first paragraph: its own number
# or mark, never text.
OWN_MARKS = frozenset(_W + tag for tag in ("footnoteRef", "endnoteRef", "annotationRef"))
_STORIES = frozenset(_W + tag for tag in ("body", "hdr", "ftr", "footnote", "endnote", "comment"))
_W_P = _W + "p"
_W_PPR = _W + "pPr"


def _story_of(element: Any) -> Any:
    return next((a for a in element.iterancestors() if a.tag in _STORIES), None)


def _inside(node: Any, element: Any) -> bool:
    return node is element or any(a is element for a in node.iterancestors())


def put_start(mark: Any, p: Any) -> None:
    """A range start at the beginning of ``p``'s content (after its pPr)."""
    ppr = p.find(_W_PPR)
    if ppr is not None:
        ppr.addnext(mark)
    else:
        p.insert(0, mark)


def lift_marks(element: Any) -> int:
    """Range marks under ``element`` whose partner lies outside it, moved to
    the nearest paragraph that stays: a start to the beginning of the next
    one, an end to the end of the previous one, in the same story. A mark
    with nowhere to go is removed with its partner. Returns how many moved."""
    story = _story_of(element)
    if story is None:
        return 0
    # The list holds every proxy, so their ids stay theirs while it lives.
    every = list(story.iter(_W_P))
    inside = [n for n, p in enumerate(every) if _inside(p, element)] or [len(every)]
    first, last = min(inside), max(inside)
    after = next((p for n, p in enumerate(every) if n > last and not _inside(p, element)), None)
    before = next((p for n, p in reversed(list(enumerate(every))) if n < first and not _inside(p, element)), None)
    moved = 0
    for mark in [m for m in element.iter(*_PARTNER) if isinstance(m.tag, str)]:
        partner = next((m for m in story.iter(_PARTNER[mark.tag]) if m.get(W_ID) == mark.get(W_ID)), None)
        if partner is None or _inside(partner, element):
            continue
        home = after if mark.tag in MARK_STARTS else before
        if home is None:
            partner.getparent().remove(partner)
            continue
        mark.getparent().remove(mark)
        if mark.tag in MARK_STARTS:
            put_start(mark, home)
        else:
            home.append(mark)
        moved += 1
    return moved


def _referenced_outside(doc: Any, tag: str, value: str, element: Any) -> bool:
    for part in story_parts(doc):
        root = root_of_part(part)
        if root is None:
            continue
        if any(ref.get(W_ID) == value and not _inside(ref, element) for ref in root.iter(tag)):
            return True
    return False


def remove_comments(doc: Any, ids: set[str]) -> int:
    """The comments with ``ids`` and their entries in the companion parts
    (thread, durable id, date); returns how many comments went."""
    part = comments_part(doc)
    root = root_of_part(part) if part is not None else None
    if root is None or not ids:
        return 0
    from formats.docx.ids import paragraph_id

    gone_paras: set[str] = set()
    removed = 0
    for comment in [c for c in root.iterchildren(_W + "comment") if c.get(W_ID) in ids]:
        gone_paras.update(pid for pid in (paragraph_id(p) for p in comment.iter(_W_P)) if pid)
        root.remove(comment)
        removed += 1
    w15, cid, cex = f"{{{W15_NS}}}", f"{{{W16CID_NS}}}", f"{{{W16CEX_NS}}}"
    extended = first_related(doc, RT_COMMENTS_EXTENDED)
    ext_root = root_of_part(extended) if extended is not None else None
    if ext_root is not None:
        for entry in list(ext_root.iterchildren(w15 + "commentEx")):
            if (entry.get(w15 + "paraId") or "").upper() in gone_paras:
                ext_root.remove(entry)
            elif (entry.get(w15 + "paraIdParent") or "").upper() in gone_paras:
                del entry.attrib[w15 + "paraIdParent"]
    durable: set[str] = set()
    ids_part = first_related(doc, RT_COMMENTS_IDS)
    ids_root = root_of_part(ids_part) if ids_part is not None else None
    if ids_root is not None:
        for entry in list(ids_root.iterchildren(cid + "commentId")):
            if (entry.get(cid + "paraId") or "").upper() in gone_paras:
                durable.add((entry.get(cid + "durableId") or "").upper())
                ids_root.remove(entry)
    extensible = first_related(doc, RT_COMMENTS_EXTENSIBLE)
    cex_root = root_of_part(extensible) if extensible is not None else None
    if cex_root is not None and durable:
        for entry in list(cex_root.iterchildren(cex + "commentExtensible")):
            if (entry.get(cex + "durableId") or "").upper() in durable:
                cex_root.remove(entry)
    return removed


def release(doc: Any, element: Any) -> dict[str, int]:
    """Before ``element`` leaves the document: its range marks whose partner
    stays are lifted out, and the notes and comments only it refers to are
    deleted (a note's own comments with it). Returns counts by kind."""
    counts = {FOOTNOTE: 0, ENDNOTE: 0, COMMENT: 0, "marks": lift_marks(element)}
    comment_ids: set[str] = set()
    for tag, kind in REFERENCES.items():
        values = {ref.get(W_ID) for ref in element.iter(tag)}
        values = {v for v in values if v is not None and not _referenced_outside(doc, tag, v, element)}
        if not values:
            continue
        if kind == COMMENT:
            comment_ids |= values
            continue
        part = notes_part(doc, kind)
        root = root_of_part(part) if part is not None else None
        if root is None:
            continue
        for note in [n for n in root.iterchildren(NOTE_KINDS[kind].note) if n.get(W_ID) in values]:
            if not is_separator(note):
                inner = release(doc, note)
                counts[COMMENT] += inner[COMMENT]
                root.remove(note)
                counts[kind] += 1
    counts[COMMENT] += remove_comments(doc, comment_ids)
    return counts


def describe(counts: dict[str, int]) -> str:
    """'1 footnote and 2 comments' for what a removal took with it, or ''."""
    names = {FOOTNOTE: "footnote", ENDNOTE: "endnote", COMMENT: "comment"}
    parts = [f"{n} {names[k]}{'s' if n != 1 else ''}" for k, n in counts.items() if k in names and n]
    return " and ".join(parts)
