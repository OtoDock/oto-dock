"""Tests for `shared._to_agents_relative` (v2 path framework)
and `shared._unicode_match_on_disk` (NFC/NFD fallback for non-ASCII
filenames written by Drive/macOS/Slack/etc.).
"""

import os
import sys
import unicodedata
from pathlib import Path

# Make the parent dir importable as a top-level module
sys.path.insert(0, str(Path(__file__).parent.parent))

import shared


def test_to_agents_relative_strips_mount_prefix():
    """Container-absolute paths under /agents become agents-relative."""
    assert shared._to_agents_relative(
        "/agents/personal-assistant/users/alice/workspace/foo.docx"
    ) == "personal-assistant/users/alice/workspace/foo.docx"


def test_to_agents_relative_handles_root():
    """The bare mount root collapses to empty string."""
    assert shared._to_agents_relative("/agents") == ""


def test_to_agents_relative_nested_paths():
    """Deep paths are stripped correctly."""
    assert shared._to_agents_relative(
        "/agents/agent/users/u/workspace/dir/sub/file.png"
    ) == "agent/users/u/workspace/dir/sub/file.png"


def test_to_agents_relative_already_relative_unchanged():
    """Already-agents-relative paths are returned unchanged."""
    assert shared._to_agents_relative(
        "personal-assistant/users/alice/workspace/foo.docx"
    ) == "personal-assistant/users/alice/workspace/foo.docx"


def test_to_agents_relative_out_of_tree_unchanged():
    """Out-of-tree absolute paths (no /agents prefix) are returned unchanged."""
    assert shared._to_agents_relative("/tmp/foo") == "/tmp/foo"
    assert shared._to_agents_relative("/etc/passwd") == "/etc/passwd"


def test_to_agents_relative_partial_match_not_stripped():
    """Paths that *contain* but don't *start with* /agents are NOT stripped."""
    # /agents-other/foo should not be treated as under /agents/
    assert shared._to_agents_relative("/agents-other/foo") == "/agents-other/foo"


def test_mount_agents_dir_constant():
    """MOUNT_AGENTS_DIR is hardcoded post-v2 — no env knob."""
    assert shared.MOUNT_AGENTS_DIR == "/agents"


# ---------------------------------------------------------------------------
# _unicode_match_on_disk — NFC/NFD fallback for non-ASCII filenames
# ---------------------------------------------------------------------------

# Greek filename in NFC (precomposed ί = U+03AF) and NFD (ι + U+0301)
# This is the exact case from the Drive download bug: Google Drive served
# the filename in NFD (Mac-uploaded), LLM/JSON echoes paths back in NFC.
GREEK_BASE = "Για την ιστοσελίδα.docx"
NFC = unicodedata.normalize("NFC", GREEK_BASE)
NFD = unicodedata.normalize("NFD", GREEK_BASE)


def test_unicode_match_exact_path_fast_path(tmp_path):
    """ASCII-only path that exists is returned verbatim without listdir."""
    f = tmp_path / "hello.txt"
    f.write_text("hi")
    assert shared._unicode_match_on_disk(str(f)) == str(f)


def test_unicode_match_nfd_on_disk_nfc_query(tmp_path):
    """File written in NFD (Drive/Mac), looked up via NFC (LLM) — finds it."""
    nfd_path = tmp_path / NFD
    nfd_path.write_text("body")
    # Sanity-check that NFC != NFD as raw bytes
    assert NFC != NFD, "test setup wrong — strings are equal"
    # Lookup with NFC (the form the LLM passes back)
    nfc_query = str(tmp_path / NFC)
    result = shared._unicode_match_on_disk(nfc_query)
    # Should return the actual on-disk (NFD) path
    assert os.path.exists(result)
    assert os.path.basename(result) == NFD


def test_unicode_match_nfc_on_disk_nfd_query(tmp_path):
    """Reverse: file in NFC, looked up via NFD — also resolves."""
    nfc_path = tmp_path / NFC
    nfc_path.write_text("body")
    nfd_query = str(tmp_path / NFD)
    result = shared._unicode_match_on_disk(nfd_query)
    assert os.path.exists(result)
    assert os.path.basename(result) == NFC


def test_unicode_match_returns_original_when_no_match(tmp_path):
    """Genuinely missing file → return original path so caller raises a
    clear FileNotFoundError downstream."""
    missing = str(tmp_path / "nope.txt")
    assert shared._unicode_match_on_disk(missing) == missing


def test_unicode_match_returns_original_when_parent_missing(tmp_path):
    """No parent dir → skip the listdir, return original."""
    nowhere = str(tmp_path / "nonexistent_subdir" / "file.txt")
    assert shared._unicode_match_on_disk(nowhere) == nowhere


def test_unicode_match_empty_basename(tmp_path):
    """Edge case: directory paths or trailing-slash inputs don't crash."""
    # str(Path) strips trailing slash, so we synthesize the edge case
    assert shared._unicode_match_on_disk("") == ""
    assert shared._unicode_match_on_disk("/") == "/"


def test_unicode_match_ascii_path_no_listdir(tmp_path, monkeypatch):
    """Fast-path: when ASCII path exists verbatim, we don't scan listdir."""
    f = tmp_path / "ascii.txt"
    f.write_text("hi")
    calls = []
    original = os.listdir
    def spy(p):
        calls.append(p)
        return original(p)
    monkeypatch.setattr(os, "listdir", spy)
    shared._unicode_match_on_disk(str(f))
    assert calls == [], "should not listdir when exact path exists"


def test_unicode_match_handles_listdir_permission_error(tmp_path, monkeypatch):
    """If listdir raises (permission denied etc.), fall through to original."""
    missing = str(tmp_path / NFC)
    def boom(p):
        raise PermissionError("no access")
    monkeypatch.setattr(os, "listdir", boom)
    assert shared._unicode_match_on_disk(missing) == missing


def test_unicode_match_multiple_entries_picks_normalized_match(tmp_path):
    """Parent has many files; we find the one whose NFC form matches query."""
    (tmp_path / "unrelated1.txt").write_text("x")
    (tmp_path / "unrelated2.txt").write_text("x")
    (tmp_path / NFD).write_text("body")
    (tmp_path / "unrelated3.txt").write_text("x")
    nfc_query = str(tmp_path / NFC)
    result = shared._unicode_match_on_disk(nfc_query)
    assert os.path.basename(result) == NFD


# ---------------------------------------------------------------------------
# writing flag threading (_resolve_via_proxy → /v1/hooks/resolve-path)
# ---------------------------------------------------------------------------


class _Resp:
    status_code = 200

    def json(self):
        return {
            "host_path": "/x",
            "agents_relative": "agent/users/u/workspace/f.png",
        }


def _wire_proxy(monkeypatch, captured):
    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(
        shared, "_current_session", lambda: ("sess-1", "Bearer t"),
    )
    monkeypatch.setattr(shared, "PROXY_URL", "http://proxy:8000")
    monkeypatch.setattr(shared.httpx, "post", fake_post)


def test_resolve_via_proxy_posts_writing_flag(monkeypatch):
    """Write targets tell the proxy so a missing output file resolves to its
    creation path on remote sessions instead of failing the lazy pull."""
    captured = {}
    _wire_proxy(monkeypatch, captured)
    rel, reason = shared._resolve_via_proxy(
        "/users/u/workspace/f.png", writing=True,
    )
    assert rel == "agent/users/u/workspace/f.png"
    assert captured["json"]["writing"] is True


def test_resolve_via_proxy_defaults_to_read(monkeypatch):
    captured = {}
    _wire_proxy(monkeypatch, captured)
    rel, reason = shared._resolve_via_proxy("/users/u/workspace/f.png")
    assert rel == "agent/users/u/workspace/f.png"
    assert captured["json"]["writing"] is False


# ---------------------------------------------------------------------------
# In-place edits are WRITES: their input must resolve with writing=True so
# the proxy's write-RBAC fires (editor + /knowledge in-place edit → 403).
# The explicit-output shape keeps the input as a read.
# ---------------------------------------------------------------------------

import asyncio


def _capture_resolve(monkeypatch, module):
    calls = []

    def fake_resolve(p, writing=False):
        calls.append((p, writing))
        return "/nonexistent/target"

    monkeypatch.setattr(module, "_resolve_path", fake_resolve)
    return calls


def test_edit_image_in_place_resolves_input_as_write(monkeypatch):
    import images

    calls = _capture_resolve(monkeypatch, images)
    out = asyncio.run(images.handle_edit_image({"path": "/knowledge/logo.png"}))
    assert "File not found" in out  # resolve happened; missing file stops it
    assert calls == [("/knowledge/logo.png", True)]


def test_edit_image_with_output_keeps_input_read(monkeypatch):
    import images

    calls = _capture_resolve(monkeypatch, images)
    asyncio.run(images.handle_edit_image(
        {"path": "/knowledge/logo.png", "output_path": "/workspace/out.png"}
    ))
    assert calls[0] == ("/knowledge/logo.png", False)


def test_edit_pdf_resolves_input_as_write(monkeypatch):
    import types

    import pdf

    # pymupdf only exists in the container image; the handler bails on the
    # missing file before touching it, so a stub module satisfies the import.
    monkeypatch.setitem(sys.modules, "fitz", types.ModuleType("fitz"))
    calls = _capture_resolve(monkeypatch, pdf)
    out = asyncio.run(pdf.handle_edit_pdf({"path": "/knowledge/doc.pdf"}))
    assert "File not found" in out
    assert calls == [("/knowledge/doc.pdf", True)]
