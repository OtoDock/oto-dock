"""Slide numbers as PowerPoint stores them: a slide-number placeholder on
each numbered slide holding a ``slidenum`` field, whose cached text is the
number viewers show until they evaluate the field.

python-pptx never clones date, footer or slide-number placeholders onto a new
slide, so they are added here.
"""

import copy
import uuid
from typing import Any

from ooxml import order

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS = f'xmlns:p="{_P}" xmlns:a="{_A}"'
_SLIDENUM = "slidenum"
_TITLE = "title"
_CENTER_TITLE = "ctrTitle"
_TITLE_PAIR = frozenset({_TITLE, "subTitle"})
_TYPE = "type"
_EMU_PER_INCH = 914400


def _qn(tag: str) -> str:
    from pptx.oxml.ns import qn

    return qn(tag)


def _ph(shape_el: Any) -> Any:
    return shape_el.find(f"{_qn('p:nvSpPr')}/{_qn('p:nvPr')}/{_qn('p:ph')}")


def _number_placeholders(tree: Any) -> list[Any]:
    """The top-level slide-number placeholders of a shape tree."""
    found = []
    for shape_el in tree.iterchildren(_qn("p:sp")):
        ph = _ph(shape_el)
        if ph is not None and ph.get("type") == "sldNum":
            found.append(shape_el)
    return found


def _fields(el: Any) -> list[Any]:
    return [f for f in el.iter(_qn("a:fld")) if f.get("type") == _SLIDENUM]


def first_number(prs: Any) -> int:
    try:
        return int(prs.part._element.get("firstSlideNum", "1"))
    except ValueError:
        return 1


def _holds_title_pair(el: Any) -> bool:
    kinds = {ph.get("type") for ph in el.iter(_qn("p:ph"))}
    return _CENTER_TITLE in kinds or _TITLE_PAIR <= kinds


def is_title_slide(slide: Any) -> bool:
    """A title slide: its layout is typed so, or the layout or the slide
    holds a centred title, or a title with a subtitle. LibreOffice types its
    title layout as text and points other slides at a master's first
    layout, so neither the type nor the layout alone tells."""
    layout = slide.slide_layout
    if layout._element.get(_TYPE) == _TITLE:
        return True
    return _holds_title_pair(layout.shapes._spTree) or _holds_title_pair(slide.shapes._spTree)


def has_number(slide: Any) -> bool:
    return any(_fields(sp) for sp in _number_placeholders(slide.shapes._spTree))


def follows_numbering(slides: list[Any], title: bool) -> bool:
    """Whether a new slide (a title slide or not) follows the numbering of
    ``slides``: slides of its own kind decide when the deck has any, so a
    deck numbered without its title slides keeps them apart; otherwise any
    numbered slide does."""
    same = [s for s in slides if is_title_slide(s) == title]
    return any(has_number(s) for s in (same or slides))


def _field_paragraph(number: int, ppr: str = "", rpr: str = "<a:rPr/>") -> Any:
    from pptx.oxml import parse_xml

    guid = "{" + str(uuid.uuid4()).upper() + "}"
    return parse_xml(f'<a:p {_NS}>{ppr}<a:fld id="{guid}" type="{_SLIDENUM}">{rpr}'
                     f"<a:t>{number}</a:t></a:fld></a:p>")


def _fill(shape_el: Any, number: int) -> None:
    """Put the field into a slide-number placeholder that holds no text."""
    from pptx.oxml import parse_xml

    body = shape_el.find(_qn("p:txBody"))
    if body is None:
        body = parse_xml(f"<p:txBody {_NS}><a:bodyPr/><a:lstStyle/></p:txBody>")
        order.insert_child(shape_el, body)
    for p in body.findall(_qn("a:p")):
        body.remove(p)
    body.append(_field_paragraph(number))


def _is_empty(shape_el: Any) -> bool:
    body = shape_el.find(_qn("p:txBody"))
    if body is None:
        return True
    return not any((t.text or "").strip() for t in body.iter(_qn("a:t"))) and not _fields(body)


def _free_idx(*trees: Any) -> int:
    used = [int(ph.get("idx", "0")) for tree in trees for ph in tree.iter(_qn("p:ph"))
            if (ph.get("idx") or "0").isdigit()]
    return max([12, *used]) + 1


def _orphan(slide: Any, number: int, slide_cx: int, slide_cy: int) -> Any:
    """A slide-number placeholder for a slide whose layout has none: slide
    placeholders link to the layout by idx, so it takes an idx nothing uses
    and carries the master's box and text style itself."""
    from pptx.oxml import parse_xml

    layout = slide.slide_layout
    master_ph = next(iter(_number_placeholders(layout.slide_master.shapes._spTree)), None)
    shape_id = slide.shapes._next_shape_id
    idx = _free_idx(layout.shapes._spTree, slide.shapes._spTree)
    xfrm = master_ph.find(f"{_qn('p:spPr')}/{_qn('a:xfrm')}") if master_ph is not None else None
    if xfrm is None:
        w, h, gap = int(1.5 * _EMU_PER_INCH), int(0.4 * _EMU_PER_INCH), int(0.5 * _EMU_PER_INCH)
        xfrm_xml = (f'<a:xfrm><a:off x="{slide_cx - gap - w}" y="{slide_cy - gap - h}"/>'
                    f'<a:ext cx="{w}" cy="{h}"/></a:xfrm>')
    else:
        xfrm_xml = ""
    shape_el = parse_xml(
        f'<p:sp {_NS}><p:nvSpPr><p:cNvPr id="{shape_id}" name="Slide Number Placeholder {shape_id - 1}"/>'
        f'<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="sldNum" sz="quarter" idx="{idx}"/>'
        f"</p:nvPr></p:nvSpPr><p:spPr>{xfrm_xml}</p:spPr>"
        f"<p:txBody><a:bodyPr/><a:lstStyle/></p:txBody></p:sp>")
    body = shape_el.find(_qn("p:txBody"))
    if master_ph is not None:
        if xfrm is not None:
            shape_el.find(_qn("p:spPr")).append(copy.deepcopy(xfrm))
        master_body = master_ph.find(_qn("p:txBody"))
        for tag in ("a:bodyPr", "a:lstStyle"):
            source = master_body.find(_qn(tag)) if master_body is not None else None
            if source is not None:
                body.replace(body.find(_qn(tag)), copy.deepcopy(source))
        body.append(_field_paragraph(number))
    else:
        body.append(_field_paragraph(number, '<a:pPr algn="r"/>', '<a:rPr sz="1200"/>'))
    return shape_el


def number_slide(prs: Any, slide: Any, number: int) -> None:
    """Show ``number`` on ``slide`` as a live slide-number field. A
    slide-number placeholder already holding a field or text the person
    wrote is left as it is."""
    existing = _number_placeholders(slide.shapes._spTree)
    if existing:
        for shape_el in existing:
            if _is_empty(shape_el):
                _fill(shape_el, number)
        return
    from pptx.enum.shapes import PP_PLACEHOLDER

    layout_ph = next((ph for ph in slide.slide_layout.placeholders
                      if ph.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER), None)
    tree = slide.shapes._spTree
    if layout_ph is not None:
        shape_id = slide.shapes._next_shape_id
        slide.shapes.clone_placeholder(layout_ph)
        shape_el = next(sp for sp in tree.iterchildren(_qn("p:sp"))
                        if sp.find(f"{_qn('p:nvSpPr')}/{_qn('p:cNvPr')}").get("id") == str(shape_id))
        _fill(shape_el, number)
    else:
        tree.insert_element_before(_orphan(slide, number, prs.slide_width, prs.slide_height),
                                   "p:extLst")
    for master in prs.slide_masters:
        hf = master._element.find(_qn("p:hf"))
        if hf is not None and hf.get("sldNum") == "0":
            del hf.attrib["sldNum"]


def unnumber_slide(slide: Any) -> int:
    """Remove the slide's slide-number placeholders; the count removed."""
    found = _number_placeholders(slide.shapes._spTree)
    for shape_el in found:
        shape_el.getparent().remove(shape_el)
    return len(found)


def refresh_numbers(prs: Any) -> None:
    """Rewrite the cached text of slide-number fields that no longer match
    their slide's position. A value already right is not written."""
    first = first_number(prs)
    for position, slide in enumerate(prs.slides):
        expected = str(first + position)
        for field in _fields(slide.shapes._spTree):
            t = field.find(_qn("a:t"))
            if t is None:
                from lxml import etree

                t = etree.SubElement(field, _qn("a:t"))
            if t.text != expected:
                t.text = expected
