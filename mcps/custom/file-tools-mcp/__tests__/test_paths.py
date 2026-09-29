"""Tests for `shared._to_agents_relative` (v2 path framework)
and `shared._unicode_match_on_disk` (NFC/NFD fallback for non-ASCII
filenames written by Drive/macOS/Slack/etc.).
"""

import asyncio
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
    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(
        shared, "_current_session", lambda: ("sess-1", "Bearer t"),
    )
    monkeypatch.setattr(shared, "PROXY_URL", "http://proxy:8000")
    monkeypatch.setattr(shared.httpx, "AsyncClient", _FakeClient)


def test_resolve_via_proxy_posts_writing_flag(monkeypatch):
    """Write targets tell the proxy so a missing output file resolves to its
    creation path on remote sessions instead of failing the lazy pull."""
    captured = {}
    _wire_proxy(monkeypatch, captured)
    rel, agent, reason = asyncio.run(shared._resolve_via_proxy(
        "/users/u/workspace/f.png", writing=True,
    ))
    assert rel == "agent/users/u/workspace/f.png"
    assert captured["json"]["writing"] is True


def test_resolve_via_proxy_defaults_to_read(monkeypatch):
    captured = {}
    _wire_proxy(monkeypatch, captured)
    rel, agent, reason = asyncio.run(shared._resolve_via_proxy("/users/u/workspace/f.png"))
    assert rel == "agent/users/u/workspace/f.png"
    assert captured["json"]["writing"] is False


def test_container_absolute_paths_go_through_the_proxy(monkeypatch):
    """The mount holds every agent's tree, so a `/agents/...` path is not
    trusted on its prefix alone: it is handed to the proxy in agents-relative
    form and the session's path policy decides (2026-09-08)."""
    calls = []

    async def fake_proxy(p, writing=False):
        calls.append((p, writing))
        if "other-agent" in p:
            return None, "", "403: outside this session's scope"
        return p, "pa", ""

    monkeypatch.setattr(shared, "_resolve_via_proxy", fake_proxy)
    monkeypatch.setattr(shared, "_unicode_match_on_disk", lambda p: p)
    out = asyncio.run(shared._resolve_path("/agents/pa/users/u/workspace/f.docx", writing=True))
    assert out == "/agents/pa/users/u/workspace/f.docx"
    assert calls == [("pa/users/u/workspace/f.docx", True)]
    try:
        asyncio.run(shared._resolve_path("/agents/other-agent/users/v/workspace/secret.docx"))
    except ValueError as e:
        assert "outside this session's scope" in str(e)
    else:
        raise AssertionError("another agent's tree must not resolve on the prefix alone")


# ---------------------------------------------------------------------------
# In-place edits are WRITES: their input must resolve with writing=True so
# the proxy's write-RBAC fires (editor + /knowledge in-place edit → 403).
# The explicit-output shape keeps the input as a read.
# ---------------------------------------------------------------------------


def _capture_resolve(monkeypatch, module):
    calls = []

    async def fake_resolve(p, writing=False):
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


# ---------------------------------------------------------------------------
# The answer is confined to the caller's own tree
# ---------------------------------------------------------------------------


def _mount(tmp_path, monkeypatch):
    mount = tmp_path / "agents"
    (mount / "proj-a/users/pm/workspace").mkdir(parents=True)
    (mount / "proj-b/config").mkdir(parents=True)
    (mount / "proj-b/config/agent.md").write_text("VICTIM PERSONA")
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(mount))
    return mount


def _proxy_says(monkeypatch, agents_rel, agent=None):
    """The hook always names the agent its answer sits in (the host-cache
    pseudo-agent for a satellite-host pull); a test that gives none gets the
    answer's first segment, as the hook computes it."""
    echoed = agents_rel.split("/", 1)[0] if agent is None else agent

    async def fake(path, writing=False):
        return agents_rel, echoed, ""
    monkeypatch.setattr(shared, "_resolve_via_proxy", fake)


def test_resolve_refuses_a_path_that_lands_in_another_agents_tree(tmp_path, monkeypatch):
    """The proxy authorized the caller's own (then absent) output; a link now
    sits there. The answer leaves the caller's tree, so it is refused and the
    other tree is untouched."""
    import pytest
    mount = _mount(tmp_path, monkeypatch)
    _proxy_says(monkeypatch, "proj-a/users/pm/workspace/out.docx")
    victim = mount / "proj-b/config/agent.md"
    selfw = mount / "proj-a/users/pm/workspace"
    (selfw / "out.docx").symlink_to(os.path.relpath(victim, selfw))
    with pytest.raises(ValueError, match="leaves this session's agent tree"):
        asyncio.run(shared._resolve_path("/workspace/out.docx", writing=True))
    assert victim.read_text() == "VICTIM PERSONA"


def test_resolve_admits_the_callers_own_tree_and_prefers_the_echoed_slug(tmp_path, monkeypatch):
    mount = _mount(tmp_path, monkeypatch)
    _proxy_says(monkeypatch, "proj-a/users/pm/workspace/out.docx")
    out = asyncio.run(shared._resolve_path("/workspace/out.docx", writing=True))
    assert out == str(mount / "proj-a/users/pm/workspace/out.docx")
    # The proxy's echoed slug binds the tree even when the answer's first
    # segment says otherwise.
    import pytest
    _proxy_says(monkeypatch, "proj-b/config/agent.md", agent="proj-a")
    with pytest.raises(ValueError, match="leaves this session's agent tree"):
        asyncio.run(shared._resolve_path("/config/agent.md"))
    # A slug that is not a plain name binds nothing.
    _proxy_says(monkeypatch, "../etc/passwd", agent="..")
    with pytest.raises(ValueError):
        asyncio.run(shared._resolve_path("/etc/passwd"))


def test_resolve_admits_the_sessions_own_host_cache_only(tmp_path, monkeypatch):
    import pytest
    mount = _mount(tmp_path, monkeypatch)
    cache = mount / ".remote-host-cache"
    (cache / "sess-1" / "d1").mkdir(parents=True)
    (cache / "sess-2" / "d2").mkdir(parents=True)
    (cache / "sess-1" / "d1" / "x.docx").write_text("mine")
    (cache / "sess-2" / "d2" / "y.docx").write_text("theirs")
    shared.set_request_context("sess-1", "Bearer t")
    _proxy_says(monkeypatch, ".remote-host-cache/sess-1/d1/x.docx")
    assert asyncio.run(shared._resolve_path("C:/Users/u/Desktop/x.docx")).endswith("sess-1/d1/x.docx")
    _proxy_says(monkeypatch, ".remote-host-cache/sess-2/d2/y.docx")
    with pytest.raises(ValueError, match="leaves this session's agent tree"):
        asyncio.run(shared._resolve_path("C:/Users/u/Desktop/y.docx"))
    shared.set_request_context("", "")
    _proxy_says(monkeypatch, ".remote-host-cache/sess-1/d1/x.docx")
    with pytest.raises(ValueError):
        asyncio.run(shared._resolve_path("C:/Users/u/Desktop/x.docx"))
