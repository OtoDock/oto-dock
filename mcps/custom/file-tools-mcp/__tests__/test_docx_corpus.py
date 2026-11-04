"""Word files made by other applications, edited as they are: every one
reads with addresses, takes an edit by address with no package error it
did not already have, and keeps everything the edit did not touch; the
corpus carries no personal data, and a file no application saved names
none."""

import asyncio
import collections
import re
import shutil
import zipfile
from pathlib import Path

import pytest
from lxml import etree

pytest.importorskip("docx")

import checks
import word
from ooxml import validate

HERE = Path(__file__).parent / "corpus"
FILES = sorted([*(HERE / "docx").glob("*.docx"), *(HERE / "shared").glob("*.docx")])
_AUTHOR_RE = re.compile(r"<(?:dc:creator|cp:lastModifiedBy)>([^<]*)<")
_EMAIL_RE = re.compile(rb"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)
_HOME_RE = re.compile(rb"/home/[\w.-]+|/Users/[\w.-]+", re.IGNORECASE)
_REVIEWER_RE = re.compile(rb'w:(?:author|initials)="([^"]*)"')
_APPLICATION_RE = re.compile(rb"<Application>([^<]*)<")
# Reviewers in the corpus are roles, never people.
_ROLE_NAMES = {b"Editor", b"Legal", b"Sales", b"OtoDock", b"E", b"L", b"S", b"O"}


async def _ident(p, *a, **k):
    return p


async def _quiet(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import shared

    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _quiet)
    monkeypatch.setattr(shared, "_notify_file_written", _quiet)


def _write(path, ops) -> str:
    return asyncio.run(word.handle_write_docx({"path": str(path), "operations": ops}))


def _errors(path) -> collections.Counter:
    with checks.CheckContext(str(path), "docx") as ctx:
        return collections.Counter(f.key or f.message for f in validate.validate_package(ctx)
                                   if f.severity is checks.Severity.ERROR)


def _canonical(data: bytes) -> bytes:
    from lxml import etree

    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False, no_network=True)
    return etree.tostring(etree.fromstring(data, parser), method="c14n")


def _body_without(data: bytes, added: set[str]) -> bytes:
    """The document part without the paragraphs an edit added and without
    paragraph ids, which every save stamps on the parts it touched."""
    from lxml import etree

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    w14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(data, parser)
    for p in list(root.iter(f"{w}p")):
        if "".join(t.text or "" for t in p.iter(f"{w}t")) in added:
            p.getparent().remove(p)
    for el in root.iter():
        for name in (f"{w14}paraId", f"{w14}textId"):
            el.attrib.pop(name, None)
    # Stamping ids declares w14 ignorable on the root.
    root.attrib.pop("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable", None)
    # Elements, attributes and text only: a save may move namespace
    # declarations, which is not a change to the body.
    return repr([(el.tag, sorted(el.attrib.items()), (el.text or "").strip(), (el.tail or "").strip())
                 for el in root.iter()]).encode()


def _parts(path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _types_by_part(parts: dict[str, bytes]) -> dict[str, str]:
    """Each part's content type, from a Default or an Override: python-docx
    rewrites the content types part and drops defaults no part uses."""
    root = etree.fromstring(parts["[Content_Types].xml"])
    defaults = {el.get("Extension").lower(): el.get("ContentType") for el in root if el.tag.endswith("}Default")}
    overrides = {el.get("PartName").lstrip("/"): el.get("ContentType") for el in root if el.tag.endswith("}Override")}
    return {name: overrides.get(name, defaults.get(name.rsplit(".", 1)[-1].lower(), ""))
            for name in parts if name != "[Content_Types].xml"}


@pytest.fixture(params=FILES, ids=lambda p: p.name)
def corpus_file(request, tmp_path):
    target = tmp_path / request.param.name
    shutil.copy(request.param, target)
    return target


def test_it_reads_with_addresses(corpus_file):
    out = word.read_docx(str(corpus_file))
    assert "\np0" in out and "blocks):" in out


def test_an_edit_by_address_adds_no_error_and_keeps_the_rest(corpus_file):
    before_errors = _errors(corpus_file)
    before = _parts(corpus_file)
    added = {"Added by address", "Added at the end"}
    # Untracked on purpose: a file that records changes would track them,
    # and this compares everything else byte for byte.
    msg = _write(corpus_file, [{"op": "insert_paragraph", "target": "p0", "text": "Added by address", "track": False},
                               {"op": "add_paragraph", "text": "Added at the end", "track": False}])
    assert msg.startswith("Document saved"), msg
    after_errors = _errors(corpus_file)
    assert after_errors - before_errors == collections.Counter()
    after = _parts(corpus_file)
    assert set(after) == set(before)
    assert _body_without(after["word/document.xml"], added) == _body_without(before["word/document.xml"], added)
    para_ids = re.compile(rb'w14:paraId="([0-9A-Fa-f]{8})"')
    assert set(para_ids.findall(before["word/document.xml"])) <= set(para_ids.findall(after["word/document.xml"]))
    for name, data in before.items():
        if name in ("word/document.xml", "[Content_Types].xml"):
            continue
        if name.endswith((".xml", ".rels")):
            assert _canonical(after[name]) == _canonical(data), name
        else:
            assert after[name] == data, name
    assert _types_by_part(after) == _types_by_part(before)
    reread = word.read_docx(str(corpus_file))
    assert re.search(r"\np1#[0-9A-F]{8} Added by address\n", reread), reread


def test_split_tokens_fill_in_the_hand_built_file(tmp_path):
    source = HERE / "docx" / "word-markup-handbuilt.docx"
    target = tmp_path / source.name
    shutil.copy(source, target)
    msg = _write(target, [{"op": "fill_template", "data": {"client": {"name": "Ada"}}}])
    assert "filled 1 token: client.name 1" in msg
    out = word.read_docx(str(target))
    assert "Dear Ada, the fee is {-500-}{+600+} EUR a month." in out
    assert "\nsdt0 content control\n" in out and "t0/r2 (repeating)" in out and "p3/textbox0/p0" in out


@pytest.mark.parametrize("path", sorted((HERE / "docx").glob("*.docx")), ids=lambda p: p.name)
def test_no_personal_data(path):
    with zipfile.ZipFile(path) as archive:
        core = archive.read("docProps/core.xml").decode("utf-8")
        assert set(_AUTHOR_RE.findall(core)) <= {"", "python-docx"}
        for name in archive.namelist():
            data = archive.read(name)
            assert not _EMAIL_RE.search(data), name
            assert not _HOME_RE.search(data), name
            assert set(_REVIEWER_RE.findall(data)) <= _ROLE_NAMES, name
        if "handbuilt" in path.name:
            assert _APPLICATION_RE.findall(archive.read("docProps/app.xml")) == [b""]


def test_libreoffices_notes_comments_and_changes_are_read_and_settled(tmp_path):
    source = HERE / "docx" / "libreoffice-7.4-review.docx"
    target = tmp_path / source.name
    shutil.copy(source, target)
    out = word.read_docx(str(target))
    assert "[footnote1]" in out and "**Footnotes**:\n  footnote1/p0" in out, out
    assert "{-500-}{+550+}" in out and "{+Fees are invoiced monthly.+}" in out, out
    legal = re.search(r"\n  (comment\d) on p\d+ \"thirty \(30\) days\" by \"Legal\"", out)
    assert legal, out
    msg = _write(target, [{"op": "reply_to_comment", "target": legal.group(1), "text": "Settled at 45.",
                           "author": "Legal"},
                          {"op": "set_paragraph_text", "target": "footnote1/p0", "text": "Calendar days, not working "
                           "days."}])
    assert msg.startswith("Document saved"), msg
    msg = _write(target, [{"op": "accept_all_changes"}])
    assert "accepted:" in msg, msg
    pristine = tmp_path / "pristine.docx"
    shutil.copy(source, pristine)
    assert _errors(target) - _errors(pristine) == collections.Counter()
    reread = word.read_docx(str(target))
    assert "550 EUR" in reread and "{-" not in reread and "Calendar days, not working days." in reread
