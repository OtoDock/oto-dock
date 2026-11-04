"""A password-protected PDF keeps its protection when edited: the one
save keeps the file's own encryption, unless an operation sets or removes
it. The password opens the file and never shows in a result."""

import asyncio
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pdf as pdf_mod
import shared
from formats.pdf import edit

_USER, _OWNER = "u-secret-1", "o-secret-2"


async def _ident(p, writing=False, **kw):
    return p


async def _nothing(*a, **k):
    return None


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setattr(shared, "_resolve_path", _ident)
    monkeypatch.setattr(shared, "_push_preview", _nothing)
    monkeypatch.setattr(shared, "_notify_file_written", _nothing)
    monkeypatch.setattr(shared, "_to_agents_relative", lambda p: p)


def _protected(path: Path, *, user: str = _USER, method=None, text: str = "SSN 123-45-6789 visible") -> Path:
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), text, fontsize=12)
    doc.new_page().insert_text((72, 100), "second page", fontsize=12)
    doc.save(str(path), encryption=method or fitz.PDF_ENCRYPT_AES_256, user_pw=user, owner_pw=_OWNER,
             permissions=fitz.PDF_PERM_PRINT)
    doc.close()
    return path


def _edit(path: Path, ops, **extra) -> str:
    return asyncio.run(pdf_mod.handle_edit_pdf({"path": str(path), "operations": ops, **extra}))


def _protection(path: Path, user: str = _USER) -> tuple:
    """The method, the permission bits at user level, and what each password
    opens: (method, bits, user level, owner level)."""
    doc = fitz.open(str(path))
    if doc.needs_pass:
        doc.authenticate(user)
    method, bits = (doc.metadata or {}).get("encryption"), doc.permissions
    user_level = fitz.open(str(path)).authenticate(user) if doc.needs_pass else 0
    return method, bits, user_level, fitz.open(str(path)).authenticate(_OWNER)


def _opened(path: Path, password: str = _USER):
    doc = fitz.open(str(path))
    if doc.needs_pass:
        assert doc.authenticate(password)
    return doc


def test_the_password_opens_the_file_and_it_stays_protected(tmp_path):
    src = _protected(tmp_path / "a.pdf")
    before = _protection(src)
    out = _edit(src, [{"op": "add_text", "text": "edited"}, {"op": "compress"}], password=_USER)
    assert _protection(src) == before and before[2:] == (2, 4)
    assert out.startswith("PDF saved:") and "kept its password protection" in out
    assert _USER not in out and _OWNER not in out
    doc = fitz.open(str(src))
    assert doc.needs_pass and doc.authenticate(_USER) == 2
    assert doc.metadata["encryption"].startswith("Standard V5")
    assert "edited" in doc[0].get_text()
    assert fitz.open(str(src)).authenticate(_OWNER) == 4


def test_without_its_password_nothing_is_saved_and_the_fix_is_named(tmp_path):
    src = _protected(tmp_path / "b.pdf")
    before = src.read_bytes()
    out = _edit(src, [{"op": "add_text", "text": "x"}])
    assert out.startswith("Nothing saved:") and "password argument" in out
    out = _edit(src, [{"op": "add_text", "text": "x"}], password="wrong")
    assert out.startswith("Nothing saved:") and "not the one given" in out and "wrong" not in out
    assert src.read_bytes() == before


def test_an_owner_password_alone_is_kept(tmp_path):
    src = _protected(tmp_path / "c.pdf", user="", method=fitz.PDF_ENCRYPT_AES_128)
    assert not fitz.open(str(src)).needs_pass
    before = _protection(src)
    out = _edit(src, [{"op": "rotate_page", "pages": "1"}])
    assert out.startswith("PDF saved:") and "kept its password protection" in out
    assert _protection(src) == before
    assert before[0].startswith("Standard V4") and before[1] != -4 and before[3] == 4


@pytest.mark.parametrize("method", ["PDF_ENCRYPT_RC4_128", "PDF_ENCRYPT_AES_128"])
def test_older_methods_are_kept_too(tmp_path, method):
    src = _protected(tmp_path / f"{method}.pdf", method=getattr(fitz, method))
    before = fitz.open(str(src))
    before.authenticate(_USER)
    kind = before.metadata["encryption"]
    _edit(src, [{"op": "add_text", "text": "x"}], password=_USER)
    assert _opened(src).metadata["encryption"] == kind


def test_a_redaction_on_a_protected_file_leaves_no_earlier_revision(tmp_path):
    src = _protected(tmp_path / "d.pdf")
    out = _edit(src, [{"op": "redact", "find": "123-45-6789"}], password=_USER)
    assert out.startswith("PDF saved:")
    data = src.read_bytes()
    assert data.count(b"%%EOF") == 1  # one revision: nothing to cut back to
    assert "123-45-6789" not in _opened(src)[0].get_text()


def test_decrypt_removes_the_password_and_the_later_of_encrypt_and_decrypt_wins(tmp_path):
    src = _protected(tmp_path / "e.pdf")
    out = _edit(src, [{"op": "decrypt", "password": _OWNER}])
    assert out.startswith("PDF saved:") and not fitz.open(str(src)).needs_pass
    _edit(src, [{"op": "encrypt", "user_password": "new"}, {"op": "decrypt"}])
    assert not fitz.open(str(src)).needs_pass
    _edit(src, [{"op": "decrypt"}, {"op": "encrypt", "user_password": "new"}])
    doc = fitz.open(str(src))
    assert doc.needs_pass and doc.authenticate("new")


def test_a_decrypt_op_s_password_opens_the_file_for_the_ops_before_it(tmp_path):
    src = _protected(tmp_path / "f.pdf")
    out = _edit(src, [{"op": "add_text", "text": "before decrypt"}, {"op": "decrypt", "password": _OWNER}])
    assert out.startswith("PDF saved:"), out
    doc = fitz.open(str(src))
    assert not doc.needs_pass and "before decrypt" in doc[0].get_text()


def test_an_ocr_copy_of_a_protected_file_keeps_its_protection(tmp_path):
    from batch import WriteCall
    from results import ResolvedPath

    src = _protected(tmp_path / "g.pdf")
    before = _protection(src)
    copy = tmp_path / "g-ocr.pdf"
    work = tmp_path / "work"
    work.mkdir()
    call = WriteCall(path=str(src), ops=[
        (0, {"op": "ocr", "pages": "1", "output_path": ResolvedPath(str(copy))}),
        (1, {"op": "add_text", "text": "also edited"}),
    ], work_dir=str(work), extra={"password": _USER}, original=None)
    report = edit.edit_pdf_core(call, ocr_page=edit._stub_ocr)
    assert report.saved, report.text
    assert "has no password" not in report.text
    assert _protection(src) == before and "also edited" in _opened(src)[0].get_text()
    assert _protection(copy) == before
    assert "also edited" not in _opened(copy)[0].get_text()


def test_the_main_document_is_saved_once(tmp_path, monkeypatch):
    saves = []
    real = fitz.Document.save

    def _counting(self, *a, **k):
        saves.append(a[0] if a else k.get("filename"))
        return real(self, *a, **k)

    src = _protected(tmp_path / "h.pdf")
    monkeypatch.setattr(fitz.Document, "save", _counting)
    _edit(src, [{"op": "add_text", "text": "x"}, {"op": "set_metadata", "title": "T"}], password=_USER)
    assert len(saves) == 1


def test_read_document_opens_a_protected_pdf_with_its_password(tmp_path):
    src = _protected(tmp_path / "i.pdf")
    with pytest.raises(ValueError, match="password-protected"):
        pdf_mod.read_pdf(str(src), None)
    out = pdf_mod.read_pdf(str(src), None, password=_USER)
    assert "SSN 123-45-6789" in out and "**Security**: Encrypted (Standard V5" in out
    owner_only = _protected(tmp_path / "j.pdf", user="", method=fitz.PDF_ENCRYPT_AES_128)
    assert "**Security**: Encrypted" in pdf_mod.read_pdf(str(owner_only), None)


def _owner_only(path: Path, perms: int = fitz.PDF_PERM_PRINT) -> Path:
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "owner restricted", fontsize=12)
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="", owner_pw=_OWNER, permissions=perms)
    doc.close()
    return path


def test_removing_an_owner_password_needs_the_owner_password(tmp_path):
    src = _owner_only(tmp_path / "k.pdf")
    before = src.read_bytes()
    for ops, extra in (([{"op": "decrypt"}], {}), ([{"op": "decrypt", "password": "wrong"}], {}),
                       ([{"op": "encrypt", "user_password": "mine"}], {})):
        out = _edit(src, ops, **extra)
        assert out.startswith("Nothing saved:") and "owner password" in out, out
        assert src.read_bytes() == before
    out = _edit(src, [{"op": "decrypt", "password": _OWNER}])
    assert out.startswith("PDF saved:") and "restrictions are lifted" in out and _OWNER not in out, out
    doc = fitz.open(str(src))
    assert not doc.metadata.get("encryption")


def test_a_user_password_alone_cannot_remove_a_separate_owner_password(tmp_path):
    src = _protected(tmp_path / "l.pdf")
    out = _edit(src, [{"op": "decrypt", "password": _USER}])
    assert out.startswith("Nothing saved:") and "owner password" in out
    out = _edit(src, [{"op": "decrypt"}], password=_OWNER)
    assert out.startswith("PDF saved:"), out
    assert not fitz.open(str(src)).needs_pass


def test_one_password_for_both_opens_and_removes(tmp_path):
    src = tmp_path / "m.pdf"
    _edit(_protected(tmp_path / "seed.pdf"), [{"op": "decrypt", "password": _OWNER}])
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "x")
    doc.save(str(src))
    _edit(src, [{"op": "encrypt", "user_password": "both"}])
    out = _edit(src, [{"op": "decrypt", "password": "both"}])
    assert out.startswith("PDF saved:"), out


def test_an_edit_the_owner_forbids_is_made_with_a_warning(tmp_path):
    src = _owner_only(tmp_path / "n.pdf")
    before = _protection(src)
    out = _edit(src, [{"op": "add_text", "text": "x"}, {"op": "rotate_page", "pages": "1"}])
    assert out.startswith("PDF saved:"), out
    assert _protection(src) == before
    assert "op #0 add_text: the file's owner does not allow changing its content" in out
    assert "op #1 rotate_page: the file's owner does not allow" in out
    assert fitz.open(str(src)).metadata["encryption"]
    out = _edit(src, [{"op": "add_text", "text": "y"}], password=_OWNER)
    assert "does not allow" not in out
    allowed = _owner_only(tmp_path / "o.pdf", perms=fitz.PDF_PERM_PRINT | fitz.PDF_PERM_MODIFY |
                          fitz.PDF_PERM_ASSEMBLE)
    assert "does not allow" not in _edit(allowed, [{"op": "add_text", "text": "z"}])



# A copy of a protected PDF: it keeps the protection the PDF has at that
# point of the batch, or the copy is refused.


@pytest.mark.parametrize("kind", ["owner-only", "user and owner"])
def test_a_split_keeps_the_pdf_s_protection(tmp_path, kind):
    if kind == "owner-only":
        src, extra = _owner_only(tmp_path / "v.pdf"), {}
    else:
        src, extra = _protected(tmp_path / "v.pdf"), {"password": _USER}
    before = _protection(src)
    part = tmp_path / "part.pdf"
    out = _edit(src, [{"op": "split", "pages": "all", "output_path": str(part)}], **extra)
    assert "Side files written" in out and "has no password" not in out, out
    assert _protection(part) == before


def test_a_split_after_removing_or_replacing_the_protection_follows_the_batch(tmp_path):
    src = _owner_only(tmp_path / "w.pdf")
    part = tmp_path / "plain.pdf"
    out = _edit(src, [{"op": "decrypt", "password": _OWNER}, {"op": "split", "pages": "1", "output_path": str(part)}])
    assert out.startswith("PDF saved:"), out
    assert not (fitz.open(str(part)).metadata or {}).get("encryption")
    src2 = _owner_only(tmp_path / "w2.pdf")
    part2 = tmp_path / "new.pdf"
    _edit(src2, [{"op": "encrypt", "user_password": "fresh"}, {"op": "split", "pages": "1", "output_path": str(part2)}],
          password=_OWNER)
    doc = fitz.open(str(part2))
    assert doc.needs_pass and doc.authenticate("fresh")


@pytest.mark.parametrize("kind", ["owner-only", "user and owner"])
def test_a_protected_pdf_is_not_merged_into_another(tmp_path, kind):
    source = _owner_only(tmp_path / "src.pdf") if kind == "owner-only" else _protected(tmp_path / "src.pdf")
    target = tmp_path / "plain.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(target))
    before = target.read_bytes()
    out = _edit(target, [{"op": "merge", "files": [str(source)]}, {"op": "delete_page", "pages": [0]}])
    assert out.startswith("Nothing saved:") and "src.pdf is protected" in out and "owner password" in out, out
    assert target.read_bytes() == before


def test_images_are_extracted_from_a_protected_pdf_only_as_its_owner_allows(tmp_path):
    from PIL import Image

    logo = tmp_path / "logo.png"
    Image.new("RGB", (10, 10), "blue").save(logo)

    def with_image(name: str, perms: int) -> Path:
        doc = fitz.open()
        doc.new_page().insert_image(fitz.Rect(10, 10, 60, 60), filename=str(logo))
        path = tmp_path / name
        doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="", owner_pw=_OWNER, permissions=perms)
        return path

    locked = with_image("locked.pdf", fitz.PDF_PERM_PRINT)
    out = _edit(locked, [{"op": "extract_images", "output_dir": str(tmp_path / "a")}])
    assert out.startswith("Nothing saved:") and "does not allow copying" in out and "owner password" in out, out
    assert not (tmp_path / "a").exists()
    out = _edit(locked, [{"op": "extract_images", "output_dir": str(tmp_path / "b")}], password=_OWNER)
    assert "extracted 1 image" in out, out
    allowed = with_image("allowed.pdf", fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY)
    out = _edit(allowed, [{"op": "extract_images", "output_dir": str(tmp_path / "c")}])
    assert "extracted 1 image" in out, out


def test_page_images_of_a_protected_pdf_follow_its_owner_s_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_mod, "_push_image_preview", _nothing)
    monkeypatch.setattr(pdf_mod, "_resolve_path", _ident)
    locked = _owner_only(tmp_path / "x.pdf")
    with pytest.raises(RuntimeError, match="does not allow copying"):
        asyncio.run(pdf_mod.handle_pdf_to_images({"path": str(locked), "output_dir": str(tmp_path / "p"), "dpi": 20}))
    assert not (tmp_path / "p").exists()
    out = asyncio.run(pdf_mod.handle_pdf_to_images({"path": str(locked), "output_dir": str(tmp_path / "q"),
                                                    "dpi": 20, "password": _OWNER}))
    assert out.startswith("Rendered 1 pages")


def test_lifting_the_restrictions_names_printing(tmp_path):
    src = _owner_only(tmp_path / "y.pdf", perms=0)
    out = _edit(src, [{"op": "decrypt", "password": _OWNER}])
    assert out.startswith("PDF saved:") and "printing it" in out, out
