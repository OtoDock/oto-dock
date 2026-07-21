"""Tests for manifest ``tool_arg_paths`` parsing + JSONPath validator.

Covers ``ToolArgPathDeclaration`` dataclass, ``_parse_tool_arg_paths``
strict-validation behavior, and ``_validate_tool_arg_json_path``
subset rules.
"""

import json
import sys
from pathlib import Path

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from services.mcp.mcp_registry import (  # noqa: E402
    ToolArgPathDeclaration,
    _parse_manifest,
    _parse_tool_arg_paths,
    _validate_tool_arg_json_path,
)


# ---------------------------------------------------------------------------
# JSONPath validator
# ---------------------------------------------------------------------------


class TestJsonPathValidatorAccepts:
    @pytest.mark.parametrize(
        "expr",
        [
            "path",
            "file_path",
            "data.file_path",
            "a.b.c.d",
            "images[*].source",
            "paths[*]",
            "matches[*].results[*].file_path",
            "a[*].b[*]",
            "deeply.nested.thing[*].with[*].many[*].levels",
            "snake_case_name",
            "_underscore_leading",
            "x123.y456[*]",
        ],
    )
    def test_accepts(self, expr):
        assert _validate_tool_arg_json_path(expr) == ""


class TestJsonPathValidatorRejects:
    def test_empty(self):
        err = _validate_tool_arg_json_path("")
        assert err
        assert "non-empty" in err

    def test_none_type(self):
        err = _validate_tool_arg_json_path(None)  # type: ignore[arg-type]
        assert err

    def test_recursive_descent(self):
        err = _validate_tool_arg_json_path("path..key")
        assert "recursive descent" in err

    def test_wildcard_double_star(self):
        err = _validate_tool_arg_json_path("a.**.b")
        assert "wildcard" in err.lower() or "**" in err

    def test_filter_predicate(self):
        err = _validate_tool_arg_json_path("path[?(@.name)]")
        assert "predicate" in err.lower()

    def test_numeric_index(self):
        err = _validate_tool_arg_json_path("paths[3]")
        assert "numeric" in err.lower() or "indices" in err.lower()

    def test_bracket_string_double_quotes(self):
        err = _validate_tool_arg_json_path('a["key"]')
        assert "bracket-string" in err.lower() or "[\"key\"]" in err

    def test_bracket_string_single_quotes(self):
        err = _validate_tool_arg_json_path("a['key']")
        assert "bracket-string" in err.lower() or "'" in err

    def test_leading_dot(self):
        err = _validate_tool_arg_json_path(".path")
        assert "leading" in err.lower() or "trailing" in err.lower()

    def test_trailing_dot(self):
        err = _validate_tool_arg_json_path("path.")
        assert "leading" in err.lower() or "trailing" in err.lower()

    def test_dot_before_bracket(self):
        err = _validate_tool_arg_json_path("path.[*]")
        # Not in the regex grammar; we don't need a precise message,
        # just a non-empty error.
        assert err

    def test_bare_brackets(self):
        err = _validate_tool_arg_json_path("[*]")
        # No leading identifier — invalid.
        assert err

    def test_space_in_name(self):
        err = _validate_tool_arg_json_path("path with space")
        assert err

    def test_hyphen_in_name(self):
        # Hyphens are not Python-identifier chars; reject.
        err = _validate_tool_arg_json_path("kebab-name")
        assert err

    def test_starts_with_digit(self):
        # Identifiers must start with letter or underscore.
        err = _validate_tool_arg_json_path("3path")
        assert err


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_or_raise(raw):
    """Helper — returns list[ToolArgPathDeclaration]."""
    return _parse_tool_arg_paths(raw, mcp_name="test-mcp")


class TestParserAccepts:
    def test_missing_field_yields_empty_list(self):
        assert _parse_or_raise(None) == []

    def test_empty_dict_yields_empty_list(self):
        assert _parse_or_raise({}) == []

    def test_single_tool_single_path(self):
        decls = _parse_or_raise({
            "send_file": {"path": {"mode": "read"}},
        })
        assert decls == [
            ToolArgPathDeclaration(
                tool="send_file", json_path="path", mode="read",
                optional=False, relative_anchor="",
            ),
        ]

    def test_default_mode_is_read(self):
        decls = _parse_or_raise({
            "show": {"path": {}},
        })
        assert decls[0].mode == "read"

    def test_write_mode_parsed(self):
        decls = _parse_or_raise({
            "save_image": {"dest_path": {"mode": "write"}},
        })
        assert decls[0].mode == "write"

    def test_multiple_tools_and_paths(self):
        decls = _parse_or_raise({
            "display_images": {
                "images[*].source": {"mode": "read"},
            },
            "send_file": {
                "path": {"mode": "read"},
            },
            "save_image": {
                "dest_path": {"mode": "write"},
            },
        })
        assert len(decls) == 3
        tools = {d.tool for d in decls}
        assert tools == {"display_images", "send_file", "save_image"}

    def test_optional_flag(self):
        decls = _parse_or_raise({
            "edit_image": {
                "input_path": {"mode": "read"},
                "preview_path": {"mode": "write", "optional": True},
            },
        })
        opt_map = {d.json_path: d.optional for d in decls}
        assert opt_map["input_path"] is False
        assert opt_map["preview_path"] is True

    def test_relative_anchor_carried_through(self):
        decls = _parse_or_raise({
            "tool": {"path": {"mode": "read", "relative_anchor": "/users"}},
        })
        assert decls[0].relative_anchor == "/users"

    def test_nested_jsonpath_preserved(self):
        decls = _parse_or_raise({
            "tool": {
                "matches[*].results[*].file_path": {"mode": "read"},
            },
        })
        assert decls[0].json_path == "matches[*].results[*].file_path"


class TestParserRejects:
    def test_top_level_not_dict(self):
        with pytest.raises(ValueError, match="must be an object"):
            _parse_or_raise([])

    def test_top_level_string(self):
        with pytest.raises(ValueError, match="must be an object"):
            _parse_or_raise("paths")

    def test_empty_tool_name(self):
        with pytest.raises(ValueError, match="non-empty tool name"):
            _parse_or_raise({"": {"path": {"mode": "read"}}})

    def test_per_tool_not_dict(self):
        with pytest.raises(ValueError, match="must be an object"):
            _parse_or_raise({"tool": "path"})

    def test_decl_not_dict(self):
        with pytest.raises(ValueError, match="must be an object"):
            _parse_or_raise({"tool": {"path": "read"}})

    def test_invalid_jsonpath_predicate(self):
        with pytest.raises(ValueError, match="predicate"):
            _parse_or_raise({
                "tool": {"path[?(@.x)]": {"mode": "read"}},
            })

    def test_invalid_jsonpath_recursive(self):
        with pytest.raises(ValueError, match="recursive"):
            _parse_or_raise({
                "tool": {"a..b": {"mode": "read"}},
            })

    def test_invalid_jsonpath_numeric(self):
        with pytest.raises(ValueError, match="numeric"):
            _parse_or_raise({
                "tool": {"paths[0]": {"mode": "read"}},
            })

    def test_invalid_jsonpath_bracket_string(self):
        with pytest.raises(ValueError, match="bracket-string"):
            _parse_or_raise({
                "tool": {'data["k"]': {"mode": "read"}},
            })

    def test_invalid_mode(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            _parse_or_raise({
                "tool": {"path": {"mode": "execute"}},
            })

    def test_relative_anchor_must_start_with_slash(self):
        with pytest.raises(ValueError, match="must start with"):
            _parse_or_raise({
                "tool": {"path": {"relative_anchor": "users"}},
            })

    def test_error_message_includes_tool_and_path(self):
        with pytest.raises(ValueError) as ei:
            _parse_or_raise({
                "display_images": {"images[?(@.x)].source": {"mode": "read"}},
            })
        # The validator should mention the specific tool and json_path
        # so manifest authors can locate the typo quickly.
        msg = str(ei.value)
        assert "display_images" in msg
        assert "images[?(@.x)].source" in msg


# ---------------------------------------------------------------------------
# End-to-end via _parse_manifest
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: dict) -> Path:
    """Write a minimal valid manifest plus the given extra body fields."""
    base = {
        "name": "tap-test",
        "label": "TAP Test",
        "description": "tool-arg-paths parser test",
        "version": "1.0.0",
        "category": "custom",
        "server": {"type": "stdio", "command": "python", "args": ["./server.py"]},
    }
    base.update(body)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(base))
    return p


class TestManifestIntegration:
    def test_manifest_without_tool_arg_paths(self, tmp_path):
        mf = _write_manifest(tmp_path, {})
        m = _parse_manifest(mf)
        assert m is not None
        assert m.tool_arg_paths == []

    def test_manifest_with_valid_tool_arg_paths(self, tmp_path):
        mf = _write_manifest(tmp_path, {
            "tool_arg_paths": {
                "display_images": {
                    "images[*].source": {"mode": "read"},
                },
                "save_image": {
                    "dest_path": {"mode": "write"},
                },
            },
        })
        m = _parse_manifest(mf)
        assert m is not None
        assert len(m.tool_arg_paths) == 2
        tools = {d.tool for d in m.tool_arg_paths}
        assert tools == {"display_images", "save_image"}

    def test_manifest_with_invalid_tool_arg_paths_raises(self, tmp_path):
        mf = _write_manifest(tmp_path, {
            "tool_arg_paths": {
                "tool": {"a[?(@)]": {"mode": "read"}},
            },
        })
        with pytest.raises(ValueError, match="tap-test:.*tool_arg_paths"):
            _parse_manifest(mf)

    def test_per_tool_filter_helper(self, tmp_path):
        # Confirm flat-list shape gives a straightforward filter pattern
        # for the interceptor.
        mf = _write_manifest(tmp_path, {
            "tool_arg_paths": {
                "display_images": {
                    "images[*].source": {"mode": "read"},
                    "header_image": {"mode": "read"},
                },
                "save_image": {
                    "dest_path": {"mode": "write"},
                },
            },
        })
        m = _parse_manifest(mf)
        assert m is not None
        per_tool = [d for d in m.tool_arg_paths if d.tool == "display_images"]
        assert len(per_tool) == 2
        paths = {d.json_path for d in per_tool}
        assert paths == {"images[*].source", "header_image"}
