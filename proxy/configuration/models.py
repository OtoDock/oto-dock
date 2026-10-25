"""The model registry: the builtin models, their tiers, the succession of
retired ids, pricing, and the model a session or a run resolves to.

``config`` re-exports every public name. A function here calls its siblings
in this module, so a test whose code reaches a name through another function
of this module patches it here (``resolve_run_model`` reads ``get_cli_model``
here); a caller that reads ``config.<name>`` sees a patch on ``config``.
"""

import contextlib

import psycopg

# NOTE: ANTHROPIC_API_KEY / ANTHROPIC_MODEL / CLAUDE_MODEL env vars have been
# removed. API keys flow through the subscription pool (admin-managed via
# Execution Layers page); model selection flows through agent.default_model
# with a DB-driven fallback to the first enabled model for the agent's
# execution_path (see resolve_agent_model() below). No more Anthropic-specific
# platform-level defaults — the platform is provider-agnostic.


# Default effort level for adaptive reasoning (CLI --effort flag).
# Applied at session startup.
# Valid platform levels: "low", "medium", "high", "xhigh", "max" — plus
# "ultra" on the models that support it (per-model, see below).
#   - "max" is the model's reasoning ceiling on every layer but Codex 5.6+:
#     the `claude` CLI's --effort flag accepts only low/medium/high/xhigh/max.
#   - "xhigh" is a distinct intermediate level between "high" and "max", only
#     supported on Opus 4.7+ (Anthropic, incl. 4.8) and the OpenAI gpt-5 family.
#     Models without support silently fall back to "max" at wire time via the
#     failsafe in each execution layer — see get_model_supports_xhigh().
#   - "ultra" (Codex: gpt-5.6 Sol/Terra and GPT-6 Astra) is max reasoning PLUS Codex-native
#     proactive multi-agent orchestration (parallel sub-agent workstreams inside
#     one turn). Offered per-model via supports_ultra / get_model_supports_ultra()
#     and mapped in core/layers/codex/helpers.map_effort_to_codex; every other
#     layer/model clamps a stored "ultra" to its own ceiling ("max"/"xhigh")
#     so it can never reach a CLI or API that rejects it.
#   - "ultracode" (Opus 5) is deliberately NOT in this list. It is a Claude
#     Code *session setting* (`"ultracode": true` via --settings), not a wire
#     effort value: it sends "xhigh" to the model AND turns on dynamic
#     multi-agent workflow orchestration in the CLI harness. Modelling it would
#     be a separate per-session CLI toggle, not a seventh --effort level.
# The platform values are passed through as-is to Anthropic (no translation).
# OpenAI/Codex adapters map the platform scale onto their own native values.
DEFAULT_EFFORT_LEVEL = "high"


# ---------------------------------------------------------------------------
# Central model registry — single source of truth for model metadata.
# Pricing is per 1M tokens: (input, output, cache_write, cache_read).
# Update this when Anthropic changes pricing or adds new models.
# ---------------------------------------------------------------------------
MODEL_REGISTRY: dict[str, dict] = {
    # Fable 5.1 (2026-09-01) replaces the Fable 5 builtin at the SAME base
    # price ($10/$50) with cheaper cache reads (2.5% of input vs 10% —
    # platform.claude.com models overview), so the swap is lossless: a
    # one-time boot remap moves fable-5-pinned agents/tasks/chats to
    # claude-fable-5-1 (MODEL_SUCCESSORS, walked at startup); sync_builtin_models
    # retires the builtin row so pickers stop offering Fable 5 (still served
    # by Anthropic as a legacy model — admins can re-add claude-fable-5 as a
    # custom model).
    "claude-fable-5-1": {
        "label": "Fable 5.1",
        "provider": "anthropic",
        "context_window": 1_000_000,   # 1M native window (as Fable 5)
        "pricing": (10.0, 50.0, 12.50, 0.25),  # cache write 1.25x input; reads 2.5% of input (5.1 cut)
        # CLI-only: Fable's pricing is above Opus-tier, so keeping it off
        # direct-llm bounds accidental spend on the hosted (credit-metered) path.
        # Fable's safety classifiers can refuse a request mid-run; Claude Code
        # ships a built-in automatic fallback to its default Opus for that
        # case (Opus 5.5 since Claude Code 2.1.280; interactive TUI only).
        "layers": ["claude-code-cli"],
        "server_tools": True,   # supports Anthropic web_search/web_fetch
        "supports_reasoning": True,   # thinking is ALWAYS on for Fable (adaptive)
        "supports_xhigh": True,
        "tier": 1,
        "good_at": "complex coding, architecture, subtle bugs, judgement calls and long autonomous work",
    },
    # Opus 5.5 (API id claude-opus-5-5, no alias) replaces the retired Opus 5
    # builtin (which had itself replaced Opus 4.8) at a LOWER price on every
    # element of the tuple with the same capabilities
    # (1M, xhigh, server tools, adaptive thinking always on), so the swap is
    # lossless: a boot remap moves Opus 5-pinned agents/tasks/chats to
    # claude-opus-5-5 (MODEL_SUCCESSORS, walked at startup; Opus 4.8 pins
    # follow the chain); sync_builtin_models retires the builtin row so
    # pickers stop offering Opus 5 (Anthropic lists it as legacy; retirement
    # not announced). Admins can re-add claude-opus-5 as a custom model — the
    # walk then leaves pins on it alone. Prices per 1M from the
    # platform.claude.com models overview at the swap: $4 in, $20 out, $5 cache write
    # (1.25x input), $0.20 cache read (5% of input). Needs Claude Code
    # >= 2.1.280 (VERSIONS.md). Fable 5.1 stays the tier-1 offer above it.
    "claude-opus-5-5": {
        "label": "Opus 5.5",
        "provider": "anthropic",
        "context_window": 1_000_000,   # native 1M window, the API default (no beta header)
        "pricing": (4.0, 20.0, 5.0, 0.20),  # (input, output, cache_write, cache_read) per 1M
        "layers": ["claude-code-cli"],
        "server_tools": True,   # supports Anthropic web_search/web_fetch
        "supports_reasoning": True,
        "supports_xhigh": True,   # keeps the xhigh level (low/medium/high/xhigh/max). NOTE: "ultracode" is NOT an effort level — it's a CLI session setting that pairs xhigh with workflow orchestration; see get_model_supports_xhigh() and DEFAULT_EFFORT_LEVEL.
        "tier": 2,
        "good_at": "substantial, well-scoped coding and analysis; complex or open-ended work belongs on the frontier",
    },
    "claude-sonnet-5-5": {
        "label": "Sonnet 5.5",
        "provider": "anthropic",
        "context_window": 1_000_000,   # native 1M window (no [1m] suffix needed)
        "pricing": (2.0, 10.0, 2.50, 0.10),  # Sonnet 5's tuple but cache reads at 0.05x input (Anthropic's price page; Claude Code 2.1.296 prices them so too). 5.5 replaced Sonnet 5 (MODEL_SUCCESSORS; needs Claude Code >= 2.1.284 and its hosted-relay row, it serves direct-llm too)
        "layers": ["claude-code-cli", "direct-llm"],
        "server_tools": True,
        "supports_reasoning": True,   # adaptive thinking on by default; temperature/top_p never sent
        "supports_xhigh": True,   # xhigh and max, as Sonnet 5
        "tier": 3,
        "good_at": "drafting, summaries and bounded analysis",
    },
    # Haiku 5.5 (API id claude-haiku-5-5, 2026-10-07) replaces the retired
    # Haiku 4.5 builtin at a tenth of its price for a prompt up to 100,000
    # tokens and half of it above: the one current model Anthropic prices by
    # prompt length (input plus cache reads and writes, each request on its
    # own), input, cache and output all x5 past the line — ``long_context``,
    # applied by price_usage below and mirrored by the hosted relay's row.
    # Its capabilities are a superset of Haiku 4.5's (1M window, adaptive
    # thinking at every effort level, programmatic tool calling), so the swap
    # is lossless: the boot walk moves Haiku 4.5 pins onto it
    # (MODEL_SUCCESSORS; an admin who wants 4.5 re-adds it as a custom model
    # first and the walk leaves those pins alone). It serves Claude Code too,
    # whose own default Haiku it is since 2.1.293 (2.1.296 counts its tokens
    # right; an older CLI runs it with a 200K window at a fallback price), and
    # it is the Anthropic title model. Prices per 1M from the
    # platform.claude.com price page at the swap.
    "claude-haiku-5-5": {
        "label": "Haiku 5.5",
        "provider": "anthropic",
        "context_window": 1_000_000,   # native 1M window
        "pricing": (0.10, 0.50, 0.125, 0.01),  # (input, output, cache_write, cache_read) per 1M, a prompt up to 100K tokens
        "long_context": (100_000, 5.0, 5.0),   # (prompt tokens over, input and cache multiplier, output multiplier)
        "layers": ["claude-code-cli", "direct-llm"],
        "server_tools": True,   # programmatic tool calling and the web tools, unlike Haiku 4.5
        "supports_reasoning": True,   # adaptive thinking, default effort medium
        "supports_xhigh": True,   # low to max, xhigh included
        "tier": 4,
        "good_at": "high-volume routine work",
    },
    # Admins add further models (Ollama, LM Studio, more Groq/OpenAI, …) through
    # the admin discover-models UI; the entries below are the shipped builtins.

    # --- OpenAI models ---
    # All gpt-5 family entries set supports_xhigh: True. Effort mapping:
    # pre-5.6 models top out at wire "xhigh" (platform "max" clamps there);
    # the GPT-5.6 family and GPT-6 add wire "max" — and "ultra" (Sol/Terra
    # and Astra, supports_ultra below): Codex-native proactive multi-agent
    # orchestration on top of max reasoning (Astra delegates at xhigh) — see
    # core/layers/codex/helpers.map_effort_to_codex.
    #
    # GPT-6.1 Sol (API id gpt-6.1-sol) replaces GPT-6 Sol, which
    # had replaced GPT-5.6 Sol on 2026-09-24, and GPT-6 Luna replaces GPT-5.6
    # Luna (2026-09-24): each at a LOWER or equal price on every element of
    # the tuple with the same effort ceilings (Sol: max + ultra; Luna: max,
    # no ultra), so the swaps are lossless MODEL_SUCCESSORS entries and the
    # chain gpt-5.6-sol → gpt-6-sol → gpt-6.1-sol ends on 6.1. GPT-5.6 Terra
    # stays (no cheaper same-capability successor on direct-llm). Prices per
    # 1M from developers.openai.com/api/docs/pricing at each swap, permanent.
    # Cache WRITES cost 1.25x the uncached input rate, cache reads 95% off on
    # 6.1 Sol (90% before) — reflected in the tuples. Codex 0.160.0 bundles
    # all three (VERSIONS.md); gpt-6.1-sol is its own default (priority 1,
    # default effort low, ultra delegating at xhigh) and is ABSENT from the
    # 0.156.1 catalog, so a Codex still on that pin refuses the id.
    # Context: the GPT-6 and 5.6 models are 1.05M (922k input + 128k output);
    # input beyond 272k bills the WHOLE request at the long-context tier
    # (2x input / 1.5x output), the ``long_context`` tuple on every row:
    # price_usage multiplies once a request's prompt passes the line. The
    # registry window drives the direct-llm layer's context management and
    # the capability display (until 2026-10-13 it sat at the 272k line so a
    # Direct LLM chat never crossed into the tier). codex-cli ignores it: the
    # daemon reports the LIVE modelContextWindow per turn, OpenAI's catalog
    # per Codex version (258k on 0.160.0, 500k for Sol on 0.162.1), so the
    # headless gauge shows the account's real window.
    # Agents still PINNED to a retired id with no successor (gpt-5.5) keep
    # running (the CLI supports it; pricing falls back to the provider
    # default) — sync_builtin_models retires the builtin row so pickers
    # stop offering it.
    "gpt-6.1-sol": {
        "label": "GPT-6.1 Sol",
        "provider": "openai",
        "context_window": 1_050_000,
        "long_context": (272_000, 2.0, 1.5),   # (prompt tokens over, input x, output x)
        "pricing": (2.0, 10.0, 2.50, 0.10),  # per 1M: (input, output, cache_write, cache_read)
        "layers": ["codex-cli"],   # codex-cli only until the hosted relay prices it
        "supports_reasoning": True,
        "supports_xhigh": True,
        "supports_ultra": True,   # Codex "ultra": proactive multi-agent on top of xhigh reasoning
        "tier": 2,
        "good_at": "substantial, well-scoped coding and analysis; complex or open-ended work belongs on the frontier",
    },
    # GPT-6 Astra (2026-09-03; API id gpt-6-astra, no alias, no snapshots): a
    # NEW model, not a rename of any row — no MODEL_SUCCESSORS entry points
    # at it. OpenAI's flagship and Codex's own bundled default since 0.153.4.
    # The platform's codex-cli default is DECLARED (core/layers/codex
    # ModelPolicy.default_model = GPT-6.1 Sol, tier 2): Astra costs 5× the
    # Sol tier on an API key, so a frontier model stays a deliberate choice —
    # registry order no longer decides the "Auto" default.
    # Context: the same 1.05M window and 272k tier as the 5.6 family ($20 /
    # $75 past the line). Prices per 1M verified
    # 2026-09-08 (developers.openai.com/api/docs/models/gpt-6-astra):
    # $10 in, $50 out, $12.50 cache write, $1 cached read. codex-cli only for
    # now: the hosted relay prices per model and has no Astra row yet, so a
    # direct-llm listing would fail hosted installs — add the layer with the
    # relay row. Reasoning: low/medium/high/xhigh/max on the API; Codex adds
    # "ultra" (delegation at xhigh — multi_agent_reasoning_effort — not max).
    "gpt-6-astra": {
        "label": "GPT-6 Astra",
        "provider": "openai",
        "context_window": 1_050_000,
        "long_context": (272_000, 2.0, 1.5),
        "pricing": (10.0, 50.0, 12.50, 1.00),  # per 1M: (input, output, cache_write, cache_read)
        "layers": ["codex-cli"],
        "supports_reasoning": True,
        "supports_xhigh": True,
        "supports_ultra": True,   # Codex "ultra": proactive multi-agent on top of xhigh reasoning
        "tier": 1,
        "good_at": "complex coding, architecture, subtle bugs, judgement calls and long autonomous work",
    },
    "gpt-5.6-terra": {
        "label": "GPT-5.6 Terra",
        "provider": "openai",
        "context_window": 1_050_000,
        "long_context": (272_000, 2.0, 1.5),
        "pricing": (2.0, 12.0, 2.50, 0.20),  # 2026-07-30 cut ($2.50/$15 → $2/$12), permanent
        "layers": ["codex-cli", "direct-llm"],
        "server_tools": True,   # OpenAI's built-in web_search (Responses API, direct-llm)
        "supports_reasoning": True,
        "supports_xhigh": True,
        "supports_ultra": True,   # codex-cli only — the dashboard offers Ultra
                                  # solely when the codex engine serves the model
        "tier": 3,
        "good_at": "drafting, summaries and bounded analysis",
    },
    # GPT-6 Luna is on direct-llm too: the hosted relay prices per model and
    # rejects unknown ids, so its relay row (otodock-commercial
    # relay_vendors.py) must be served BEFORE an OSS release lists a new
    # direct-llm builtin — a direct-llm row here without one fails every
    # hosted install's titles and Luna chats.
    "gpt-6-luna": {
        "label": "GPT-6 Luna",
        "provider": "openai",
        "context_window": 1_050_000,
        "long_context": (272_000, 2.0, 1.5),
        "pricing": (0.10, 0.50, 0.125, 0.01),  # per 1M: (input, output, cache_write, cache_read)
        "layers": ["codex-cli", "direct-llm"],
        "server_tools": True,   # OpenAI's built-in web_search (Responses API, direct-llm)
        "supports_reasoning": True,
        "supports_xhigh": True,
        # No supports_ultra: OpenAI's own model manifest caps Luna (the fast/
        # cheap tier) at "max" — ultra is a Sol/Terra/Astra capability.
        "tier": 4,
        "good_at": "high-volume routine work",
    },
    # (Older GPT-5.x builtins — gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex
    # — were retired with the 5.6 family, 5.6 Sol / 5.6 Luna with the GPT-6
    # pair, GPT-6 Sol with 6.1 Sol. Pins with a successor move at boot; the
    # others keep running; admins can re-add any as a custom model.)

    # --- Groq models (direct-llm; OpenAI-compatible API) ---
    # gpt-oss-120b is GroqCloud production; qwen3.6-27b is Groq "preview" (kept
    # anyway: it's Groq's recommended fast general-purpose tier). supports_reasoning
    # per model (gpt-oss: yes; qwen3.6 is a hybrid thinker the GroqAdapter pins to
    # non-thinking via reasoning_effort="none" — see openai_compat_adapter.py).
    # No supports_xhigh (Groq has no xhigh tier). Admins can add more Groq /
    # Ollama / etc. models dynamically via the admin discover-models UI.
    # Cache pricing: Groq's automatic prompt caching bills cached input at a
    # 50% discount (writes free) and reports it OpenAI-style via
    # prompt_tokens_details.cached_tokens — cache_read = input/2 keeps the
    # cost rows exact whenever Groq reports a hit (models it doesn't cache
    # simply never report one).
    "qwen/qwen3.6-27b": {
        "label": "Qwen3.6 27B",
        "provider": "groq",
        "context_window": 131_072,
        "pricing": (0.60, 3.00, 0.0, 0.30),
        "layers": ["direct-llm"],
        "tier": 4,
        "good_at": "general-purpose answers served on Groq",
    },
    "openai/gpt-oss-120b": {
        "label": "GPT-OSS 120B",
        "provider": "groq",
        "context_window": 131_072,
        "pricing": (0.15, 0.60, 0.0, 0.075),
        "layers": ["direct-llm"],
        "supports_reasoning": True,
        "tier": 4,
        "good_at": "open-weight answers served on Groq",
    },
}

# Capability tiers, most capable first. A tier is a judgement about which
# offered model an agent should pick for a job (a curated table shipped with
# the registry, never fetched: installs on private networks must give the
# same answer as everyone else). Builtins carry theirs on the entry above;
# admin-added models are untiered until an admin tags them.
MODEL_TIER_LABELS: dict[int, str] = {
    1: "frontier",
    2: "strong",
    3: "balanced",
    4: "fast",
}
MODEL_GOOD_AT_MAX_CHARS = 120


def get_model_tier(model: str) -> tuple[int | None, str, str]:
    """``(tier, tier_label, good_at)`` for a model id: registry first, then
    the first tiered ``execution_layer_models`` row for the id (a custom
    model tagged by an admin), else ``(None, "", "")`` = untiered."""
    entry = MODEL_REGISTRY.get(model)
    if entry:
        tier = entry.get("tier")
        return tier, MODEL_TIER_LABELS.get(tier, ""), entry.get("good_at", "")  # type: ignore[arg-type]  # typeshed types dict.get's key as the key type; an untiered entry looks up None and gets the default
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model and m.get("tier"):
                tier = int(m["tier"])
                return tier, MODEL_TIER_LABELS.get(tier, ""), m.get("good_at") or ""
    return None, "", ""


def model_catalog_sort_key(m: dict) -> tuple:
    """Sort key for model rows wherever a list is shown to an agent or a
    human: tier first (untiered last), builtins before customs, then
    registry order, then creation time."""
    tier = m.get("tier")
    return (
        int(tier) if tier else 99,
        not m.get("is_builtin", True),
        _MODEL_REGISTRY_ORDER.get(m.get("model_id") or m.get("value") or "", 999),
        str(m.get("created_at") or ""),
    )


def model_supports_server_tools(model: str) -> bool:
    """Whether the Direct LLM engine offers the provider's own server-run tools
    on this model: Anthropic ``web_search`` / ``web_fetch``
    (``AnthropicAdapter.SERVER_TOOLS``), OpenAI's built-in ``web_search`` on the
    Responses API. Registry builtins only — an admin-added model stays False
    (a custom OpenAI id may be a gateway that rejects the tool)."""
    entry = MODEL_REGISTRY.get(model)
    return entry.get("server_tools", False) if entry else False


def get_model_supports_xhigh(model: str) -> bool:
    """Return True if the model accepts the platform's "xhigh" effort level.

    xhigh was introduced by Anthropic with Opus 4.7 as a new level between
    "high" and "max" (not an alias for "max"). OpenAI's gpt-5 family has had
    xhigh as the top of its reasoning-effort scale for a while. Pre-4.7
    Anthropic models reject xhigh at the API, so each execution layer's
    startup path consults this helper and silently falls back to "max" on
    models that don't support it — see the failsafe in core/layers/cli/session.py,
    core/layers/providers/anthropic_adapter.py, etc.

    Resolution order: MODEL_REGISTRY (builtins) → DB execution_layer_models
    (admin-edited custom models) → False (conservative — unknown models
    fall back to "max" rather than risking an API rejection).
    """
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return bool(entry.get("supports_xhigh", False))
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model:
                return bool(m.get("supports_xhigh", 0))
    return False


def get_model_supports_ultra(model: str) -> bool:
    """Return True if the model accepts the platform's "ultra" effort level.

    Ultra is Codex-only (GPT-6.1 Sol, gpt-5.6 Terra, GPT-6 Astra): max reasoning plus
    Codex-native proactive multi-agent orchestration. Registry-only on purpose — custom
    admin-added models have no supports_ultra column (the wire clamp in
    map_effort_to_codex is prefix-based, so a custom "gpt-6.1-sol-*" id still
    maps correctly; the dashboard just won't offer the option). Unknown
    models → False: a stored "ultra" then clamps to the model's ceiling in
    every execution layer rather than risking an API rejection.
    """
    entry = MODEL_REGISTRY.get(model)
    return bool(entry.get("supports_ultra", False)) if entry else False


def get_model_provider(model: str, layer: str = "") -> str:
    """Resolve the provider for a model ID.

    Resolution order: MODEL_REGISTRY → DB execution_layer_models → prefix
    heuristics. ``layer`` scopes the DB lookup to that layer's rows (a model
    id discovered on codex-cli lives in codex-cli rows); empty = any layer,
    first match (usage attribution and titles only know the model id).
    """
    # 1. Check in-memory registry (Anthropic builtins)
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return entry.get("provider", "anthropic")
    # 2. Check DB (dynamically discovered/added models have provider set)
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        db_models = subscription_store.list_models(layer=layer or None)
        for m in db_models:
            if m.get("model_id") == model and m.get("provider"):
                return m["provider"]
    # 3. Prefix heuristics for unknown models
    if model.startswith(("gpt-", "o1-", "o3", "o4-", "chatgpt-", "openai/")):
        return "openai"
    if model.startswith(("llama", "gemma", "mixtral", "deepseek", "qwen")):
        return "groq"
    if model.startswith("claude"):
        return "anthropic"
    return "anthropic"


def get_model_layers(model: str) -> list[str]:
    """Resolve which execution layers a model belongs to.

    Resolution: MODEL_REGISTRY (builtins) → DB execution_layer_models (custom).
    Empty list = unknown model. Used to keep an agent's PRIMARY execution layer
    consistent with its default model — a model only runs on its own layer
    (e.g. ``gpt-6.1-sol`` is ``codex-cli`` only), so the no-picker default (primary
    layer + default model) must agree or tasks hard-reject the model.
    """
    if not model:
        return []
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return list(entry.get("layers", []))
    try:
        from storage.billing import subscription_store
        return [
            m["layer"] for m in subscription_store.list_models()
            if m.get("model_id") == model and m.get("layer")
        ]
    except psycopg.Error:
        return []


# Per-provider default pricing (input, output, cache_write, cache_read) per 1M tokens.
# Used when a model isn't in MODEL_REGISTRY (e.g., dynamically discovered models).
# A future per-model pricing table in the DB can override these defaults.
PROVIDER_DEFAULT_PRICING: dict[str, tuple[float, float, float, float]] = {
    "anthropic": (2.0, 10.0, 2.50, 0.10),   # Sonnet-level (Sonnet 5.5's $2/$10, cache reads at 0.05x)
    "openai":    (2.0, 8.0, 0, 1.0),          # GPT-4.1 level
    "groq":      (0.20, 0.20, 0, 0),          # very cheap hosted inference
    "ollama":            (0, 0, 0, 0),         # local, free
    "openai_compatible": (0, 0, 0, 0),         # self-hosted endpoint, pricing depends on backend
}
MODEL_DEFAULT_PRICING = PROVIDER_DEFAULT_PRICING["anthropic"]
MODEL_DEFAULT_CONTEXT_WINDOW = 200_000

# Retired builtin → successor. Walked at boot (startup.py →
# subscription_store.remap_retired_model per entry, each retired id remapped
# to the END of its chain via successor_model, so the dict's order never
# strands a pin on an intermediate retired id) so every persisted PIN of
# a retired id — agents.default_model, chats.model, dynamic_tasks.override_model
# — moves to the successor (every layer at once: a successor that serves
# fewer layers than the retired id would strand the other layer's pins).
# sync_builtin_models retires the old row and the retired id then fails the
# model-allowed check. Only list lossless swaps (same or lower price, same
# capabilities, the same layers); a retirement WITHOUT a successor just
# leaves the pins to the fallback. One line per retirement, no startup code.
# The walk skips a retired id an admin has re-added as a custom row (the
# admin chose to keep it; its pins stay). Pins the walk never sees — a
# check's judge.model, the title-model setting — resolve through
# successor_model() at read time.
MODEL_SUCCESSORS: dict[str, str] = {
    "claude-opus-4-8[1m]": "claude-opus-5",   # 2026-07-24 — identical price tuple
    "claude-fable-5": "claude-fable-5-1",     # 2026-09-01 — same base price, cheaper cache reads
    "claude-opus-5": "claude-opus-5-5",       # 2026-09-24 — cheaper on every element of the tuple
    "gpt-5.6-sol": "gpt-6-sol",               # 2026-09-24 — cheaper on every element, keeps ultra
    "gpt-5.6-luna": "gpt-6-luna",             # 2026-09-24 — cheaper on every element, max-only as before
    "claude-sonnet-5": "claude-sonnet-5-5",   # the same tuple, window, flags and layers
    "gpt-6-sol": "gpt-6.1-sol",               # cheaper cached input, the rest equal
    "claude-haiku-4-5": "claude-haiku-5-5",   # 2026-10-13 — cheaper on every element, past its 100K line too; thinking, 1M, both engines
}


def successor_model(model: str) -> str:
    """The id a retired builtin pin resolves to today: ``MODEL_SUCCESSORS``
    followed to the end of its chain (``claude-opus-4-8[1m]`` →
    ``claude-opus-5`` → ``claude-opus-5-5``), the id itself when no entry
    applies (cycle-safe). The boot walk remaps every retired id to this end;
    the pins the walk never rewrites (a check's ``judge.model``, the
    title-model setting) read through it instead."""
    cur = model or ""
    seen: set[str] = set()
    while cur in MODEL_SUCCESSORS and cur not in seen:
        seen.add(cur)
        cur = MODEL_SUCCESSORS[cur]
    return cur


def live_model_id(model: str) -> str:
    """A pinned model id as it runs today — the boot walk's rule at read
    time, for the pins the walk never sees (one written after the boot, a
    browser's sticky pick, the title setting, a check's judge): the id
    itself when it is not a retired builtin or when an admin re-added it as
    a custom model (the walk leaves those pins alone too), else
    ``successor_model``. A lookup error answers the successor: a
    ``MODEL_SUCCESSORS`` key is a retired builtin by construction."""
    if not model or model not in MODEL_SUCCESSORS:
        return model or ""
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        if subscription_store.custom_model_exists(model):
            return model
    return successor_model(model)


def get_model_pricing(model: str, provider: str = "") -> tuple[float, float, float, float]:
    """Get (input, output, cache_write, cache_read) pricing per 1M tokens.

    Resolution order: DB (custom pricing) → MODEL_REGISTRY → provider default.
    If provider is given, use it directly for fallback instead of heuristic.
    """
    # 1. Check DB for custom pricing (dynamically added models).
    # EVERY layer's rows, not just direct-llm: an admin who priced a custom
    # model on the Codex or Claude card had that price silently ignored and
    # got the registry's (or the provider default's) numbers in every usage
    # record. The row is keyed by (layer, model_id), so the first row for
    # this model id is the admin's answer whichever card they set it on.
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model and m.get("pricing_input", 0) > 0:
                return (
                    m["pricing_input"], m["pricing_output"],
                    m.get("pricing_cache_write", 0), m.get("pricing_cache_read", 0),
                )
    # 2. Check in-memory registry (Anthropic builtins)
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return entry["pricing"]
    # 3. Fall back to provider-level default
    resolved = provider or get_model_provider(model)
    return PROVIDER_DEFAULT_PRICING.get(resolved, MODEL_DEFAULT_PRICING)


def get_model_long_context(model: str) -> tuple[int, float, float]:
    """``(prompt_tokens_over, input_multiplier, output_multiplier)`` of a
    builtin the vendor prices by prompt length (``long_context`` on its
    registry entry, Haiku 5.5's 100K line): a request whose prompt — input
    plus cache reads and writes — passes the line pays the multiplied rates
    on ALL of its tokens. ``(0, 1.0, 1.0)`` for every other model."""
    lc = (MODEL_REGISTRY.get(model) or {}).get("long_context")
    if not lc:
        return 0, 1.0, 1.0
    over, in_m, out_m = lc
    return int(over), float(in_m), float(out_m)


def price_usage(model: str, provider: str = "", *, input_tokens: int = 0,
                output_tokens: int = 0, cache_write_tokens: int = 0,
                cache_read_tokens: int = 0) -> float:
    """The USD cost of one request's tokens at the model's rates
    (:func:`get_model_pricing`), the long-context multipliers applied when
    the prompt passes the model's line. The one formula for the token-priced
    paths (the Direct LLM adapters, the interactive transcript rows); a
    provider's per-call fees are the caller's."""
    p_in, p_out, p_cw, p_cr = get_model_pricing(model, provider)
    over, in_m, out_m = get_model_long_context(model)
    if over and input_tokens + cache_write_tokens + cache_read_tokens > over:
        p_in, p_cw, p_cr, p_out = p_in * in_m, p_cw * in_m, p_cr * in_m, p_out * out_m
    return (input_tokens * p_in + output_tokens * p_out
            + cache_write_tokens * p_cw + cache_read_tokens * p_cr) / 1_000_000


def get_model_context_window(model: str) -> int:
    """Get the context window size for a model.

    Resolution order: DB → MODEL_REGISTRY → default.
    """
    # Check DB for a custom context window — every layer's rows, for the same
    # reason as get_model_pricing above (an admin's window set on the Codex or
    # Claude card was ignored, so the gauge and the direct-llm truncation
    # threshold used the registry's number instead).
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model and m.get("context_window", 0) > 0:
                return m["context_window"]
    entry = MODEL_REGISTRY.get(model)
    return entry["context_window"] if entry else MODEL_DEFAULT_CONTEXT_WINDOW


def model_supports_reasoning(model: str) -> bool:
    """Check if a model supports reasoning effort parameters.

    Resolution: MODEL_REGISTRY (builtin) → DB flag (dynamic/admin) → False.
    Admin can toggle this per model in the execution layers page.
    """
    # Builtin models: MODEL_REGISTRY is authoritative
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return entry.get("supports_reasoning", False)
    # Dynamic models: check DB (admin-configured via execution layers page)
    with contextlib.suppress(Exception):
        from storage.billing import subscription_store
        for m in subscription_store.list_models():
            if m.get("model_id") == model:
                return bool(m.get("supports_reasoning", 0))
    return False


def get_layer_models(layer: str, *, offers_ultra: bool = False) -> list[dict]:
    """Get the builtin model list for an execution layer (for LayerCapabilities).

    Includes pricing and context_window so sync_builtin_models can populate DB.
    ``offers_ultra``: the ENGINE can run the "ultra" effort at all (Codex's
    proactive multi-agent orchestration); the per-model registry flag is then
    ANDed with it, so Terra — also a direct-llm model — shows ultra on the
    codex list only, and Luna never does.
    """
    models: list[dict] = [{"value": "", "label": "System Default"}]
    for model_id, info in MODEL_REGISTRY.items():
        if layer in info.get("layers", []):
            pricing = info.get("pricing", MODEL_DEFAULT_PRICING)
            models.append({
                "value": model_id,
                "label": info["label"],
                "provider": info.get("provider", "anthropic"),
                "context_window": info.get("context_window", 0),
                "pricing_input": pricing[0],
                "pricing_output": pricing[1],
                "pricing_cache_write": pricing[2],
                "pricing_cache_read": pricing[3],
                "supports_reasoning": info.get("supports_reasoning", False),
                "supports_xhigh": info.get("supports_xhigh", False),
                "supports_ultra": bool(info.get("supports_ultra", False)) and offers_ultra,
                "tier": info.get("tier"),
                "tier_label": MODEL_TIER_LABELS.get(info.get("tier"), ""),  # type: ignore[arg-type]  # typeshed types dict.get's key as the key type; an untiered entry looks up None and gets the default
                "good_at": info.get("good_at", ""),
            })
    return models


# Precomputed once at module load: model_id → index in MODEL_REGISTRY order.
# Used to sort the DB fallback so builtins surface in the order they appear
# in MODEL_REGISTRY.keys() (Fable 5.1 before Opus 5.5 before Sonnet 5.5). Python
# 3.7+ guarantees dict insertion order, so this is stable.
_MODEL_REGISTRY_ORDER: dict[str, int] = {
    model_id: i for i, model_id in enumerate(MODEL_REGISTRY.keys())
}


def _pool_providers(layer: str) -> set[str]:
    """Providers with an active platform-pool subscription on ``layer`` (the
    relay rows count; personal-only accounts do not — they serve one user's
    chats, and the System Default is an agent-level choice)."""
    try:
        from storage.billing import subscription_store
        return {
            (s.get("provider") or "")
            for s in subscription_store.list_platform_pool(layer)
        } - {""}
    except psycopg.Error:
        return set()


def resolve_agent_model(agent_name: str, layer: str | None = None) -> str:
    """Resolve the effective model for an agent session.

    Precedence (engine-contract lane, phase 2 — the declared default is the
    new middle term; the operator's rule is "the tier is the rule": each
    engine DECLARES its default as the best tier it serves with tier 1
    offered but never defaulted):
      1. agents.default_model (the per-agent PIN), read through
         :func:`live_model_id` (a retired builtin as its successor). Two-mode:
         with ``layer=None`` it is returned with no check at all; with
         ``layer`` given it is returned when a row for it exists on that
         layer — enabled or not. A pin is an explicit human choice; the
         admin's ``enabled`` toggle moves the DEFAULT below, never a pin.
      2. The layer's declared ``ModelPolicy.default_model``, when its row on
         the layer exists, is ENABLED, and the platform pool can serve its
         provider (no pool at all = no filter).
      3. Else the enabled (+ pool-served) rows, sorted so the fallback falls
         DOWN the tiers from the declared default and never up: tiers at or
         below the default's tier first (tier order, builtins first, then
         registry order, then created_at); tiers ABOVE it only when nothing
         else is enabled — so disabling Opus 5.5 lands on Sonnet 5.5, not Fable.
      4. Raise RuntimeError — no silent Anthropic fallback, no env-var
         default. The caller decides how to surface this to the user.

    ``layer`` — the execution path the session will ACTUALLY run on. Task /
    delegate runs can override the agent's engine (override_execution_path),
    and the agent default model is engine-specific: stamping a Claude model
    onto a codex turn makes the provider 400 every turn ("The
    'claude-sonnet-5' model is not supported when using Codex with a ChatGPT
    account" — the 2026-08 "empty task turn" bug). When ``layer`` is given
    and agents.default_model is not served by it, the default is SKIPPED and
    the layer's own first enabled model is used instead. ``layer=None``
    keeps the historical behavior (the agent's own execution_path).

    This is the single entry point for model resolution across CLI, Direct
    LLM, Codex, tasks, meetings, phone — replaces the old get_cli_model /
    get_agent_model split which both baked in Anthropic-specific defaults.

    Raises:
        RuntimeError: if no usable default AND no enabled model exists for
            the effective layer. Happens on fresh installs before the admin
            enables any models, or after an admin disables every builtin for
            a layer without adding a custom one. Message includes the agent
            name and execution path so admins know where to look.
    """
    # Function-local: config imports this module and nearly everything imports
    # config, so neither takes a module-level dependency on the layers.
    from storage.agents import agent_store
    from storage.agents.schema import DEFAULT_EXECUTION_PATH
    from storage.billing import subscription_store

    agent = agent_store.get_agent(agent_name)
    path = (layer
            or (agent.get("execution_path") if agent else None)
            or DEFAULT_EXECUTION_PATH)
    default_model = live_model_id((agent or {}).get("default_model") or "")
    if default_model:
        if not layer:
            return default_model
        # Layer override: honor the default only when that layer serves it
        # (mirror of ws/dashboard._model_allowed_for_path — fail OPEN on a
        # lookup error: this guards cross-layer poison, it is not the model
        # registry's gatekeeper).
        try:
            served = any(
                (m.get("model_id") or "") == default_model
                for m in subscription_store.list_models(layer=path)
            )
        except psycopg.Error:
            served = True
        if served:
            return default_model
    return resolve_layer_default_model(path, agent_name=agent_name)


def resolve_layer_default_model(layer: str, *, agent_name: str = "") -> str:
    """The model an UNPINNED agent on ``layer`` runs right now — steps 2-4 of
    :func:`resolve_agent_model`, which calls this after the pin check. The
    catalog (``GET /v1/execution-layers``) serves it per engine as
    ``auto_model`` so the dashboard's "Auto" option names the model that
    will actually run instead of re-deriving it (and getting it wrong).

    ``agent_name`` only decorates the error: the rule itself is per engine
    and per install (enablement + the platform pool), never per agent.

    Raises:
        RuntimeError: when no model is enabled on ``layer``.
    """
    from storage.billing import subscription_store

    db_models = subscription_store.list_models(layer=layer)
    enabled = [m for m in db_models if m.get("enabled")]
    if not enabled:
        who = f"for agent '{agent_name}' " if agent_name else ""
        raise RuntimeError(
            f"No enabled model available {who}"
            f"(execution_path='{layer}'). Configure one at Admin > "
            f"Execution Layers."
        )

    # Prefer a provider the platform pool can actually serve: a local-only
    # install would otherwise land on a vendor builtin and fail with "no
    # credentials". A pool with no providers (personal accounts only) applies
    # no filter — and neither does a lookup error (_pool_providers returns an
    # empty set for both).
    configured = _pool_providers(layer)
    if configured:
        served = [m for m in enabled if (m.get("provider") or "") in configured]
        if served:
            enabled = served

    # 2. The engine's DECLARED default, if it survived both filters above.
    declared = _declared_default_model(layer)
    if declared and any(m.get("model_id") == declared for m in enabled):
        return declared

    # 3. Fall DOWN the tiers from the declared default, never up: a row whose
    # tier is better (numerically lower) than the default's sorts LAST, so it
    # is reached only when nothing at or below the default's tier is enabled
    # and servable. Within a band: tier order, builtins first, registry
    # order, created_at. Untiered custom rows count as the worst tier.
    default_tier = get_model_tier(declared)[0] if declared else None

    def _sort_key(m: dict) -> tuple:
        tier = m.get("tier")
        tier_n = int(tier) if tier else 99
        above_default = default_tier is not None and tier_n < default_tier
        return (
            above_default,
            tier_n,
            not m.get("is_builtin"),
            _MODEL_REGISTRY_ORDER.get(m.get("model_id", ""), 999),
            m.get("created_at", ""),
        )

    enabled.sort(key=_sort_key)
    return enabled[0]["model_id"]


def _declared_default_model(layer: str) -> str:
    """The ``ModelPolicy.default_model`` the layer declares, or "" when the
    layer is unknown or the registry cannot be reached (a test that stubs
    the store must not need the layer packages)."""
    try:
        from core.session.session_manager import get_layer_capabilities
        caps = get_layer_capabilities(layer)
    except ImportError:
        return ""
    return caps.model_policy.default_model if caps else ""


# Thin aliases kept for semantic clarity at call sites — CLI vs Direct path
# used to differ, but after dropping the Anthropic-specific env defaults
# they do exactly the same thing. Delegating to one implementation means
# the ~11 existing call sites don't need updates.
def get_cli_model(agent_name: str, layer: str | None = None) -> str:
    """Get the CLI model for an agent (dashboard sessions). ``layer`` = the
    execution path the session will actually run on, when it can differ
    from the agent's own (task/delegate overrides)."""
    return resolve_agent_model(agent_name, layer=layer)


def resolve_run_model(agent_name: str, override_model: str | None,
                      layer: str | None = None) -> str:
    """The model a task or delegate run spawns on: its own pin as it runs
    today (:func:`live_model_id`), else the agent's model for the run's
    layer (the agent's own engine when the run pins none, checked against
    it the same way). ONE answer for the run's chat row (what billing
    reads) and the session the run starts, so the two never name different
    models."""
    pinned = live_model_id(override_model or "")
    if pinned:
        return pinned
    if not layer:
        from storage.agents import agent_store
        from storage.agents.schema import DEFAULT_EXECUTION_PATH
        layer = ((agent_store.get_agent(agent_name) or {}).get("execution_path")
                 or DEFAULT_EXECUTION_PATH)
    return get_cli_model(agent_name, layer=layer)


def get_agent_model(agent_name: str) -> str:
    """Get the model for an agent (direct API / phone path)."""
    return resolve_agent_model(agent_name)


def get_cli_effort(agent_name: str) -> str:
    """Get the effort level for an agent (CLI sessions). A stored value off
    the platform's ladder (an agent saved before the API checked it) reads
    as the default: loading the agent never fails, and no engine is handed
    a level it would refuse at spawn."""
    from core.execution_layer import EFFORT_LADDER
    from storage.agents import agent_store
    agent = agent_store.get_agent(agent_name)
    effort = (agent or {}).get("default_effort") or ""
    return effort if effort in EFFORT_LADDER else DEFAULT_EFFORT_LEVEL


# What one provider-run web search costs on top of the tokens (USD per search):
# Anthropic's web_search tool and OpenAI's built-in web_search both list $10 per
# 1k calls. Added to the per-turn cost by ProviderAdapter.calculate_cost from
# ProviderUsage.web_search_requests; the hosted relay bills the same fee ×1.25.
WEB_SEARCH_USD_PER_REQUEST = 0.01
