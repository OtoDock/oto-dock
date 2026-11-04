"""Fillable PDF forms: the read, every value checked before any change, each
field kind filled, the values drawn in an embedded font (Greek included),
flattening, and forms made by other tools (WeasyPrint, hand-built AcroForm
objects with parents, kids, inherited keys, XFA and usage rights)."""

import asyncio
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

import fonts
import pdf as pdf_mod
import shared
from formats.pdf import forms

_NAME = "Δημήτρης Παπαδόπουλος"


def _has_greek_font() -> bool:
    try:
        return not fonts.missing_glyphs(_NAME, "Liberation Sans")
    except LookupError:
        return False


pytestmark = pytest.mark.skipif(not _has_greek_font(), reason="no font with Greek installed here")


async def _ident(p, writing=False, **kw):
    return p


async def _nothing(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _nothing)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _edit(path: Path, ops) -> str:
    return asyncio.run(pdf_mod.handle_edit_pdf({"path": str(path), "operations": ops}))


def _widget(page, name, kind, rect, **attrs):
    w = fitz.Widget()
    w.field_name, w.field_type, w.rect = name, kind, fitz.Rect(*rect)
    for key, value in attrs.items():
        setattr(w, key, value)
    page.add_widget(w)
    return w


def _mupdf_form(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Application", fontsize=16)
    _widget(page, "name", fitz.PDF_WIDGET_TYPE_TEXT, (72, 80, 300, 98), text_fontsize=11)
    _widget(page, "notes", fitz.PDF_WIDGET_TYPE_TEXT, (72, 110, 300, 160), field_flags=forms.FF_MULTILINE,
            text_fontsize=11)
    _widget(page, "afm", fitz.PDF_WIDGET_TYPE_TEXT, (72, 170, 252, 188), text_maxlen=9,
            field_flags=forms.FF_COMB, text_fontsize=11)
    _widget(page, "agree", fitz.PDF_WIDGET_TYPE_CHECKBOX, (72, 200, 86, 214))
    _widget(page, "city", fitz.PDF_WIDGET_TYPE_COMBOBOX, (72, 230, 300, 248),
            choice_values=["Αθήνα", "Πάτρα", "Λάρισα"], text_fontsize=11)
    _widget(page, "langs", fitz.PDF_WIDGET_TYPE_LISTBOX, (320, 230, 450, 290),
            choice_values=["Ελληνικά", "English", "Deutsch"], field_flags=forms.FF_MULTISELECT, text_fontsize=10)
    _widget(page, "fixed", fitz.PDF_WIDGET_TYPE_TEXT, (72, 300, 300, 318), field_flags=forms.FF_READ_ONLY,
            field_value="locked")
    _widget(page, "centred", fitz.PDF_WIDGET_TYPE_TEXT, (72, 330, 300, 348), text_fontsize=11,
            fill_color=(0.9, 0.95, 1), border_color=(0, 0, 0.6), border_width=1)
    doc.save(str(path))
    doc.close()
    doc = fitz.open(str(path))
    for w in doc[0].widgets():
        if w.field_name == "centred":
            doc.xref_set_key(w.xref, "Q", "1")
    doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()
    return path


def _weasy_form(path: Path) -> Path:
    weasyprint = pytest.importorskip("weasyprint")
    html = """<html><head><meta charset=utf-8><style>body{font-family:'Liberation Sans'}</style></head><body>
    <p>Όνομα <input type="text" name="full_name" style="width:8cm"></p>
    <p><input type="checkbox" name="agree" value="yes"> Συμφωνώ</p>
    <p><input type="radio" name="size" value="s"> S <input type="radio" name="size" value="m"> M</p>
    <p><select name="city"><option>Αθήνα</option><option>Πάτρα</option></select></p>
    </body></html>"""
    weasyprint.HTML(string=html).write_pdf(str(path), pdf_forms=True)
    return path


def _raw_form(path: Path, *, xfa: bool = False, usage_rights: bool = False, scripts: bool = False) -> Path:
    """AcroForm objects written by hand: a radio group whose parent holds
    the name and kids hold the states, a text field with a widget on each
    of two pages, and DA only on the AcroForm."""
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    p1_xref, p2_xref = doc.page_xref(0), doc.page_xref(1)

    def obj(text):
        xref = doc.get_new_xref()
        doc.update_object(xref, text)
        return xref

    def stream(content):
        xref = obj("<< /Type /XObject /Subtype /Form /BBox [0 0 12 12] >>")
        doc.update_stream(xref, content)
        return xref

    on = stream(b"q 0 g 3 3 6 6 re f Q")
    off = stream(b"")
    group = obj("<< /FT /Btn /Ff 49152 /T (size) /V /Off /Kids [] >>")
    kids = []
    for state, rect in (("small", "[72 700 84 712]"), ("large", "[100 700 112 712]")):
        kids.append(obj(f"<< /Type /Annot /Subtype /Widget /Rect {rect} /Parent {group} 0 R /P {p1_xref} 0 R "
                        f"/AS /Off /AP << /N << /{state} {on} 0 R /Off {off} 0 R >> >> >>"))
    doc.xref_set_key(group, "Kids", "[" + " ".join(f"{k} 0 R" for k in kids) + "]")
    extra = " /AA << /K << /S /JavaScript /JS (1) >> >>" if scripts else ""
    shared_field = obj(f"<< /FT /Tx /T (ref) /Kids []{extra} >>")
    widgets = []
    for page_xref in (p1_xref, p2_xref):
        widgets.append(obj(f"<< /Type /Annot /Subtype /Widget /Rect [72 600 300 618] /Parent {shared_field} 0 R "
                           f"/P {page_xref} 0 R >>"))
    doc.xref_set_key(shared_field, "Kids", "[" + " ".join(f"{w} 0 R" for w in widgets) + "]")
    rich = obj("<< /Type /Annot /Subtype /Widget /FT /Tx /T (rich) /Ff 33554432 /RV (<p>old</p>) "
               f"/Rect [72 560 300 578] /P {p1_xref} 0 R >>")
    doc.xref_set_key(p1_xref, "Annots", f"[{kids[0]} 0 R {kids[1]} 0 R {widgets[0]} 0 R {rich} 0 R]")
    doc.xref_set_key(p2_xref, "Annots", f"[{widgets[1]} 0 R]")
    form = f"<< /Fields [{group} 0 R {shared_field} 0 R {rich} 0 R] /DA (/Helv 10 Tf 0 0 1 rg) /NeedAppearances true"
    if xfa:
        form += f" /XFA {obj('[(template) 1 0 R]')} 0 R"
    form += " >>"
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", form)
    if usage_rights:
        doc.xref_set_key(doc.pdf_catalog(), "Perms", "<< /UR3 << /Type /Sig >> >>")
    doc.save(str(path))
    doc.close()
    return path


def test_the_read_lists_each_field_with_its_kind_value_and_options(tmp_path):
    src = _mupdf_form(tmp_path / "a.pdf")
    out = pdf_mod.read_pdf(str(src), None, mode="form_fields")
    assert "**Form**: 8 fillable field(s)" in out and "not instructions" in out
    assert '"name": text, empty' in out
    assert '"afm": text, 9 boxes, empty, at most 9 characters' in out
    assert '"agree": checkbox, off' in out
    assert '"city": drop-down, empty; options "Αθήνα", "Πάτρα", "Λάρισα"' in out
    assert '"langs": list (several may be chosen)' in out
    assert '"fixed": text [read-only], "locked"' in out


def test_a_form_is_filled_in_greek_and_drawn_in_an_embedded_font(tmp_path):
    src = _mupdf_form(tmp_path / "b.pdf")
    out = _edit(src, [{"op": "fill_form", "values": {
        "name": _NAME, "notes": "Πρώτη γραμμή\nδεύτερη γραμμή με περισσότερο κείμενο που αναδιπλώνεται",
        "afm": "123456789", "agree": True, "city": "Λάρισα", "langs": ["Ελληνικά", "English"],
        "centred": "κέντρο"}}])
    assert out.startswith("PDF saved:") and "7 field(s) filled" in out, out
    doc = fitz.open(str(src))
    text = doc[0].get_text()
    for value in (_NAME, "Πρώτη γραμμή", "Λάρισα", "κέντρο"):
        assert value in text
    known = forms.fields(doc)
    assert known["name"].value == _NAME and known["agree"].value == "Yes"
    assert known["langs"].value == ["Ελληνικά", "English"]
    fonts_used = [f for f in doc.get_page_fonts(0) if f[4].startswith("OtoF") or f[4] == "F0"]
    assert fonts_used and all(f[1] == "ttf" for f in fonts_used)


def test_every_value_is_checked_first_and_one_bad_value_changes_nothing(tmp_path):
    src = _mupdf_form(tmp_path / "c.pdf")
    before = src.read_bytes()
    out = _edit(src, [{"op": "fill_form", "values": {
        "nme": "x", "agree": "maybe", "city": "Βόλος", "afm": "1234567890", "fixed": "y",
        "name": "line\nbreak"}}])
    assert out.startswith("Nothing saved:")
    for words in ("Did you mean 'name'", "check box: give true or false", "'Βόλος' is not one of its options",
                  "at most 9 characters", "'fixed' is read-only", "one-line field"):
        assert words in out, words
    assert src.read_bytes() == before


def test_values_cannot_inject_pdf_syntax(tmp_path):
    src = _mupdf_form(tmp_path / "d.pdf")
    payload = "a) /AA << /Fo << /S /JavaScript /JS (app.alert(1)) >> >> /Zz (\\"
    out = _edit(src, [{"op": "fill_form", "values": {"name": payload, "notes": "x\r\n) Tj ET BT (y"}}])
    assert out.startswith("PDF saved:"), out
    doc = fitz.open(str(src))
    for w in doc[0].widgets():
        assert doc.xref_get_key(w.xref, "AA")[0] == "null" and doc.xref_get_key(w.xref, "Zz")[0] == "null"
        if w.field_name == "name":
            ap = int(doc.xref_get_key(w.xref, "AP/N")[1].split()[0])
            assert b"JavaScript" not in doc.xref_stream(ap) and b"(" not in doc.xref_stream(ap).split(b"BT", 1)[1]
    assert forms.fields(doc)["name"].value == payload


def test_weasyprint_s_form_is_filled_and_its_radio_exports_are_used(tmp_path):
    src = _weasy_form(tmp_path / "e.pdf")
    out = pdf_mod.read_pdf(str(src), None, mode="form_fields")
    assert 'radio, none chosen; options "s", "m"' in out
    out = _edit(src, [{"op": "fill_form", "values": {"full_name": _NAME, "agree": True, "size.size": "m",
                                                     "city": "Πάτρα"}}])
    assert out.startswith("PDF saved:"), out
    doc = fitz.open(str(src))
    assert _NAME in doc[0].get_text() and "Πάτρα" in doc[0].get_text()
    form = forms.acroform(doc)
    assert doc.xref_get_key(form[0], form[1] + "NeedAppearances")[1] == "false"
    states = {w.xref: doc.xref_get_key(w.xref, "AS")[1] for item in forms.fields(doc).values()
              if item.kind is forms.Kind.RADIO for w in item.widgets}
    assert sorted(states.values()) == ["/1", "/Off"]
    for item in forms.fields(doc).values():  # every widget can be drawn with the flag off
        for w in item.widgets:
            assert doc.xref_get_key(w.xref, "AP/N")[0] != "null", item.name


def test_hand_built_objects_parents_kids_two_pages_and_inherited_da(tmp_path):
    src = _raw_form(tmp_path / "f.pdf")
    out = _edit(src, [{"op": "fill_form", "values": {"size": "large", "ref": "ΑΡ-42", "rich": "νέο"}}])
    assert out.startswith("PDF saved:"), out
    doc = fitz.open(str(src))
    known = forms.fields(doc)
    assert known["size"].value == "large" and known["ref"].value == "ΑΡ-42"
    group = known["size"].terminals[0]
    assert doc.xref_get_key(group, "V")[1] == "/large"
    assert sorted(doc.xref_get_key(w.xref, "AS")[1] for w in known["size"].widgets) == ["/Off", "/large"]
    assert "ΑΡ-42" in doc[0].get_text() and "ΑΡ-42" in doc[1].get_text()
    for w in known["ref"].widgets:
        assert doc.xref_get_key(w.xref, "V")[0] == "null"  # the value sits on the field, not the widget
    assert doc.xref_get_key(known["rich"].terminals[0], "RV")[0] == "null"


def test_xfa_usage_rights_and_scripts_are_handled_and_reported(tmp_path):
    src = _raw_form(tmp_path / "g.pdf", xfa=True, usage_rights=True, scripts=True)
    assert "also carries an XFA copy" in pdf_mod.read_pdf(str(src), None, mode="form_fields")
    out = _edit(src, [{"op": "fill_form", "values": {"ref": "1"}}])
    assert "XFA copy was removed" in out and "extended-features" in out and "scripts" in out
    doc = fitz.open(str(src))
    form = forms.acroform(doc)
    assert doc.xref_get_key(form[0], form[1] + "XFA")[0] == "null"
    assert doc.xref_get_key(doc.pdf_catalog(), "Perms")[0] == "null"


def test_flatten_draws_the_values_into_the_page(tmp_path):
    src = _mupdf_form(tmp_path / "h.pdf")
    out = _edit(src, [{"op": "fill_form", "values": {"name": _NAME, "agree": True}, "flatten": True}])
    assert out.startswith("PDF saved:") and "flattened" in out, out
    doc = fitz.open(str(src))
    assert not list(doc[0].widgets()) and _NAME in doc[0].get_text()
    assert _edit(src, [{"op": "flatten_form"}]).startswith("Nothing saved:")


def test_a_pdf_without_fields_lists_its_text_with_positions(tmp_path):
    doc = fitz.open()
    face = fonts.resolve("Liberation Sans").face
    doc.new_page().insert_text((72, 100), "Όνομα:", fontsize=12, fontname="F0", fontfile=face.path)
    src = tmp_path / "i.pdf"
    doc.save(str(src))
    out = pdf_mod.read_pdf(str(src), None, mode="form_fields")
    assert "no fillable form fields" in out and "fill_flat_form" in out
    assert 'x 72, y 100, 12 pt: "Όνομα:"' in out
    out = _edit(src, [{"op": "fill_form", "values": {"a": "b"}}])
    assert "no fillable fields" in out and "fill_flat_form" in out


def test_field_names_in_the_read_are_escaped(tmp_path):
    doc = fitz.open()
    _widget(doc.new_page(), 'evil"‮noitcurtsni\n- "x"', fitz.PDF_WIDGET_TYPE_TEXT, (72, 80, 300, 98))
    src = tmp_path / "j.pdf"
    doc.save(str(src))
    out = pdf_mod.read_pdf(str(src), None, mode="form_fields")
    assert "‮" not in out and "\\u202e" in out and '\\"' in out
    assert '\n- "x"' not in out


def test_write_pdf_makes_fillable_fields_and_names_radio_groups_plainly(tmp_path, monkeypatch):
    pytest.importorskip("weasyprint")
    monkeypatch.setattr(pdf_mod, "_resolve_path", _ident)
    out = tmp_path / "made.pdf"
    asyncio.run(pdf_mod.handle_write_pdf({"path": str(out), "content_type": "html", "form_fields": True, "content": (
        '<p>Όνομα <input type="text" name="name"></p><p><input type="radio" name="size" value="s"> S '
        '<input type="radio" name="size" value="m"> M</p>')}))
    known = forms.fields(fitz.open(str(out)))
    assert set(known) == {"name", "size"} and known["size"].kind is forms.Kind.RADIO
    assert sorted(known["size"].exports.values()) == ["m", "s"]
    plain = tmp_path / "plain.pdf"
    asyncio.run(pdf_mod.handle_write_pdf({"path": str(plain), "content_type": "html",
                                          "content": '<p><input type="text" name="name"></p>'}))
    assert not forms.fields(fitz.open(str(plain)))


def test_a_large_form_reads_in_parts_with_the_pages_to_read_next(tmp_path, monkeypatch):
    monkeypatch.setattr(forms, "MAX_FIELDS_LISTED", 12)
    doc = fitz.open()
    for page_number in range(6):
        page = doc.new_page()
        for n in range(5):
            _widget(page, f"p{page_number}_f{n}", fitz.PDF_WIDGET_TYPE_TEXT, (72, 80 + 30 * n, 300, 98 + 30 * n))
    src = tmp_path / "big.pdf"
    doc.save(str(src))
    out = pdf_mod.read_pdf(str(src), None, mode="form_fields")
    assert '"p2_f4"' in out and '"p3_f0"' not in out
    assert "15 of 30 fields listed" in out and "pages '4-6'" in out
    rest = pdf_mod.read_pdf(str(src), "4-6", mode="form_fields")
    assert '"p3_f0"' in rest and '"p5_f4"' in rest and "fields listed" not in rest


def test_one_page_with_too_many_fields_is_cut_within_the_page(tmp_path, monkeypatch):
    monkeypatch.setattr(forms, "MAX_FIELDS_LISTED", 4)
    monkeypatch.setattr(forms, "PAGE_FIELDS_CAP", 6)
    doc = fitz.open()
    page = doc.new_page()
    for n in range(9):
        _widget(page, f"f{n}", fitz.PDF_WIDGET_TYPE_TEXT, (72, 60 + 30 * n, 300, 78 + 30 * n))
    src = tmp_path / "dense.pdf"
    doc.save(str(src))
    out = pdf_mod.read_pdf(str(src), None, mode="form_fields")
    assert '"f5"' in out and '"f6"' not in out and "and 3 more fields on page_index 0" in out


def _resource_cycles(doc) -> list[int]:
    """Form XObjects reachable from a page's resources whose own resources
    are that page's resources again."""
    import re

    found = []
    for page in doc:
        kind, value = doc.xref_get_key(page.xref, "Resources")
        if kind != "xref":
            continue
        page_res = int(value.split()[0])
        xobjects = doc.xref_get_key(page_res, "XObject")
        text = xobjects[1] if xobjects[0] == "dict" else (
            doc.xref_object(int(xobjects[1].split()[0])) if xobjects[0] == "xref" else "")
        for ref in re.findall(r"(\d+) 0 R", text):
            own = doc.xref_get_key(int(ref), "Resources")
            if own[0] == "xref" and int(own[1].split()[0]) == page_res:
                found.append(int(ref))
    return found


def test_flattening_a_made_form_leaves_no_resource_cycle(tmp_path):
    src = _weasy_form(tmp_path / "flat.pdf")
    out = _edit(src, [{"op": "fill_form", "values": {"agree": True, "size.size": "m"}, "flatten": True}])
    assert out.startswith("PDF saved:"), out
    doc = fitz.open(str(src))
    assert _resource_cycles(doc) == []
    fitz.TOOLS.reset_mupdf_warnings()
    doc[0].get_text()
    doc[0].get_pixmap(dpi=40)
    assert fitz.TOOLS.mupdf_warnings() == ""


def test_the_next_pages_named_are_the_ones_asked_for(tmp_path, monkeypatch):
    monkeypatch.setattr(forms, "MAX_FIELDS_LISTED", 3)
    doc = fitz.open()
    for page_number in range(9):
        page = doc.new_page()
        for n in range(3):
            _widget(page, f"p{page_number}_f{n}", fitz.PDF_WIDGET_TYPE_TEXT, (72, 80 + 30 * n, 300, 98 + 30 * n))
    src = tmp_path / "pick.pdf"
    doc.save(str(src))
    out = pdf_mod.read_pdf(str(src), "1,5,9", mode="form_fields")
    assert '"p0_f2"' in out and '"p4_f0"' not in out
    assert "read pages '5, 9' next" in out, out
