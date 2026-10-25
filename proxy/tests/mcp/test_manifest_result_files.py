"""Manifest ``result_files`` — the tools whose pictures and file blobs the
platform keeps: exact tool names and ONE plain folder name, refused at parse
otherwise (never at write time)."""

from __future__ import annotations

import json

import pytest

from services.mcp.mcp_manifest_parse import _parse_manifest


def _write(tmp_path, extra: dict, name: str = "x-mcp"):
    d = tmp_path / name
    d.mkdir(parents=True)
    data = {
        "name": name, "label": "X", "description": "X tools.", "version": "1.0.0",
        "category": "community",
        "server": {"runtime": "python", "transport": "stdio", "command": "python", "args": ["s.py"]},
        "credentials": {"type": "none"},
        **extra,
    }
    p = d / "manifest.json"
    p.write_text(json.dumps(data))
    return p


def test_absent_is_none(tmp_path):
    assert _parse_manifest(_write(tmp_path, {})).result_files is None


def test_the_catalog_shape_parses(tmp_path):
    m = _parse_manifest(_write(tmp_path, {
        "result_files": {"tools": ["maps_static_map"], "subdir": "maps"}}))
    assert m.result_files.tools == ["maps_static_map"]
    assert m.result_files.subdir == "maps"


def test_subdir_defaults_to_the_mcp_name(tmp_path):
    m = _parse_manifest(_write(tmp_path, {"result_files": {"tools": ["a"]}}, name="google-maps"))
    assert m.result_files.subdir == "google-maps"


def test_subdir_defaults_to_mcp_files_when_the_name_is_no_folder_name(tmp_path):
    m = _parse_manifest(_write(tmp_path, {"result_files": {"tools": ["a"]}}, name="x.mcp"))
    assert m.result_files.subdir == "mcp-files"


@pytest.mark.parametrize("subdir", [
    "..", ".", "", "/abs", "a/b", "a\\b", ".hidden", "C:x", "maps.png",
    "x" * 65, "-lead", None, 3, ["maps"], "NUL", "com1", "Lpt9", "inbox", "Uploads", "apps",
])
def test_a_subdir_that_is_not_one_plain_folder_is_refused(tmp_path, subdir):
    with pytest.raises(ValueError, match="result_files"):
        _parse_manifest(_write(tmp_path, {"result_files": {"tools": ["a"], "subdir": subdir}}))


@pytest.mark.parametrize("tools", [
    [], "maps_static_map", ["*"], ["maps_*"], ["a b"], [""], [3], ["a", "a"],
    [f"t{i}" for i in range(65)], None,
])
def test_tools_must_be_exact_names(tmp_path, tools):
    with pytest.raises(ValueError, match="result_files"):
        _parse_manifest(_write(tmp_path, {"result_files": {"tools": tools, "subdir": "maps"}}))


@pytest.mark.parametrize("raw", ["maps", ["maps"], 1, True])
def test_the_block_must_be_an_object(tmp_path, raw):
    with pytest.raises(ValueError, match="result_files"):
        _parse_manifest(_write(tmp_path, {"result_files": raw}))


def test_a_tool_relocation_already_moves_is_refused(tmp_path):
    outputs = [{"source": "${mcp_dir}/shots", "destination_template": "${workspace_dir}/.screenshots",
                "after_tools": ["browser_take_screenshot"]}]
    with pytest.raises(ValueError, match="both outputs and result_files"):
        _parse_manifest(_write(tmp_path, {"outputs": outputs, "result_files": {
            "tools": ["browser_take_screenshot"], "subdir": "shots"}}))
    star = [{**outputs[0], "after_tools": ["*"]}]
    with pytest.raises(ValueError, match="both outputs and result_files"):
        _parse_manifest(_write(tmp_path / "b", {"outputs": star, "result_files": {
            "tools": ["route_map"], "subdir": "maps"}}))


def test_a_relocated_tool_beside_another_kept_tool_parses(tmp_path):
    outputs = [{"source": "${mcp_dir}/shots", "destination_template": "${workspace_dir}/.screenshots",
                "after_tools": ["browser_take_screenshot"]}]
    m = _parse_manifest(_write(tmp_path, {"outputs": outputs, "result_files": {"tools": ["route_map"]}}))
    assert m.result_files.tools == ["route_map"]


def test_the_default_folder_skips_a_reserved_mcp_name(tmp_path):
    m = _parse_manifest(_write(tmp_path, {"result_files": {"tools": ["a"]}}, name="inbox"))
    assert m.result_files.subdir == "mcp-files"
