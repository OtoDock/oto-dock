"""The shared corpus carries no personal data: every package's authors are a
library's name or empty, and no part holds an e-mail address or a home
folder."""

import re
import zipfile
from pathlib import Path

import pytest

CORPUS = Path(__file__).parent / "corpus" / "shared"
_TOOLS = {"", "python-docx", "openpyxl", "python-pptx"}
_AUTHOR_RE = re.compile(r"<(?:dc:creator|cp:lastModifiedBy)>([^<]*)<")
_EMAIL_RE = re.compile(rb"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)
_HOME_RE = re.compile(rb"/home/[\w.-]+|/Users/[\w.-]+|[A-Z]:\\\\Users\\\\", re.IGNORECASE)


_LEGACY = {".doc", ".xls", ".ppt"}
_COMPANY_RE = re.compile(r"<Company>([^<]*)<")


def _clean(data: bytes, where: str) -> None:
    for text in (data, data.replace(b"\x00", b"")):
        assert not _EMAIL_RE.search(text), where
        assert not _HOME_RE.search(text), where


@pytest.mark.parametrize("path", sorted(p for p in CORPUS.iterdir() if p.suffix != ".md"), ids=lambda p: p.name)
def test_no_personal_data(path):
    if path.suffix in _LEGACY:
        _clean(path.read_bytes(), path.name)
        return
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "docProps/core.xml" in names:
            core = archive.read("docProps/core.xml").decode("utf-8")
            assert set(_AUTHOR_RE.findall(core)) <= _TOOLS
        if "docProps/app.xml" in names:
            assert set(_COMPANY_RE.findall(archive.read("docProps/app.xml").decode("utf-8"))) <= {""}
        for name in names:
            _clean(archive.read(name), name)


def test_every_file_has_its_provenance():
    readme = (CORPUS / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in CORPUS.iterdir() if p.suffix != ".md" and f"`{p.name}`" not in readme]
    assert missing == []
