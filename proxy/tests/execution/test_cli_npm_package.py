"""The npm package a globally installed engine CLI comes from, read from
where its binary resolves (``core/sandbox/sandbox_render._npm_package_of``)."""

from __future__ import annotations

import pytest

from core.sandbox import sandbox_render


@pytest.mark.parametrize("path, pkg", [
    ("/u/lib/node_modules/@openai/codex/bin/codex.js", "@openai/codex"),
    ("/u/lib/node_modules/some-cli/bin/x", "some-cli"),
    ("/usr/local/bin/claude", ""),
])
def test_the_package_is_read_from_the_resolved_binary(path, pkg):
    assert sandbox_render._npm_package_of(path) == pkg
