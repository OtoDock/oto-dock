"""Footnotes and endnotes: add one at a place in a paragraph, delete one.

A note's reference is a run in the body styled as a superscript number; the
note itself is a ``w:footnote`` (``w:endnote``) in its own part whose first
paragraph starts with its own number run (``w:footnoteRef``) and a space,
as Word writes it. A note's text is edited with the paragraph operations on
its address (``footnote2/p0``).
"""

from typing import Any

from catalog import KeySpec, OpSpec
from formats.docx import address as addr, annotations
from formats.docx.opkit import AT, RUN_KEYS, RUNS, TRACKED, fill_runs, revision_for
from formats.docx.replace import _Flat
from formats.docx.revisions import delete_runs, split_at, track_new_paragraph, wrap
from formats.docx.styles import CHARACTER, ensure
from formats.docx.targets import PartParent, locate
from results import Code, NoOp, OpCtx, OpError

W_NS = annotations.W_NS
_W = f"{{{W_NS}}}"
_W_R = _W + "r"
_W_P = _W + "p"
_W_ID = _W + "id"
# A place for a note or a comment's mark must be ordinary text: never a
# field's result, a link's text, moved text or a content control filled
# from the document's properties.
_REFUSED_HOLDERS = {_W + "hyperlink": "a link", _W + "fldSimple": "a field's result", _W + "moveTo": "moved text",
                    _W + "del": "deleted text"}


def _w(tag: str) -> str:
    return _W + tag


def body_paragraph(job: Any, ctx: OpCtx, op: dict, what: str) -> tuple[Any, Any]:
    """(part, w:p) an annotation goes in: the target paragraph, else the
    body's last paragraph. Only the body takes notes and comments' marks
    here, never a header, footer, note or comment."""
    if op.get("target") is None:
        body = job.doc.element.body
        paragraphs = body.findall(_W_P)
        if not paragraphs:
            raise OpError(Code.NOT_FOUND, "the document has no paragraph", hint="Add a paragraph first.")
        job.book.touch(job.doc.part)
        return job.doc.part, paragraphs[-1]
    located = locate(job.book, ctx, op, kinds=(addr.PARAGRAPH,))
    story = next((a for a in located.element.iterancestors() if a.tag in (
        _w("body"), _w("hdr"), _w("ftr"), _w("footnote"), _w("endnote"), _w("comment"))), None)
    if story is None or story.tag != _w("body"):
        where = {_w("hdr"): "a header", _w("ftr"): "a footer", _w("footnote"): "a footnote",
                 _w("endnote"): "an endnote", _w("comment"): "a comment"}.get(story.tag if story is not None else "",
                                                                             "this part")
        raise OpError(Code.UNSUPPORTED, f"{located.address} is in {where}, which cannot hold {what}",
                      location=f"target {op['target']}", hint=f"Put {what} in a paragraph of the body.")
    job.book.touch(located.part)
    return located.part, located.element


def find_text(p: Any, text: str, key: str, *, span: bool = True) -> tuple[_Flat, int, int]:
    """(flattened paragraph, start, end) of the first occurrence of ``text``
    in ``p``'s shown text, the read's change marks ({+ +}, {- -}) left out.
    With ``span`` the words are covered (a comment, a bookmark) and must sit
    in one stretch of plain text; without it only the place after them
    must (a note's mark)."""
    from formats.docx.read import without_marks

    flat = _Flat(p)
    wanted = without_marks(text)
    start = flat.text.find(wanted) if wanted else -1
    if start < 0:
        shown = flat.text[:80] + ("…" if len(flat.text) > 80 else "")
        raise OpError(Code.NOT_FOUND, f"{key} {text!r} is not in the paragraph", location=key,
                      hint=f"Give words as the paragraph shows them: {shown!r}.")
    end = start + len(wanted)
    if span and not flat.one_segment(start, end):
        raise OpError(Code.BAD_VALUE, f"{key} {text!r} crosses a link, field, note mark or tracked change",
                      location=key, hint="Give a shorter piece of text that sits in plain text.")
    atom = flat.atoms[flat.owner[end - 1 if not span else start]]
    holder = atom.element.getparent().getparent()
    reason = "a field's result" if atom.in_field else _REFUSED_HOLDERS.get(holder.tag)
    if reason is not None:
        what = "words" if span else "place after the words"
        raise OpError(Code.BAD_VALUE, f"{key} {text!r}: the {what} {'are' if span else 'is'} in {reason}",
                      location=key, hint="Give text from the paragraph's ordinary text.")
    return flat, start, end


def _reference_run(kind: str, note_id: str, style: str) -> Any:
    from docx.oxml import parse_xml

    return parse_xml(f'<w:r xmlns:w="{W_NS}"><w:rPr><w:rStyle w:val="{style}"/></w:rPr>'
                     f'<w:{kind}Reference w:id="{note_id}"/></w:r>')


def _add_note(job: Any, ctx: OpCtx, op: dict, kind: str) -> None:
    from docx.oxml import parse_xml
    from docx.text.paragraph import Paragraph

    spec = annotations.NOTE_KINDS[kind]
    part, p = body_paragraph(job, ctx, op, f"a {kind}")
    place = find_text(p, op["after_text"], "after_text", span=False) if op.get("after_text") is not None else None
    rev = revision_for(job, op, ctx)
    text_style = ensure(job.doc, spec.style)
    ref_style = ensure(job.doc, spec.ref_style, CHARACTER)
    notes_part = annotations.ensure_notes_part(job.doc, kind)
    root = annotations.editable_root(notes_part, f"{kind}s")
    note_id = annotations.next_note_id(job.doc, root, kind)
    note = parse_xml(
        f'<w:{kind} xmlns:w="{W_NS}" w:id="{note_id}"><w:p><w:pPr><w:pStyle w:val="{text_style}"/></w:pPr>'
        f'<w:r><w:rPr><w:rStyle w:val="{ref_style}"/></w:rPr><w:{kind}Ref/></w:r>'
        f'<w:r><w:t xml:space="preserve"> </w:t></w:r></w:p></w:{kind}>')
    root.append(note)
    note_p = note.find(_W_P)
    fill_runs(Paragraph(note_p, PartParent(notes_part)), op.get("runs"), op)
    run = _reference_run(kind, note_id, ref_style)
    if place is None:
        p.append(run)
    else:
        flat, _start, end = place
        following = split_at(flat, end, job.ids)
        if following is not None:
            following.addprevious(run)
        else:
            flat.atoms[flat.owner[end - 1]].element.getparent().addnext(run)
    if rev is not None:
        wrap([run], "ins", rev)
        track_new_paragraph(note_p, rev)
    job.book.touch(notes_part)
    job.made_annotations.append((ctx.index, kind, note, " as a tracked change" if rev is not None else "", None))


def _op_add_footnote(job: Any, ctx: OpCtx, op: dict) -> None:
    _add_note(job, ctx, op, annotations.FOOTNOTE)


def _op_add_endnote(job: Any, ctx: OpCtx, op: dict) -> None:
    _add_note(job, ctx, op, annotations.ENDNOTE)


def _note_of(job: Any, ctx: OpCtx, op: dict) -> tuple[str, Any, Any]:
    """(kind, part, note element) the target names: a note's address, or
    the id of one of its paragraphs."""
    located = locate(job.book, ctx, op, kinds=(addr.PART, addr.PARAGRAPH))
    element = located.element
    holders = [element, *element.iterancestors()]
    for kind in (annotations.FOOTNOTE, annotations.ENDNOTE):
        note = next((el for el in holders if el.tag == _w(kind)), None)
        if note is not None:
            return kind, located.part, note
    raise OpError(Code.BAD_VALUE, f"target {op['target']} is not a footnote or an endnote",
                  location=f"target {op['target']}", hint="Give footnote1, endnote2, or a note paragraph's id.")


def _op_delete_note(job: Any, ctx: OpCtx, op: dict) -> None:
    kind, part, note = _note_of(job, ctx, op)
    tag = annotations.NOTE_KINDS[kind].reference
    value = note.get(_W_ID)
    runs = []
    for story in annotations.story_parts(job.doc):
        root = annotations.root_of_part(story)
        if root is None:
            continue
        for ref in root.iter(tag):
            if ref.get(_W_ID) == value:
                runs.append(ref.getparent())
                job.book.touch(story)
    rev = revision_for(job, op, ctx)
    if rev is not None:
        done = delete_runs([r for r in runs if r.tag == _W_R], rev)
        if not done:
            raise NoOp(f"the {kind}'s reference is already deleted")
        if done.recorded:
            ctx.note(f"the {kind}'s reference is marked deleted; the {kind} goes when the change is accepted")
        else:
            ctx.note(f"deleted the {kind}: it was this author's own tracked insertion")
        return
    for ref_run in runs:
        holder = ref_run.getparent()
        holder.remove(ref_run)
        if holder.tag in (_w("ins"), _w("del")) and not len(holder):
            holder.getparent().remove(holder)
    annotations.release(job.doc, note)
    note.getparent().remove(note)
    job.book.touch(part)
    ctx.note(f"deleted the {kind} and its reference")


_NOTE_KEYS = (
    KeySpec("text", ("str",), default=""), RUNS, *RUN_KEYS,
    KeySpec("after_text", ("str",), help="the mark goes right after these words of the paragraph; default its end"),
    *AT, *TRACKED,
)


def _ops() -> list[OpSpec]:
    paragraph = ({"op": "add_paragraph", "text": "Revenue grew 9% in the quarter."},)
    with_note = (*paragraph, {"op": "add_footnote", "text": "Audited figures."})
    return [
        OpSpec("add_footnote", _op_add_footnote, _NOTE_KEYS, "REVIEW",
               note="target a body paragraph (default the last); the note's text is footnote1/p0 in the read",
               setup=paragraph,
               examples=({"op": "add_footnote", "target": "p0", "after_text": "9%",
                          "text": "Source: the audited accounts."},
                         {"op": "add_footnote", "runs": [{"text": "See "}, {"text": "Annex B", "italic": True}],
                          "track": True})),
        OpSpec("add_endnote", _op_add_endnote, _NOTE_KEYS, "REVIEW", setup=paragraph,
               examples=({"op": "add_endnote", "target": "p0", "text": "Method: see the appendix."},)),
        OpSpec("delete_note", _op_delete_note, (*AT, *TRACKED), "REVIEW", setup=with_note,
               note="a footnote or endnote and its reference",
               examples=({"op": "delete_note", "target": "footnote1"},
                         {"op": "delete_note", "target": "footnote1", "track": True})),
    ]


OPS = _ops()
