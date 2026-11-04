"""The image takes every module, subfolder and data file the server reads,
and nothing else: `.dockerignore` is matched here by Docker's own rules
(patterns compared segment by segment, `*` never crossing a `/`, a path
excluded or admitted when one of its parent folders matches, the last
matching line winning)."""

import fnmatch
import os
import re
from pathlib import Path

import pytest

_HERE = Path(__file__).parent.parent
_SKIPPED_DIRS = {"__tests__", "skills", "venv", ".venv", "__pycache__", ".pytest_cache"}
_LICENSES = _HERE / "licenses"


def _patterns() -> list[tuple[bool, list[str]]]:
    out = []
    for line in (_HERE / ".dockerignore").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:] if negate else line
        out.append((negate, [s for s in pattern.strip("/").split("/") if s]))
    return out


def _match(segments: list[str], parts: list[str]) -> bool:
    if not segments:
        return not parts
    head, rest = segments[0], segments[1:]
    if head == "**":
        return any(_match(rest, parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _match(rest, parts[1:])


def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    excluded = False
    for negate, segments in _patterns():
        if any(_match(segments, parts[:i]) for i in range(1, len(parts) + 1)):
            excluded = not negate
    return excluded


@pytest.mark.parametrize("rel", [
    "server.py", "catalog.py", "requirements.txt", "THIRD-PARTY-NOTICES.md", "ooxml/order.py",
    "formats/__init__.py", "formats/xlsx/refshift.py", "formats/docx/numbering.py", "formats/pptx/deck_checks.py",
    "ooxml/schema/wml.json", "data/fonts.json", "licenses/Apache-2.0.txt",
])
def test_the_image_takes_the_server_and_its_data(rel):
    assert not _excluded(rel), rel


@pytest.mark.parametrize("rel", [
    "__tests__/test_batch.py", "__tests__/corpus/shared/a.docx", "skills/file-tools.md", "venv/bin/python",
    ".env", "manifest.json", "icon.png", "collabora-welcome.js", "docker-compose.yml", "Dockerfile",
    ".dockerignore", "__pycache__/server.cpython-313.pyc", "ooxml/__pycache__/order.cpython-313.pyc",
    "formats/xlsx/__pycache__/refshift.cpython-313.pyc",
    "notes.md", "scratch/run.py",
])
def test_the_image_never_takes_tests_settings_or_dev_files(rel):
    assert _excluded(rel), rel


def test_every_module_and_subfolder_present_is_admitted():
    for top in sorted(_HERE.glob("*.py")):
        assert not _excluded(top.name), top.name
    for folder, dirs, files in os.walk(_HERE):
        dirs[:] = [d for d in dirs if d not in _SKIPPED_DIRS and not d.startswith(".")]
        rel_folder = Path(folder).relative_to(_HERE)
        if rel_folder == Path("."):
            continue
        for name in files:
            rel = (rel_folder / name).as_posix()
            assert not _excluded(rel), f"{rel}: add its folder to .dockerignore"


def test_the_dockerfile_copies_through_the_allowlist():
    text = (_HERE / "Dockerfile").read_text()
    assert "COPY . ." in text and "COPY *.py" not in text


def _notice_entries() -> list[tuple[str, str, str]]:
    """(project, SPDX licence id, entry text) of each THIRD-PARTY-NOTICES.md
    entry, the format block at the end left out."""
    text = (_HERE / "THIRD-PARTY-NOTICES.md").read_text().split("\n## Entry format")[0]
    parts = re.split(r"(?m)^### (.+) \(([^()]+)\)\n", text)
    return [(parts[i], parts[i + 1], parts[i + 2]) for i in range(1, len(parts), 3)]


def test_every_notice_entry_ships_its_licence_text_in_the_image():
    entries = _notice_entries()
    assert entries
    named = set()
    for project, licence, body in entries:
        texts = re.findall(r"`licenses/([^`/]+)`", body)
        assert texts, f"{project}: name its licence text in licenses/"
        for name in texts:
            assert name.endswith(f"-{licence}.txt"), f"{project}: {name} is not a {licence} text"
            assert (_LICENSES / name).is_file(), f"{project}: licenses/{name} is missing"
            assert not _excluded(f"licenses/{name}"), name
            named.add(name)
    assert {p.name for p in _LICENSES.iterdir()} == named, "a text in licenses/ that no entry names"
