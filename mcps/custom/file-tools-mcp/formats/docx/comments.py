"""Comments: on a paragraph or on words in it, replies, resolving and
deleting.

A comment covers its range between ``w:commentRangeStart`` and
``w:commentRangeEnd`` and is anchored by a ``w:commentReference`` run after
the end; its text is a ``w:comment`` in the comments part, starting with
its own mark run (``w:annotationRef``). A reply is a comment of its own
whose ``w15:commentEx`` (in ``commentsExtended``) names the thread's first
comment by the paragraph id of that comment's last paragraph; resolving
sets ``w15:done``. Comment ids, comment paragraph ids and durable ids are
never renumbered: the threads are linked through them.
"""

import random
from datetime import UTC, datetime
from typing import Any

from catalog import KeySpec, OpSpec
from formats.docx import address as addr, annotations
from formats.docx.ids import ensure_w14_namespace, paragraph_id, stamp
from formats.docx.notes import body_paragraph, find_text
from formats.docx.opkit import AT, RUN_KEYS, RUNS, fill_runs
from formats.docx.replace import _check_storable
from formats.docx.revisions import (
    ANONYMOUS_AUTHOR,
    isolate,
    removes_personal_information,
)
from formats.docx.styles import CHARACTER, ensure
from formats.docx.targets import PartParent, locate
from results import Code, NoOp, OpCtx, OpError

W_NS = annotations.W_NS
_W = f"{{{W_NS}}}"
_W_P = _W + "p"
_W_ID = _W + "id"
_W15 = f"{{{annotations.W15_NS}}}"
_CID = f"{{{annotations.W16CID_NS}}}"
_CEX = f"{{{annotations.W16CEX_NS}}}"
DEFAULT_AUTHOR = "OtoDock"
_MAX_DURABLE = 0x7FFFFFFE


def _w(tag: str) -> str:
    return _W + tag


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _initials(author: str) -> str:
    letters = [word[0] for word in author.split() if word[:1].isalnum()]
    return "".join(letters[:3]).upper() or author[:2].upper()


def _person(op: dict) -> tuple[str, str]:
    author = op.get("author") or DEFAULT_AUTHOR
    initials = op.get("initials") or _initials(author)
    for value, what in ((author, "The author"), (initials, "The initials")):
        try:
            _check_storable(value, what)
        except ValueError as exc:
            raise OpError(Code.BAD_VALUE, str(exc).rstrip("."), hint="Give it as plain text.") from None
    return author, initials


def _companion_root(doc: Any, reltype: str) -> Any | None:
    part = annotations.first_related(doc, reltype)
    return annotations.root_of_part(part) if part is not None else None


def _durable_id(doc: Any) -> str:
    taken = set()
    for reltype, attr in ((annotations.RT_COMMENTS_IDS, _CID + "durableId"),
                          (annotations.RT_COMMENTS_EXTENSIBLE, _CEX + "durableId")):
        root = _companion_root(doc, reltype)
        if root is not None:
            taken.update((el.get(attr) or "").upper() for el in root.iter() if isinstance(el.tag, str))
    rng = random.Random()
    while True:
        value = f"{rng.randint(1, _MAX_DURABLE):08X}"
        if value not in taken:
            return value


def _register(job: Any, comment: Any, parent_para: str | None = None) -> str:
    """The new comment's paragraph ids, and its entries in the companion
    parts the document has (``commentsExtended`` is made for a reply)."""
    para = ""
    for p in comment.iter(_W_P):
        para = stamp(p, job.alloc)
    doc = job.doc
    extended = _companion_root(doc, annotations.RT_COMMENTS_EXTENDED)
    if extended is None and parent_para is not None:
        part = annotations.ensure_companion(doc, annotations.RT_COMMENTS_EXTENDED)
        job.book.touch(part)
        extended = part.element
    if extended is not None:
        entry = extended.makeelement(_W15 + "commentEx", nsmap={"w15": annotations.W15_NS})
        entry.set(_W15 + "paraId", para)
        if parent_para is not None:
            entry.set(_W15 + "paraIdParent", parent_para)
        entry.set(_W15 + "done", "0")
        extended.append(entry)
    ids_root = _companion_root(doc, annotations.RT_COMMENTS_IDS)
    extensible = _companion_root(doc, annotations.RT_COMMENTS_EXTENSIBLE)
    if ids_root is not None or extensible is not None:
        durable = _durable_id(doc)
        if ids_root is not None:
            entry = ids_root.makeelement(_CID + "commentId", nsmap={"w16cid": annotations.W16CID_NS})
            entry.set(_CID + "paraId", para)
            entry.set(_CID + "durableId", durable)
            ids_root.append(entry)
        if extensible is not None:
            entry = extensible.makeelement(_CEX + "commentExtensible", nsmap={"w16cex": annotations.W16CEX_NS})
            entry.set(_CEX + "durableId", durable)
            entry.set(_CEX + "dateUtc", _now())
            extensible.append(entry)
    return para


def _new_comment(job: Any, op: dict) -> tuple[Any, Any, str]:
    """(comments part, w:comment, its id): the comment written, its text
    from ``text`` or ``runs``."""
    from docx.oxml import parse_xml
    from docx.text.paragraph import Paragraph

    author, initials = _person(op)
    text_style = ensure(job.doc, "annotation text")
    mark_style = ensure(job.doc, "annotation reference", CHARACTER)
    part = annotations.ensure_comments_part(job.doc)
    root = annotations.editable_root(part, "comments")
    comment_id = job.ids.next()
    comment = parse_xml(
        f'<w:comment xmlns:w="{W_NS}" w:id="{comment_id}" w:date="{_now()}"><w:p><w:pPr>'
        f'<w:pStyle w:val="{text_style}"/></w:pPr><w:r><w:rPr><w:rStyle w:val="{mark_style}"/></w:rPr>'
        f'<w:annotationRef/></w:r></w:p></w:comment>')
    if removes_personal_information(job.doc):
        # A document that removes personal information keeps no name, date
        # or initials (the author is required: Word writes "Author").
        comment.set(_w("author"), ANONYMOUS_AUTHOR)
        del comment.attrib[_w("date")]
    else:
        comment.set(_w("author"), author)
        comment.set(_w("initials"), initials)
    root.append(comment)
    fill_runs(Paragraph(comment.find(_W_P), PartParent(part)), op.get("runs"), op)
    job.book.touch(part)
    return part, comment, comment_id


def _marks(job: Any, comment_id: str) -> tuple[Any, Any, Any]:
    from docx.oxml import parse_xml

    style = ensure(job.doc, "annotation reference", CHARACTER)
    ns = f'xmlns:w="{W_NS}"'
    return (parse_xml(f'<w:commentRangeStart {ns} w:id="{comment_id}"/>'),
            parse_xml(f'<w:commentRangeEnd {ns} w:id="{comment_id}"/>'),
            parse_xml(f'<w:r {ns}><w:rPr><w:rStyle w:val="{style}"/></w:rPr>'
                      f'<w:commentReference w:id="{comment_id}"/></w:r>'))


def _range_paragraphs(job: Any, ctx: OpCtx, op: dict) -> tuple[Any, Any]:
    """(first, last) paragraph the comment covers: the target paragraph, or
    a table's, row's or cell's first and last."""
    if op.get("target") is None:
        _, p = body_paragraph(job, ctx, op, "a comment")
        return p, p
    address = addr.parse(op["target"])
    if address.segments and address.segments[-1][0] in (addr.TABLE, addr.ROW, addr.CELL):
        located = locate(job.book, ctx, op, kinds=(addr.TABLE, addr.ROW, addr.CELL))
        if located.part is not job.doc.part:
            raise OpError(Code.UNSUPPORTED, "a comment covers text of the body only",
                          location=f"target {op['target']}", hint="Target a table in the body.")
        job.book.touch(located.part)
        paragraphs = list(located.element.iter(_W_P))
        return paragraphs[0], paragraphs[-1]
    _, p = body_paragraph(job, ctx, op, "a comment")
    return p, p


def _op_add_comment(job: Any, ctx: OpCtx, op: dict) -> None:
    first, last = _range_paragraphs(job, ctx, op)
    span = None
    if op.get("on_text") is not None:
        if first is not last:
            raise OpError(Code.BAD_VALUE, "on_text needs a paragraph target", hint="Target the paragraph.")
        flat, start, end = find_text(first, op["on_text"], "on_text")
        span = (flat, start, end)
    _, comment, comment_id = _new_comment(job, op)
    start_mark, end_mark, reference = _marks(job, comment_id)
    if span is not None:
        flat, start, end = span
        runs = isolate(flat, start, end, job.ids)
        runs[0].addprevious(start_mark)
        runs[-1].addnext(end_mark)
    else:
        annotations.put_start(start_mark, first)
        last.append(end_mark)
    end_mark.addnext(reference)
    _register(job, comment)
    job.made_annotations.append((ctx.index, addr.COMMENT, comment, "", None))


def _comment_of(job: Any, ctx: OpCtx, op: dict) -> tuple[Any, Any]:
    """(comments part, w:comment) the target names: ``comment<N>`` or one of
    its paragraphs' ids."""
    located = locate(job.book, ctx, op, kinds=(addr.PART, addr.PARAGRAPH))
    comment = next((el for el in [located.element, *located.element.iterancestors()]
                    if el.tag == _w("comment")), None)
    if comment is None:
        raise OpError(Code.BAD_VALUE, f"target {op['target']} is not a comment", location=f"target {op['target']}",
                      hint="Give comment1, comment2, ... as the read lists them.")
    return located.part, comment


def _last_para_id(job: Any, comment: Any) -> str:
    """The paragraph id of the comment's last paragraph (what thread
    entries name), stamped now when it has none."""
    paragraphs = comment.findall(_W_P)
    last = paragraphs[-1]
    value = paragraph_id(last)
    if value is None:
        part = annotations.comments_part(job.doc)
        ensure_w14_namespace(part)
        value = stamp(last, job.alloc)
        job.book.touch(part)
    return value


def _entries(doc: Any) -> dict[str, Any]:
    root = _companion_root(doc, annotations.RT_COMMENTS_EXTENDED)
    if root is None:
        return {}
    return {(e.get(_W15 + "paraId") or "").upper(): e for e in root.iterchildren(_W15 + "commentEx")}


def _key(comment: Any) -> str:
    """The paragraph id thread entries name a comment by: its last
    paragraph's."""
    paragraphs = comment.findall(_W_P)
    return (paragraph_id(paragraphs[-1]) or "") if paragraphs else ""


def _thread(job: Any, comment: Any) -> tuple[Any, list[Any]]:
    """(the thread's first comment, its replies in comment order)."""
    entries = _entries(job.doc)
    comments = list(comment.getparent().iterchildren(_w("comment")))
    by_key = {_key(c): c for c in comments if _key(c)}

    def parent_of(c: Any) -> str:
        entry = entries.get(_key(c))
        return (entry.get(_W15 + "paraIdParent") or "").upper() if entry is not None else ""

    head, seen = comment, set()
    while parent_of(head) in by_key and parent_of(head) not in seen:
        seen.add(parent_of(head))
        head = by_key[parent_of(head)]
    head_key = _key(head)
    replies = [c for c in comments if c is not head and head_key and parent_of(c) == head_key]
    return head, replies


def _marks_of(doc: Any, comment_id: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for part in annotations.story_parts(doc):
        root = annotations.root_of_part(part)
        if root is None:
            continue
        for tag, key in ((_w("commentRangeStart"), "start"), (_w("commentRangeEnd"), "end"),
                         (_w("commentReference"), "reference")):
            for el in root.iter(tag):
                if el.get(_W_ID) == comment_id and key not in found:
                    found[key] = el if key != "reference" else el.getparent()
                    found["part"] = part
    return found


def _op_reply_to_comment(job: Any, ctx: OpCtx, op: dict) -> None:
    _, target = _comment_of(job, ctx, op)
    head, replies = _thread(job, target)
    head_para = _last_para_id(job, head)
    last = replies[-1] if replies else head
    anchor = _marks_of(job.doc, last.get(_W_ID))
    if "reference" not in anchor:
        raise OpError(Code.BAD_VALUE, "the comment has no place in the text to reply at (its mark is gone)",
                      location=f"target {op['target']}", hint="Add a new comment instead.")
    _, comment, comment_id = _new_comment(job, op)
    start_mark, end_mark, reference = _marks(job, comment_id)
    if "start" in anchor:
        anchor["start"].addnext(start_mark)
    if "end" in anchor:
        anchor["end"].addnext(end_mark)
    anchor["reference"].addnext(reference)
    job.book.touch(anchor["part"])
    entries = _entries(job.doc)
    if head_para not in entries:
        part = annotations.ensure_companion(job.doc, annotations.RT_COMMENTS_EXTENDED)
        job.book.touch(part)
        entry = part.element.makeelement(_W15 + "commentEx", nsmap={"w15": annotations.W15_NS})
        entry.set(_W15 + "paraId", head_para)
        entry.set(_W15 + "done", "0")
        part.element.append(entry)
    _register(job, comment, parent_para=head_para)
    job.made_annotations.append((ctx.index, addr.COMMENT, comment, "", head))


def _op_resolve_comment(job: Any, ctx: OpCtx, op: dict) -> None:
    _, target = _comment_of(job, ctx, op)
    head, replies = _thread(job, target)
    done = "1" if op.get("resolved", True) else "0"
    part = annotations.ensure_companion(job.doc, annotations.RT_COMMENTS_EXTENDED)
    entries = _entries(job.doc)
    changed = 0
    for comment in [head, *replies]:
        pid = _last_para_id(job, comment)
        entry = entries.get(pid)
        if entry is None:
            entry = part.element.makeelement(_W15 + "commentEx", nsmap={"w15": annotations.W15_NS})
            entry.set(_W15 + "paraId", pid)
            part.element.append(entry)
        if entry.get(_W15 + "done") != done:
            entry.set(_W15 + "done", done)
            changed += 1
    if not changed:
        raise NoOp(f"the thread is already {'resolved' if done == '1' else 'open'}")
    job.book.touch(part)
    ctx.note(f"the thread of {1 + len(replies)} comment{'s' if replies else ''} is "
             f"{'resolved' if done == '1' else 'open again'}")


def _op_delete_comment(job: Any, ctx: OpCtx, op: dict) -> None:
    part, target = _comment_of(job, ctx, op)
    head, replies = _thread(job, target)
    going = [head, *replies] if target is head else [target]
    ids = {c.get(_W_ID) for c in going}
    for story in annotations.story_parts(job.doc):
        root = annotations.root_of_part(story)
        if root is None:
            continue
        for el in list(root.iter(_w("commentRangeStart"), _w("commentRangeEnd"), _w("commentReference"))):
            if el.get(_W_ID) not in ids:
                continue
            node = el.getparent() if el.tag == _w("commentReference") else el
            holder = node.getparent()
            holder.remove(node)
            job.book.touch(story)
            if holder.tag in (_w("ins"), _w("del")) and not len(holder):
                holder.getparent().remove(holder)
    removed = annotations.remove_comments(job.doc, ids)
    job.book.touch(part)
    for reltype in (annotations.RT_COMMENTS_EXTENDED, annotations.RT_COMMENTS_IDS,
                    annotations.RT_COMMENTS_EXTENSIBLE):
        companion = annotations.first_related(job.doc, reltype)
        if companion is not None and annotations.root_of_part(companion) is not None:
            job.book.touch(companion)
    ctx.note(f"deleted {removed} comment{'s' if removed != 1 else ''}" +
             (" (the comment and its replies)" if len(going) > 1 else ""))


_PERSON = (KeySpec("author", ("str",), help="default OtoDock"),
           KeySpec("initials", ("str",), help="default the author's initials"))
_TEXT = (KeySpec("text", ("str",), default=""), RUNS, *RUN_KEYS)


def _ops() -> list[OpSpec]:
    paragraph = ({"op": "add_paragraph", "text": "Payment is due within thirty (30) days."},)
    commented = (*paragraph, {"op": "add_comment", "text": "Our standard is 45 days.", "author": "Legal"})
    return [
        OpSpec("add_comment", _op_add_comment, (
            *_TEXT, *_PERSON,
            KeySpec("on_text", ("str",), help="the comment covers these words; default the whole paragraph"),
            *AT,
        ), "REVIEW", note="target a body paragraph (default the last), a table, a row or a cell",
            setup=paragraph,
            examples=({"op": "add_comment", "target": "p0", "on_text": "thirty (30) days",
                       "text": "Our standard is 45 days.", "author": "Legal"},
                      {"op": "add_comment", "runs": [{"text": "Check", "bold": True}, {"text": " this."}]})),
        OpSpec("reply_to_comment", _op_reply_to_comment, (*_TEXT, *_PERSON, *AT), "REVIEW",
               setup=commented, note="a reply joins the thread of the comment it answers",
               examples=({"op": "reply_to_comment", "target": "comment1", "text": "Agreed, changed.",
                          "author": "Sales"},)),
        OpSpec("resolve_comment", _op_resolve_comment, (
            KeySpec("resolved", ("bool",), default=True, help="false opens it again"), *AT,
        ), "REVIEW", setup=commented, note="marks the whole thread done",
            examples=({"op": "resolve_comment", "target": "comment1"},)),
        OpSpec("delete_comment", _op_delete_comment, AT, "REVIEW", setup=commented,
               note="a thread's first comment goes with its replies; a reply goes alone",
               examples=({"op": "delete_comment", "target": "comment1"},)),
    ]


OPS = _ops()
