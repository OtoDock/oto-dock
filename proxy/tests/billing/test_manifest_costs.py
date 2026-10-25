"""Tests for the manifest `costs` block parser/validator
(`services/mcp_registry._parse_costs_block`).

Strict validation is what protects against community MCPs shipping garbage
that silently overcharges (or undercharges) at runtime — every test here
is a contract guarantee for community MCP authors.
"""

import pytest

from services.mcp.mcp_registry import CostsBlock, _parse_costs_block

# ═══════════════════════════════════════════════════════════════════════════
# Happy path
# ═══════════════════════════════════════════════════════════════════════════


class TestValidCosts:
    def test_returns_none_when_omitted(self):
        assert _parse_costs_block(None, "any") is None

    def test_minimal_valid_block(self):
        block = _parse_costs_block({
            "currency": "USD",
            "provider": "image-gen",
            "rules": [{"tool": "foo", "amount": 0.05}],
        }, "any")
        assert isinstance(block, CostsBlock)
        assert block.currency == "USD"
        assert block.provider == "image-gen"
        assert len(block.rules) == 1
        assert block.rules[0].tool == "foo"
        assert block.rules[0].amount == 0.05
        assert block.rules[0].match == {}
        assert block.rules[0].multiply_by == ""

    def test_image_gen_full_block(self):
        """Round-trip the actual image-gen rule set."""
        block = _parse_costs_block({
            "currency": "USD",
            "provider": "image-gen",
            "rules": [
                {"tool": "generate_image", "match": {"model": "nano-banana", "quality": "high"},
                 "amount": 0.134, "multiply_by": "num_images"},
                {"tool": "generate_image", "match": {"model": "nano-banana"},
                 "amount": 0.039, "multiply_by": "num_images"},
                {"tool": "edit_image_ai", "amount": 0.039},
            ],
        }, "image-gen-mcp")
        assert len(block.rules) == 3
        assert block.rules[0].multiply_by == "num_images"
        assert block.rules[2].match == {}


# ═══════════════════════════════════════════════════════════════════════════
# Validator failures — every one of these must raise ValueError so the
# install pipeline rejects the upload with a 400.
# ═══════════════════════════════════════════════════════════════════════════


class TestInvalidCosts:
    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be an object"):
            _parse_costs_block("nope", "x")

    def test_missing_currency(self):
        with pytest.raises(ValueError, match="currency"):
            _parse_costs_block({"provider": "x", "rules": [{"tool": "f", "amount": 1}]}, "x")

    def test_unknown_currency(self):
        with pytest.raises(ValueError, match="currency"):
            _parse_costs_block({"currency": "EUR", "provider": "x",
                                "rules": [{"tool": "f", "amount": 1}]}, "x")

    def test_missing_provider(self):
        with pytest.raises(ValueError, match="provider"):
            _parse_costs_block({"currency": "USD",
                                "rules": [{"tool": "f", "amount": 1}]}, "x")

    def test_empty_provider(self):
        with pytest.raises(ValueError, match="provider"):
            _parse_costs_block({"currency": "USD", "provider": "",
                                "rules": [{"tool": "f", "amount": 1}]}, "x")

    def test_missing_rules(self):
        with pytest.raises(ValueError, match="rules"):
            _parse_costs_block({"currency": "USD", "provider": "x"}, "x")

    def test_empty_rules(self):
        with pytest.raises(ValueError, match="rules"):
            _parse_costs_block({"currency": "USD", "provider": "x", "rules": []}, "x")

    def test_rule_not_dict(self):
        with pytest.raises(ValueError, match=r"rules\[0\]"):
            _parse_costs_block({"currency": "USD", "provider": "x",
                                "rules": ["not-a-dict"]}, "x")

    def test_missing_tool(self):
        with pytest.raises(ValueError, match=r"rules\[0\].tool"):
            _parse_costs_block({"currency": "USD", "provider": "x",
                                "rules": [{"amount": 1}]}, "x")

    def test_empty_tool(self):
        with pytest.raises(ValueError, match=r"rules\[0\].tool"):
            _parse_costs_block({"currency": "USD", "provider": "x",
                                "rules": [{"tool": "", "amount": 1}]}, "x")

    def test_negative_amount(self):
        with pytest.raises(ValueError, match=r"rules\[0\].amount"):
            _parse_costs_block({"currency": "USD", "provider": "x",
                                "rules": [{"tool": "f", "amount": -0.1}]}, "x")

    def test_non_numeric_amount(self):
        with pytest.raises(ValueError, match=r"rules\[0\].amount"):
            _parse_costs_block({"currency": "USD", "provider": "x",
                                "rules": [{"tool": "f", "amount": "free"}]}, "x")

    def test_bool_amount_rejected(self):
        # bool is technically a subclass of int — reject explicitly so
        # `True` doesn't silently mean amount=1.
        with pytest.raises(ValueError, match=r"rules\[0\].amount"):
            _parse_costs_block({"currency": "USD", "provider": "x",
                                "rules": [{"tool": "f", "amount": True}]}, "x")

    def test_match_not_object(self):
        with pytest.raises(ValueError, match=r"rules\[0\].match"):
            _parse_costs_block({"currency": "USD", "provider": "x",
                                "rules": [{"tool": "f", "amount": 1, "match": "wrong"}]}, "x")

    def test_multiply_by_not_string(self):
        with pytest.raises(ValueError, match=r"rules\[0\].multiply_by"):
            _parse_costs_block({"currency": "USD", "provider": "x",
                                "rules": [{"tool": "f", "amount": 1, "multiply_by": 5}]}, "x")

    def test_duplicate_rules(self):
        with pytest.raises(ValueError, match="duplicates"):
            _parse_costs_block({"currency": "USD", "provider": "x", "rules": [
                {"tool": "foo", "match": {"a": 1}, "amount": 0.5},
                {"tool": "foo", "match": {"a": 1}, "amount": 0.7},
            ]}, "x")


class TestListMatchValues:
    """Framework: a match value may be a non-empty list of scalars (membership)."""

    def test_list_match_value_parses_and_no_false_dedup(self):
        block = _parse_costs_block({
            "currency": "USD", "provider": "x",
            "rules": [
                {"tool": "gen", "match": {"q": "high", "ratio": ["16:9", "9:16"]}, "amount": 0.25},
                {"tool": "gen", "match": {"ratio": ["16:9", "9:16"]}, "amount": 0.063},
            ],
        }, "x")
        assert block.rules[0].match["ratio"] == ["16:9", "9:16"]
        assert len(block.rules) == 2   # distinct match dicts; hashable dedup key

    def test_identical_list_rules_are_deduped(self):
        with pytest.raises(ValueError, match="duplicates"):
            _parse_costs_block({"currency": "USD", "provider": "x", "rules": [
                {"tool": "f", "match": {"k": ["a", "b"]}, "amount": 1},
                {"tool": "f", "match": {"k": ["a", "b"]}, "amount": 2},
            ]}, "x")

    def test_empty_list_rejected(self):
        with pytest.raises(ValueError, match="non-empty list"):
            _parse_costs_block({"currency": "USD", "provider": "x", "rules": [
                {"tool": "f", "match": {"k": []}, "amount": 1}]}, "x")

    def test_nested_list_rejected(self):
        with pytest.raises(ValueError, match="non-empty list"):
            _parse_costs_block({"currency": "USD", "provider": "x", "rules": [
                {"tool": "f", "match": {"k": [["a"]]}, "amount": 1}]}, "x")

    def test_dict_match_value_rejected(self):
        with pytest.raises(ValueError, match="scalar or a list"):
            _parse_costs_block({"currency": "USD", "provider": "x", "rules": [
                {"tool": "f", "match": {"k": {"a": 1}}, "amount": 1}]}, "x")
