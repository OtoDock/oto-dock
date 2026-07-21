"""Failed tool calls must not be charged by the MCP cost engine.

The PostToolUse forwarder derives an `is_error` flag (structured MCP error
flag, or an "Error…" result for MCPs that return failures as plain text, e.g.
image-gen). The proxy carries it through the hooks API onto the tool block, and
the stream pump's TOOL_RESULT handler skips the cost evaluation when it's set.

These tests cover the derivation (the heart of the fix) and the request model
that transports it.
"""

from __future__ import annotations

import importlib.util

from tests._paths import PROXY_DIR
_FWD = PROXY_DIR / "hooks" / "tool_result_forwarder.py"
_spec = importlib.util.spec_from_file_location("tool_result_forwarder", _FWD)
forwarder = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(forwarder)


class TestIsErrorDerivation:
    def test_structured_is_error_flag(self):
        assert forwarder._is_error_result({"is_error": True, "content": "boom"}, "ok") is True

    def test_structured_camelcase_isError(self):
        # MCP protocol uses camelCase isError; accept both spellings.
        assert forwarder._is_error_result({"isError": True}, "ok") is True

    def test_structured_flag_wins_even_with_ok_summary(self):
        assert forwarder._is_error_result({"is_error": True}, "ok") is True

    def test_error_text_summary_without_structured_flag(self):
        # image-gen swallows the exception and returns the error as plain text,
        # so there's no structured flag — the summary classifies it instead.
        assert forwarder._is_error_result(
            {"content": "Error generating image: 429 quota exceeded"},
            "error: Error generating image: 429 quota exceeded",
        ) is True

    def test_success_not_flagged(self):
        assert forwarder._is_error_result({"content": "saved preview"}, "ok") is False

    def test_success_with_line_count_summary(self):
        assert forwarder._is_error_result({"content": "x\ny"}, "5 lines") is False

    def test_non_dict_result_with_ok_summary(self):
        assert forwarder._is_error_result("plain string", "ok") is False


class TestSummaryFeedsDerivation:
    """The image-gen 429 case end-to-end through summary → is_error."""

    def test_failed_image_gen_is_flagged(self):
        text = "Error generating image: 429 Too Many Requests"
        summary = forwarder._extract_summary("mcp__image-gen__generate_image_ai", {}, text)
        assert summary.lower().startswith("error")
        assert forwarder._is_error_result({"content": text}, summary) is True

    def test_successful_image_gen_is_not_flagged(self):
        text = "Generated image: preview attached"
        summary = forwarder._extract_summary("mcp__image-gen__generate_image_ai", {}, text)
        assert summary == "ok"
        assert forwarder._is_error_result({"content": text}, summary) is False


class TestHookRequestCarriesFlag:
    def test_default_false_and_explicit_true(self):
        from api.hooks.hooks import HookToolResultRequest

        default = HookToolResultRequest(session_id="s", tool_name="t", summary="ok")
        assert default.is_error is False

        flagged = HookToolResultRequest(
            session_id="s", tool_name="t", summary="error: boom", is_error=True,
        )
        assert flagged.is_error is True
