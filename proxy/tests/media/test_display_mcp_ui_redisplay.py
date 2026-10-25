"""Unit tests for display-mcp's html-less re-display file resolution.

`_artifact_read_candidates` / `_read_artifact_file` power the cheap
iteration flow: the agent Edits the saved artifact file, then calls
display_ui with ONLY save_path — the MCP re-reads the file in its own
namespace and forwards the content. Pure functions (env + path in,
candidates/content out), imported directly like the resize helpers.
"""

from __future__ import annotations

import sys

import pytest

from tests._paths import CUSTOM_MCPS

MCP_DIR = CUSTOM_MCPS / "display-mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import display_server as d


@pytest.fixture()
def satellite_agent_scope(monkeypatch, tmp_path):
    """Remote-machine agent-scope session: OTO_WORKSPACE_DIR is
    machine-absolute `<agent_root>/workspace`."""
    ws = tmp_path / "agents" / "demo" / "workspace"
    ws.mkdir(parents=True)
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("OTO_USERNAME", "")
    return ws


@pytest.fixture()
def satellite_user_scope(monkeypatch, tmp_path):
    """Remote-machine user-scope session: OTO_WORKSPACE_DIR is
    `<agent_root>/users/<u>/workspace`."""
    root = tmp_path / "agents" / "demo"
    ws = root / "users" / "alice" / "workspace"
    ws.mkdir(parents=True)
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("OTO_USERNAME", "alice")
    return root


class TestCandidates:
    def test_relative_joins_workspace(self, satellite_agent_scope):
        cands = d._artifact_read_candidates("games/board.html")
        assert str(satellite_agent_scope / "games" / "board.html") in cands

    def test_relative_gets_html_suffix_fallback(self, satellite_agent_scope):
        cands = d._artifact_read_candidates("games/board")
        assert str(satellite_agent_scope / "games" / "board") in cands
        assert str(satellite_agent_scope / "games" / "board.html") in cands

    def test_virtual_workspace_anchors_to_agent_root(self, satellite_agent_scope):
        cands = d._artifact_read_candidates("/workspace/games/board.html")
        # as-is first (real inside the local sandbox), then agent-root-anchored
        assert cands[0] == "/workspace/games/board.html"
        assert str(satellite_agent_scope / "games" / "board.html") in cands

    def test_user_scope_resolves_own_user_path(self, satellite_user_scope):
        cands = d._artifact_read_candidates(
            "/users/alice/workspace/generated-ui/x.html"
        )
        assert (
            str(satellite_user_scope / "users" / "alice" / "workspace" /
                "generated-ui" / "x.html") in cands
        )

    def test_user_scope_shared_path_reads_shared_tree(self, satellite_user_scope):
        # The hook's _sandbox_to_host maps /workspace/… to the SHARED
        # workspace for editor+ user sessions — the read side must anchor to
        # agent_root/workspace, NOT the user's personal workspace.
        cands = d._artifact_read_candidates("/workspace/board.html")
        assert str(satellite_user_scope / "workspace" / "board.html") in cands
        personal = str(
            satellite_user_scope / "users" / "alice" / "workspace" / "board.html"
        )
        assert personal not in cands

    def test_unknown_env_yields_only_as_is(self, monkeypatch):
        monkeypatch.setenv("OTO_WORKSPACE_DIR", "")
        monkeypatch.setenv("OTO_USERNAME", "")
        assert d._artifact_read_candidates("/workspace/x.html") == ["/workspace/x.html"]
        assert d._artifact_read_candidates("rel/x.html") == []


class TestReadArtifactFile:
    def test_reads_existing_file(self, satellite_agent_scope):
        f = satellite_agent_scope / "generated-ui" / "chart.html"
        f.parent.mkdir()
        f.write_text("<div>hi</div>", "utf-8")
        html, err = d._read_artifact_file("generated-ui/chart.html")
        assert err == ""
        assert html == "<div>hi</div>"

    def test_reads_virtual_form_on_satellite(self, satellite_agent_scope):
        f = satellite_agent_scope / "games" / "board.html"
        f.parent.mkdir()
        f.write_text("<b>board</b>", "utf-8")
        html, err = d._read_artifact_file("/workspace/games/board.html")
        assert err == ""
        assert html == "<b>board</b>"

    def test_missing_file_errors_with_guidance(self, satellite_agent_scope):
        html, err = d._read_artifact_file("nope/missing.html")
        assert html is None
        assert "no artifact file found" in err
        assert "pass 'html'" in err

    def test_empty_file_errors(self, satellite_agent_scope):
        f = satellite_agent_scope / "empty.html"
        f.write_text("   \n", "utf-8")
        html, err = d._read_artifact_file("empty.html")
        assert html is None
        assert "empty" in err

    def test_oversized_file_errors(self, satellite_agent_scope):
        f = satellite_agent_scope / "big.html"
        f.write_bytes(b"x" * (d.MAX_UI_HTML_BYTES + 1))
        html, err = d._read_artifact_file("big.html")
        assert html is None
        assert "2MB" in err
