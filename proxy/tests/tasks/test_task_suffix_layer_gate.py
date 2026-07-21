"""Tests for ``_build_task_agent_suffix`` layer-gating.

The subagent paragraph only ships on layers that expose the Agent tool
(Claude Code CLI + Codex CLI). Direct LLM has no Agent tool, so the
paragraph is dropped to avoid telling the model to call a non-existent
tool. There is NO agent-emitted completion marker anymore — task completion
is detected deterministically (the turn's `result` event + the
SubagentRegistry's all-done state), so no layer should mention [JOB_DONE].
"""

from __future__ import annotations

import sys

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core.config.task_config_builder import _build_task_agent_suffix


def test_cli_includes_subagent_paragraph():
    suffix = _build_task_agent_suffix("claude-code-cli")
    assert "## Background Task Agent Rules" in suffix
    assert "### Subagent Rules" in suffix
    assert "Agent tool" in suffix


def test_codex_includes_subagent_paragraph():
    suffix = _build_task_agent_suffix("codex-cli")
    assert "### Subagent Rules" in suffix


def test_direct_llm_omits_subagent_paragraph():
    """Direct LLM has no Agent tool — paragraph dropped."""
    suffix = _build_task_agent_suffix("direct-llm")
    assert "## Background Task Agent Rules" in suffix
    assert "### Subagent Rules" not in suffix
    assert "Agent tool" not in suffix


def test_unknown_layer_omits_subagent_paragraph():
    """Belt-and-braces — unknown layers default to no subagent block."""
    suffix = _build_task_agent_suffix("future-layer")
    assert "### Subagent Rules" not in suffix


def test_empty_layer_omits_subagent_paragraph():
    suffix = _build_task_agent_suffix("")
    assert "### Subagent Rules" not in suffix


def test_no_job_done_marker_on_any_layer():
    """The [JOB_DONE] completion marker was removed — no layer emits it."""
    for layer in ("claude-code-cli", "codex-cli", "direct-llm", "future-layer", ""):
        assert "[JOB_DONE]" not in _build_task_agent_suffix(layer)
