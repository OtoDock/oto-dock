"""The boot check of the host's engine CLIs against their pins
(``core/sandbox/sandbox.cli_version_preflight``): a drift and a CLI that
prints no version (npm 12's stub of a package whose install step it
skipped) are both reported, with the install command that fixes them."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from core.sandbox import sandbox


def _layers(bin_path: str, pin: str) -> dict:
    layer = SimpleNamespace(
        capabilities=SimpleNamespace(display_name="Claude Code",
                                     runtime=SimpleNamespace(binary="claude")),
        cli_binary_path=lambda: bin_path,
        pinned_cli_version=lambda: pin,
    )
    return {"claude-code-cli": layer}


@pytest.fixture
def npm_cli(tmp_path):
    real = tmp_path / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    real.parent.mkdir(parents=True)
    real.write_text("")
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(real)
    return str(link)


def _run(monkeypatch, exe: str, printed: str) -> None:
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "get_all_layers", lambda: _layers(exe, "2.1.300"))
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: name)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=printed, returncode=0))
    sandbox.cli_version_preflight()


def test_a_cli_printing_no_version_is_reported_with_the_allow_scripts_hint(monkeypatch, caplog, npm_cli):
    _run(monkeypatch, npm_cli, "")
    assert "printed no version" in caplog.text
    assert ("npm i -g --allow-scripts=@anthropic-ai/claude-code "
            "@anthropic-ai/claude-code@2.1.300") in caplog.text


def test_a_drift_names_the_package(monkeypatch, caplog, npm_cli):
    _run(monkeypatch, npm_cli, "2.1.299 (Claude Code)")
    assert "CLI pin DRIFT: Claude Code is 2.1.299 but VERSIONS.md pins 2.1.300" in caplog.text
    assert "--allow-scripts=@anthropic-ai/claude-code" in caplog.text


def test_the_pinned_version_is_quiet(monkeypatch, caplog, npm_cli):
    _run(monkeypatch, npm_cli, "2.1.300 (Claude Code)")
    assert "CLI pin" not in caplog.text


@pytest.mark.parametrize("path, pkg", [
    ("/u/lib/node_modules/@openai/codex/bin/codex.js", "@openai/codex"),
    ("/u/lib/node_modules/some-cli/bin/x", "some-cli"),
    ("/usr/local/bin/claude", ""),
])
def test_the_package_is_read_from_the_resolved_binary(path, pkg):
    assert sandbox._npm_package_of(path) == pkg
