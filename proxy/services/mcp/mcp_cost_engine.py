"""Generic per-tool cost evaluator for MCP manifests.

Pure function with no side effects. The pump calls this at TOOL_RESULT
time with the args the LLM sent (already stashed at TOOL_INPUT). On a hit
it returns a CostHit; the pump adds it to the chat total and to a per-
(provider, model) bucket that becomes one usage_records row per bucket
at turn end.

Manifest schema lives in `services/mcp/mcp_manifest_types.py::CostsBlock`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from services.mcp.mcp_registry import CostRule, CostsBlock

logger = logging.getLogger(__name__)


@dataclass
class CostHit:
    amount: float       # final cost (base × multiplier), rounded to 6 dp
    provider: str       # from manifest costs.provider
    model: str          # from match.get("model", "") — empty for MCPs that don't dispatch on model
    currency: str       # "USD" for v1


def _matches(rule: CostRule, tool_name: str, tool_input: dict) -> bool:
    """Does this rule apply to this call?

    A ``match`` value is either a SCALAR (exact equality) or a LIST (membership) —
    the list form lets one rule cover several arg values that share a price tier
    (e.g. all non-square aspect ratios). An absent key never matches, so a rule
    keyed on an omitted arg falls through to a broader rule (with first-match-wins
    ordering, list the most specific rules first)."""
    if rule.tool != "*" and rule.tool != tool_name:
        return False
    for k, v in rule.match.items():
        if k not in tool_input:
            return False
        if isinstance(v, list):
            if tool_input[k] not in v:
                return False
        elif tool_input[k] != v:
            return False
    return True


def _multiplier(rule: CostRule, tool_input: dict) -> int:
    """Resolve the multiplier from `multiply_by`. Missing/garbage → 1."""
    if not rule.multiply_by:
        return 1
    raw = tool_input.get(rule.multiply_by, 1)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def evaluate(
    mcp_name: str,
    tool_name: str,
    tool_input: dict | None,
    costs_block: CostsBlock | None,
) -> CostHit | None:
    """Return a CostHit if a rule matches, else None.

    `tool_input is None` (translator didn't emit TOOL_INPUT) returns None
    rather than falling through to a catch-all rule — silent catch-all
    matching on missing args is exactly the kind of bug we want to avoid.
    """
    if costs_block is None or not costs_block.rules:
        return None
    if tool_input is None:
        logger.warning(
            "mcp_cost_engine: tool_input missing for %s/%s — skipping cost evaluation",
            mcp_name, tool_name,
        )
        return None

    for rule in costs_block.rules:
        if _matches(rule, tool_name, tool_input):
            mult = _multiplier(rule, tool_input)
            amount = round(rule.amount * mult, 6)
            return CostHit(
                amount=amount,
                provider=costs_block.provider,
                model=str(rule.match.get("model", "")),
                currency=costs_block.currency,
            )
    return None


def find_costs_block_for_tool(tool_name_with_prefix: str) -> tuple[str, str, CostsBlock] | None:
    """Resolve `mcp__{server}__{tool}` to (mcp_name, plain_tool, costs_block).

    Returns None if the prefix doesn't match an MCP, the MCP has no costs
    block, or the tool name can't be parsed. Mirrors the MCP-name lookup
    pattern used by `mcp_output_relocation` in the pump's TOOL_RESULT
    handler.
    """
    if not tool_name_with_prefix.startswith("mcp__"):
        return None
    parts = tool_name_with_prefix.split("__", 2)
    if len(parts) < 3:
        return None
    server_name = parts[1]
    plain_tool = parts[2]
    # Lazy import — avoids a circular import (mcp_registry imports many
    # things that may eventually import this module).
    from services.mcp import mcp_registry
    for n, m in mcp_registry.get_all_manifests().items():
        if (m.server_name or m.name) == server_name and m.costs is not None:
            return (n, plain_tool, m.costs)
    return None
