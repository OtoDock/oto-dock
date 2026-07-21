"""Tests for the per-tool MCP cost evaluator (`services/mcp/mcp_cost_engine.py`).

The engine is a pure function — no DB, no async — so these tests don't
need the temp_db fixture. They build CostsBlock fixtures inline and call
`evaluate()` directly.
"""

import pytest

from services.mcp.mcp_cost_engine import evaluate
from services.mcp.mcp_registry import CostsBlock, CostRule


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures — image-gen-mcp's actual rule set so tests double as a regression
# guard for the manifest.
# ═══════════════════════════════════════════════════════════════════════════


def _image_gen_costs() -> CostsBlock:
    return CostsBlock(
        currency="USD",
        provider="image-gen",
        rules=[
            CostRule(tool="generate_image",
                     match={"model": "nano-banana", "quality": "high"},
                     amount=0.134, multiply_by="num_images"),
            CostRule(tool="generate_image",
                     match={"model": "nano-banana"},
                     amount=0.039, multiply_by="num_images"),
            CostRule(tool="generate_image",
                     match={"model": "gpt-image", "quality": "high"},
                     amount=0.08, multiply_by="num_images"),
            CostRule(tool="generate_image",
                     match={"model": "gpt-image"},
                     amount=0.04, multiply_by="num_images"),
            CostRule(tool="edit_image_ai",
                     match={"model": "gpt-image"},
                     amount=0.04),
            CostRule(tool="edit_image_ai",
                     amount=0.039),
        ],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Basic matching
# ═══════════════════════════════════════════════════════════════════════════


class TestEvaluate:
    def test_no_costs_block_returns_none(self):
        assert evaluate("any", "any_tool", {}, None) is None

    def test_no_rules_returns_none(self):
        block = CostsBlock(currency="USD", provider="x", rules=[])
        assert evaluate("any", "any_tool", {}, block) is None

    def test_tool_name_mismatch_returns_none(self):
        block = CostsBlock(currency="USD", provider="x", rules=[
            CostRule(tool="foo", amount=1.0),
        ])
        assert evaluate("any", "bar", {}, block) is None

    def test_catch_all_matches_when_no_match_keys(self):
        block = CostsBlock(currency="USD", provider="x", rules=[
            CostRule(tool="foo", amount=2.5),
        ])
        hit = evaluate("any", "foo", {"unrelated": "yes"}, block)
        assert hit is not None
        assert hit.amount == 2.5
        assert hit.provider == "x"
        assert hit.model == ""
        assert hit.currency == "USD"

    def test_match_subset_required(self):
        block = CostsBlock(currency="USD", provider="x", rules=[
            CostRule(tool="foo", match={"a": 1, "b": 2}, amount=1.0),
        ])
        # Missing key b → no match
        assert evaluate("any", "foo", {"a": 1}, block) is None
        # Wrong value → no match
        assert evaluate("any", "foo", {"a": 1, "b": 3}, block) is None
        # Exact subset → match
        hit = evaluate("any", "foo", {"a": 1, "b": 2, "c": 99}, block)
        assert hit is not None and hit.amount == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# First-match-wins ordering — image-gen's actual rule set
# ═══════════════════════════════════════════════════════════════════════════


class TestFirstMatchWins:
    @pytest.fixture
    def block(self):
        return _image_gen_costs()

    def test_nano_banana_high(self, block):
        hit = evaluate("image-gen", "generate_image",
                       {"model": "nano-banana", "quality": "high"}, block)
        assert hit is not None
        assert hit.amount == 0.134
        assert hit.model == "nano-banana"

    def test_nano_banana_standard_via_catch_all(self, block):
        hit = evaluate("image-gen", "generate_image",
                       {"model": "nano-banana", "quality": "standard"}, block)
        assert hit is not None
        assert hit.amount == 0.039
        assert hit.model == "nano-banana"

    def test_nano_banana_no_quality_arg_falls_through(self, block):
        # LLM omitted quality — relies on the catch-all rule (which is the
        # documented author convention).
        hit = evaluate("image-gen", "generate_image", {"model": "nano-banana"}, block)
        assert hit is not None
        assert hit.amount == 0.039

    def test_gpt_image_high(self, block):
        hit = evaluate("image-gen", "generate_image",
                       {"model": "gpt-image", "quality": "high"}, block)
        assert hit is not None
        assert hit.amount == 0.08
        assert hit.model == "gpt-image"

    def test_gpt_image_standard(self, block):
        hit = evaluate("image-gen", "generate_image",
                       {"model": "gpt-image", "quality": "standard"}, block)
        assert hit is not None
        assert hit.amount == 0.04

    def test_edit_gpt_image(self, block):
        hit = evaluate("image-gen", "edit_image_ai", {"model": "gpt-image"}, block)
        assert hit is not None
        assert hit.amount == 0.04
        assert hit.model == "gpt-image"

    def test_edit_no_model_falls_to_default(self, block):
        # LLM omitted model — catches the trailing catch-all (nano-banana edit).
        hit = evaluate("image-gen", "edit_image_ai", {}, block)
        assert hit is not None
        assert hit.amount == 0.039
        assert hit.model == ""


# ═══════════════════════════════════════════════════════════════════════════
# multiply_by
# ═══════════════════════════════════════════════════════════════════════════


class TestMultiplyBy:
    @pytest.fixture
    def block(self):
        return _image_gen_costs()

    def test_three_images(self, block):
        hit = evaluate("image-gen", "generate_image",
                       {"model": "nano-banana", "num_images": 3}, block)
        assert hit is not None
        assert hit.amount == pytest.approx(0.039 * 3)

    def test_missing_arg_defaults_to_one(self, block):
        # `num_images` omitted — multiplier falls back to 1, not 0 and not match-failure.
        hit = evaluate("image-gen", "generate_image", {"model": "nano-banana"}, block)
        assert hit is not None
        assert hit.amount == 0.039

    def test_garbage_arg_falls_back_to_one(self, block):
        # Hostile/buggy input — should not raise, should not zero out.
        hit = evaluate("image-gen", "generate_image",
                       {"model": "nano-banana", "num_images": "not-an-int"}, block)
        assert hit is not None
        assert hit.amount == 0.039

    def test_zero_or_negative_clamped_to_one(self, block):
        hit = evaluate("image-gen", "generate_image",
                       {"model": "nano-banana", "num_images": 0}, block)
        assert hit is not None
        assert hit.amount == 0.039


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_tool_input_none_returns_none_not_catch_all(self):
        """If TOOL_INPUT didn't arrive, we MUST NOT silently match a catch-all
        rule — that would charge for a tool whose args we never observed.
        """
        block = CostsBlock(currency="USD", provider="x", rules=[
            CostRule(tool="generate_image", amount=10.0),  # catch-all
        ])
        assert evaluate("image-gen", "generate_image", None, block) is None

    def test_wildcard_tool_matches_any(self):
        block = CostsBlock(currency="USD", provider="x", rules=[
            CostRule(tool="*", amount=0.5),
        ])
        hit = evaluate("any", "anything_at_all", {}, block)
        assert hit is not None
        assert hit.amount == 0.5

    def test_rounded_to_six_decimals(self):
        block = CostsBlock(currency="USD", provider="x", rules=[
            CostRule(tool="foo", amount=0.001, multiply_by="n"),
        ])
        hit = evaluate("any", "foo", {"n": 1}, block)
        assert hit is not None
        # 0.001 * 1 = 0.001 exactly
        assert hit.amount == 0.001


# ═══════════════════════════════════════════════════════════════════════════
# List-valued match (framework feature): one rule covers several arg values
# sharing a price tier. Generic — not image-gen-specific.
# ═══════════════════════════════════════════════════════════════════════════


class TestListMatch:
    def _block(self) -> CostsBlock:
        # Most specific first (first-match-wins).
        return CostsBlock(currency="USD", provider="x", rules=[
            CostRule(tool="gen", match={"q": "high", "ratio": ["16:9", "9:16"]}, amount=0.25),
            CostRule(tool="gen", match={"q": "high"}, amount=0.167),
            CostRule(tool="gen", match={"ratio": ["16:9", "9:16"]}, amount=0.063),
            CostRule(tool="gen", amount=0.042),
        ])

    def test_membership_hits_the_tier(self):
        assert evaluate("m", "gen", {"q": "high", "ratio": "16:9"}, self._block()).amount == 0.25

    def test_value_outside_list_falls_through(self):
        # "1:1" not in the list → next matching rule (high, no ratio) wins.
        assert evaluate("m", "gen", {"q": "high", "ratio": "1:1"}, self._block()).amount == 0.167

    def test_absent_key_never_matches_a_list(self):
        # ratio omitted → the list rule can't match → square default tier.
        assert evaluate("m", "gen", {"q": "high"}, self._block()).amount == 0.167

    def test_list_rule_without_other_constraints(self):
        assert evaluate("m", "gen", {"ratio": "9:16"}, self._block()).amount == 0.063

    def test_default_when_no_rule_matches(self):
        assert evaluate("m", "gen", {"ratio": "1:1"}, self._block()).amount == 0.042

    def test_scalar_match_still_exact(self):
        # Backward-compat: a scalar match value is still strict equality.
        block = CostsBlock(currency="USD", provider="x", rules=[
            CostRule(tool="t", match={"k": "v"}, amount=1.0),
        ])
        assert evaluate("m", "t", {"k": "v"}, block).amount == 1.0
        assert evaluate("m", "t", {"k": "other"}, block) is None
