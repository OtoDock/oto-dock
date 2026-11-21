"""Tests for `shared._to_agents_relative` (v2 path framework)
and `shared._unicode_match_on_disk` (NFC/NFD fallback for non-ASCII
filenames written by Drive/macOS/Slack/etc.).
"""

import asyncio
import os
import sys
import unicodedata
from pathlib import Path

import pytest

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
    monkeypatch.setattr(shared, "_resolve_path", pdf._resolve_path)
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


def test_a_link_at_the_answer_is_met_by_the_open_not_the_lookup(tmp_path, monkeypatch):
    """The proxy authorized the caller's own (then absent) output; a link to
    a stand-in in another agent's tree now sits there. The answer is admitted
    by its text (the lookup follows nothing), a read of it is refused, and
    the write replaces the link: the stand-in is untouched."""
    mount = _mount(tmp_path, monkeypatch)
    _proxy_says(monkeypatch, "proj-a/users/pm/workspace/out.docx")
    standin = mount / "proj-b/config/agent.md"
    selfw = mount / "proj-a/users/pm/workspace"
    (selfw / "out.docx").symlink_to(os.path.relpath(standin, selfw))
    out = asyncio.run(shared._resolve_path("/workspace/out.docx", writing=True))
    assert out == str(selfw / "out.docx")
    with pytest.raises(PermissionError, match="Cannot read"):
        shared.safe_open_read(out)
    with shared.safe_open_write(out) as fh:
        fh.write(b"NEW")
    assert not (selfw / "out.docx").is_symlink() and (selfw / "out.docx").read_bytes() == b"NEW"
    assert standin.read_text() == "VICTIM PERSONA"


def test_resolve_returns_the_answer_as_written(tmp_path, monkeypatch):
    """The server's answer is link-free when it is given; the tool never
    resolves it again, so a link that appears on the way afterwards does not
    change the path handed to the reader (the strict open meets it)."""
    mount = _mount(tmp_path, monkeypatch)
    (mount / "proj-a/users/pm/real").mkdir()
    (mount / "proj-a/users/pm/linked").symlink_to("real")
    _proxy_says(monkeypatch, "proj-a/users/pm/linked/out.docx")
    out = asyncio.run(shared._resolve_path("/linked/out.docx", writing=True))
    assert out == str(mount / "proj-a/users/pm/linked/out.docx")


@pytest.mark.parametrize("answer", [
    "proj-a/users/../users/pm/x.docx",
    "proj-a/./users/pm/x.docx",
    "proj-a//users/pm/x.docx",
    "proj-a/users/pm/..",
])
def test_resolve_refuses_an_answer_with_a_dot_or_empty_part(tmp_path, monkeypatch, answer):
    """Every part of the answer is a plain name: a dot or an empty part is
    refused, never folded."""
    _mount(tmp_path, monkeypatch)
    _proxy_says(monkeypatch, answer, agent="proj-a")
    with pytest.raises(ValueError, match="leaves this session's agent tree"):
        asyncio.run(shared._resolve_path("/x.docx"))


def test_resolve_matches_the_name_off_the_event_loop(tmp_path, monkeypatch):
    """The Unicode match touches the disk: it runs in a worker thread, never
    on the loop every session shares."""
    import threading
    _mount(tmp_path, monkeypatch)
    _proxy_says(monkeypatch, "proj-a/users/pm/workspace/x.docx")
    seen = []

    def spy(p):
        seen.append(threading.current_thread() is threading.main_thread())
        return p
    monkeypatch.setattr(shared, "_unicode_match_on_disk", spy)
    asyncio.run(shared._resolve_path("/workspace/x.docx"))
    assert seen == [False]


def test_unicode_match_lists_through_the_handle(tmp_path):
    """A folder on the way that is a link to a stand-in holding a name in
    another Unicode form: the folder is never listed through the link, so the
    name is answered as asked (the strict open then refuses it)."""
    standin = tmp_path / "standin"
    standin.mkdir()
    (standin / NFD).write_text("STAND-IN")
    (tmp_path / "docs").symlink_to(standin)
    query = str(tmp_path / "docs" / NFC)
    assert shared._unicode_match_on_disk(query) == query


def test_unicode_match_lists_the_folder_it_opened(tmp_path, monkeypatch):
    """The parent is listed through the handle its strict open gave, never
    by its name a second time."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / NFD).write_text("x")
    seen = []
    real = os.listdir

    def spy(p="."):
        seen.append(p)
        return real(p)

    monkeypatch.setattr(os, "listdir", spy)
    assert shared._unicode_match_on_disk(str(tmp_path / "docs" / NFC)) == str(tmp_path / "docs" / NFD)
    assert seen and all(isinstance(p, int) for p in seen), seen


def test_an_answer_in_a_folder_that_only_starts_with_the_slug_is_refused():
    """The answer is the caller's own folder or below it: a folder whose
    name merely starts with the caller's slug is another agent's tree."""
    own = shared._answer_in_tree("proj-a/x.docx", "proj-a")
    assert own == shared.MOUNT_AGENTS_DIR + "/proj-a/x.docx"
    assert shared._answer_in_tree("proj-a-other/x.docx", "proj-a") is None


def test_unicode_match_finds_a_name_in_a_nested_folder(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / NFD).write_text("body")
    found = shared._unicode_match_on_disk(str(tmp_path / "a" / "b" / NFC))
    assert found == str(tmp_path / "a" / "b" / NFD)


def test_cleanup_partials_removes_through_the_parents_handle(tmp_path):
    """The output's folder swapped for a link to a stand-in holding a temp of
    the same name: nothing is listed or removed through the link."""
    standin = tmp_path / "standin"
    standin.mkdir()
    partial = standin / ".out.docx.0123456789ab.partial"
    partial.write_bytes(b"STAND-IN")
    (tmp_path / "out").symlink_to(standin)
    shared.cleanup_partials(str(tmp_path / "out" / "out.docx"))
    assert partial.read_bytes() == b"STAND-IN"


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


# ---------------------------------------------------------------------------
# A sibling output is named from the caller's own path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caller, suffix, want", [
    ("/users/u/workspace/photo.png", ".jpg", "/users/u/workspace/photo.jpg"),
    ("photo.png", ".webp", "photo.webp"),
    ("C:\\Users\\u\\My.Pictures\\photo", ".jpg", "C:\\Users\\u\\My.Pictures\\photo.jpg"),
    ("C:\\Users\\u\\My.Pictures\\photo.png", ".jpg", "C:\\Users\\u\\My.Pictures\\photo.jpg"),
    ("C:/Users/u/v1.2/report", ".pdf", "C:/Users/u/v1.2/report.pdf"),
    ("/workspace/archive.tar.gz", ".pdf", "/workspace/archive.tar.pdf"),
    ("/workspace/.hidden", ".pdf", "/workspace/.hidden.pdf"),
    ("/workspace/report.pdf", "_pages", "/workspace/report_pages"),
    ("/workspace/report.pdf", "", "/workspace/report"),
])
def test_sibling_path_changes_the_suffix_of_the_last_part_only(caller, suffix, want):
    assert shared.sibling_path(caller, suffix) == want


def test_a_converted_image_is_named_from_the_callers_path(tmp_path, monkeypatch):
    """The converted copy's name is the caller's own string with the new
    suffix, resolved once as a write; never the resolved container form,
    which the server reads for reads only."""
    from PIL import Image

    import images

    Image.new("RGB", (8, 8), "red").save(tmp_path / "photo.png")
    calls = []

    async def fake(p, writing=False):
        calls.append((p, writing))
        return str(tmp_path / p.replace("\\", "/").rsplit("/", 1)[-1])

    monkeypatch.setattr(images, "_resolve_path", fake)
    monkeypatch.setattr(shared, "_resolve_path", fake)
    out = asyncio.run(images.handle_edit_image({"path": "/workspace/photo.png", "operations": [
        {"op": "convert_format", "format": "webp"}]}))
    assert ("/workspace/photo.webp", True) in calls, calls
    assert out.startswith("Image saved:") and (tmp_path / "photo.webp").exists()


def _convert_defaults(tmp_path, monkeypatch, caller: str, answer: Path, args: dict, *, mount: Path):
    """The write path convert_document asks for when no output_path is
    given (the call stops there)."""
    import pdf
    import render_engine

    monkeypatch.setattr(pdf, "MOUNT_AGENTS_DIR", str(mount))
    writes = []

    async def fake(p, writing=False):
        if writing:
            writes.append(p)
            raise ValueError("stopped at the output")
        return str(answer)

    async def converted(path, fmt, *, out_dir, **_kw):
        made = Path(out_dir) / f"{Path(path).stem}.{fmt}"
        await asyncio.to_thread(made.write_bytes, b"made")
        return str(made)

    monkeypatch.setattr(pdf, "_resolve_path", fake)
    monkeypatch.setattr(shared, "_resolve_path", fake)
    monkeypatch.setattr(render_engine, "convert", converted)
    with pytest.raises(ValueError, match="stopped at the output"):
        asyncio.run(pdf.handle_convert_document({"input_path": caller, **args}))
    return writes


_DEFAULTS = [
    ("notes.md", b"# Notes\n", {"output_format": "pdf"}, ".pdf"),
    ("photo.png", None, {"output_format": "pdf"}, ".pdf"),
    ("doc.pdf", None, {"output_format": "png"}, ""),
    ("report.docx", b"PK", {"output_format": "odt"}, ".odt"),
]


@pytest.mark.parametrize("name, data, args, suffix", _DEFAULTS)
def test_a_default_output_beside_a_remote_machines_file_keeps_its_machine_path(
        tmp_path, monkeypatch, name, data, args, suffix):
    """A host-cache answer: the default output is the caller's own path with
    its suffix changed, so a paired computer's file gets its default beside
    it when that output is already on the machine. A new file there is
    refused with a sentence: give ``output_path`` in the workspace."""
    cache = tmp_path / ".remote-host-cache" / "sess-1" / "abc"
    cache.mkdir(parents=True)
    (cache / name).write_bytes(data or b"x")
    caller = f"C:\\Users\\u\\My.Docs\\{name}"
    stem = name.rsplit(".", 1)[0]
    writes = _convert_defaults(tmp_path, monkeypatch, caller, cache / name, args, mount=tmp_path)
    assert writes == [f"C:\\Users\\u\\My.Docs\\{stem}{suffix}"]


@pytest.mark.parametrize("form", ["display", "container", "sandbox"])
def test_a_default_output_in_the_agents_tree_is_asked_for_in_its_sandbox_form(tmp_path, monkeypatch, form):
    """The server resolves the agents-relative form the tools print for
    reads only: an input named that way gets its default output in the
    sandbox form."""
    docs = tmp_path / "pa" / "users" / "u" / "workspace" / "docs"
    docs.mkdir(parents=True)
    (docs / "notes.md").write_bytes(b"# Notes\n")
    caller = {"display": "pa/users/u/workspace/docs/notes.md",
              "container": str(docs / "notes.md"),
              "sandbox": "/users/u/workspace/docs/notes.md"}[form]
    writes = _convert_defaults(tmp_path, monkeypatch, caller, docs / "notes.md", {"output_format": "pdf"},
                               mount=tmp_path)
    assert writes == ["/users/u/workspace/docs/notes.pdf"]


def test_a_default_output_through_a_persons_folder_link_keeps_the_callers_place(tmp_path, monkeypatch):
    """An input named in the tree form through a person's own folder link:
    the server answers the link's target, and the default output is asked
    for at the caller's own place in its sandbox form, through the link."""
    shared_docs = tmp_path / "pa/workspace/team"
    shared_docs.mkdir(parents=True)
    (shared_docs / "notes.md").write_bytes(b"# Notes\n")
    mine = tmp_path / "pa/users/u/workspace"
    mine.mkdir(parents=True)
    (mine / "team").symlink_to(shared_docs)
    writes = _convert_defaults(tmp_path, monkeypatch, "pa/users/u/workspace/team/notes.md",
                               shared_docs / "notes.md", {"output_format": "pdf"}, mount=tmp_path)
    assert writes == ["/users/u/workspace/team/notes.pdf"]


def test_a_relative_input_that_starts_with_the_slug_keeps_its_relative_output(tmp_path, monkeypatch):
    """``pa/notes.md`` read as a path in the workspace (nothing sits at the
    agent's own ``pa/notes.md``): its default output is asked for the same
    way, never at the agent's root."""
    ws = tmp_path / "pa/workspace/pa"
    ws.mkdir(parents=True)
    (ws / "notes.md").write_bytes(b"# Notes\n")
    writes = _convert_defaults(tmp_path, monkeypatch, "pa/notes.md", ws / "notes.md",
                               {"output_format": "pdf"}, mount=tmp_path)
    assert writes == ["pa/notes.pdf"]


def test_a_relative_input_named_like_another_agent_keeps_its_relative_output(tmp_path, monkeypatch):
    """``team/notes.md`` in agent ``pa``'s workspace, while an agent named
    ``team`` holds a ``notes.md`` too: only the caller's own agent names the
    tree form, so the output stays beside the input."""
    ws = tmp_path / "pa" / "workspace" / "team"
    ws.mkdir(parents=True)
    (ws / "notes.md").write_bytes(b"# Notes\n")
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "notes.md").write_bytes(b"# Theirs\n")
    writes = _convert_defaults(tmp_path, monkeypatch, "team/notes.md", ws / "notes.md",
                               {"output_format": "pdf"}, mount=tmp_path)
    assert writes == ["team/notes.pdf"]


def test_a_container_form_that_names_nothing_in_the_tree_keeps_its_own_output(tmp_path, monkeypatch):
    """The container form and the bare form are one text to the server: a
    container path that names nothing at its tree place, read in the
    workspace, gets its output named from the caller's own text too."""
    ws = tmp_path / "pa" / "workspace" / "pa"
    ws.mkdir(parents=True)
    (ws / "notes.md").write_bytes(b"# Notes\n")
    writes = _convert_defaults(tmp_path, monkeypatch, f"{tmp_path}/pa/notes.md", ws / "notes.md",
                               {"output_format": "pdf"}, mount=tmp_path)
    assert writes == [f"{tmp_path}/pa/notes.pdf"]


def test_pdf_to_images_names_its_default_folder_from_the_callers_path(tmp_path, monkeypatch):
    import pdf

    cache = tmp_path / ".remote-host-cache" / "sess-1" / "abc"
    cache.mkdir(parents=True)
    (cache / "scan.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(pdf, "MOUNT_AGENTS_DIR", str(tmp_path))
    writes = []

    async def fake(p, writing=False):
        if writing:
            writes.append(p)
            raise ValueError("stopped at the output")
        return str(cache / "scan.pdf")

    monkeypatch.setattr(pdf, "_resolve_path", fake)
    with pytest.raises(ValueError, match="stopped at the output"):
        asyncio.run(pdf.handle_pdf_to_images({"path": "C:\\Users\\u\\Scans.2026\\scan.pdf"}))
    assert writes == ["C:\\Users\\u\\Scans.2026\\scan_pages"]
