"""Per-session Codex model catalog for a LOCAL model (Ollama), so Codex defers
its MCP tools and searches them on demand instead of sending every tool schema
on every request.

Codex only emits ``tool_search`` + ``defer_loading`` for a model whose catalog
entry has ``supports_search_tool``; an unknown slug gets the built-in fallback
(``models-manager/src/model_info.rs::model_info_from_slug``) with it off, so a
local model receives every MCP server as a ``namespace`` tool on every request
(T1 capture 2026-09-07: 25 tools, 98 functions, 200 KB; the desktop: 47k-113k
prompt tokens a turn). ``model_catalog_json = "<abs path>"`` in config.toml
(0.149.1 through 0.160.0 alike) replaces the bundled catalog with the file's
entries (longest slug-prefix match); with an entry for the session's model
Codex sends its built-ins, ``tool_search`` (searched client-side in Codex) and
``web_search`` — 12 tools, 35 KB — and Ollama 0.34+ returns the model's
``tool_search_call`` item, so the matches arrive on the next request only. See
CODEX.md "Local models and MCP tools".

The entry mirrors the fallback of the PINNED Codex field by field and changes
only what the deferral needs, so nothing else about the session moves — the
instructions text in particular. ``ModelInfo.get_model_instructions`` returns
EMPTY instructions for an entry without a template, and the legacy top-level
``base_instructions`` is promoted into it on load, so the entry carries
``codex_base_instructions.md`` next to this module: the verbatim
``codex-rs/models-manager/prompt.md`` of the pinned Codex, which is exactly what
the fallback sends (T1 capture on 0.149.1: 20,751 chars; the file is identical
from rust-v0.149.1 to rust-v0.160.0). Refresh it on every Codex bump against the
file at the new tag (VERSIONS.md "To bump a CLI version"; attribution in
``codex_base_instructions.NOTICE``).

The catalog lists EVERY codex-cli model row of the provider (the session's
model first): the dashboard allows a mid-chat switch to another model of the
same provider, and a slug missing from a custom catalog falls back to the
namespace behaviour with a per-turn warning. The rows ride the subscription env
(``_CODEX_LOCAL_MODEL_ROWS``, read off the event loop in
``resolve_subscription_env``) — the layers never touch the store on the loop.

Only the ``ollama`` provider gets a catalog: Ollama 0.34+ implements the
client-executed ``tool_search`` round trip. ``openai_compatible`` hides vLLM,
LiteLLM, llama.cpp and LM Studio behind one URL, and an unknown ``tool_search``
tool type could turn a working namespace round trip into a 400 there — those
keep today's behaviour until verified.

The proxy builds the JSON for both paths: the local layer writes it into the
session's CODEX_HOME (``_write_config_toml``), the remote layer ships it in the
start payload (``local_model_provider.catalog_json``) for the satellite's
writers (0.5.117+).
"""

from __future__ import annotations

import functools
import json
import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Codex's fallback assumes this context window for an unknown slug; a local
# model row without a configured context window keeps it (Codex's own
# auto-compaction threshold, 90% of the window, follows it — an admin sets the
# real window on the model row; Discover cannot read it from the server).
FALLBACK_CONTEXT_WINDOW = 272_000

# File name inside CODEX_HOME (both hosts) that ``model_catalog_json`` points at.
CATALOG_FILE_NAME = "models.json"

# Subscription-env variable carrying the provider's codex-cli model rows as
# JSON ``[[model_id, context_window], …]`` (subscription_pool, off-loop);
# popped by both layers like ``_CODEX_ENDPOINT_URL``.
LOCAL_MODEL_ROWS_ENV = "_CODEX_LOCAL_MODEL_ROWS"

_BASE_INSTRUCTIONS_PATH = Path(__file__).with_name("codex_base_instructions.md")


@functools.cache
def base_instructions() -> str:
    """The pinned Codex's ``prompt.md`` (what the fallback entry sends)."""
    return _BASE_INSTRUCTIONS_PATH.read_text(encoding="utf-8")


def catalog_provider_supported(provider: str) -> bool:
    return (provider or "").strip().lower() == "ollama"


def local_model_catalog_entry(model: str, context_window: int = 0) -> dict:
    """The ``ModelInfo`` for ``model``: Codex's fallback entry
    (``model_info_from_slug``, same shape from 0.149.1 to the pinned 0.160.0) with ``supports_search_tool``
    on, ``tool_mode`` direct, the model LISTED (an unavailable requested model
    is replaced by the first listed one when the app-server allows a provider
    fallback — an unlisted catalog would resolve to no model at all) and the
    row's context window. Field names and values follow codex-rs
    ``protocol/src/openai_models.rs`` (serde: snake_case enums; ``shell_type``
    ``default`` is the fallback's value on 0.149.1 and an alias of
    ``unified_exec`` from 0.153.4 on).
    """
    ctx = int(context_window) if context_window and int(context_window) > 0 else FALLBACK_CONTEXT_WINDOW
    return {
        "slug": model,
        "display_name": model,
        "description": None,
        "default_reasoning_level": None,
        "supported_reasoning_levels": [],
        "shell_type": "default",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 99,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_service_tier": None,
        "availability_nux": None,
        "upgrade": None,
        # Promoted into model_messages.instructions_template on load.
        "base_instructions": base_instructions(),
        "model_messages": None,
        "include_skills_usage_instructions": False,
        "include_plugin_usage_instructions": False,
        "include_apps_usage_instructions": False,
        "supports_reasoning_summary_parameter": True,
        "default_reasoning_summary": "auto",
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": None,
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "bytes", "limit": 10_000},
        "supports_image_detail_original": False,
        "context_window": ctx,
        "max_context_window": ctx,
        "auto_compact_token_limit": None,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_search_tool": True,
        "use_responses_lite": False,
        "node_repl_auto_review_required": False,
        "node_repl_disabled": False,
        "auto_review_model_override": None,
        "model_specialty": None,
        "tool_mode": "direct",
        "multi_agent_version": None,
    }


def local_model_catalog_json(
    model: str, provider: str, rows: Iterable[tuple[str, int]] = (),
) -> str:
    """The ``models.json`` text for a local-endpoint session, or "" when the
    provider does not get one (see the module docstring). ``rows`` are the
    provider's codex-cli model rows as ``(model_id, context_window)``; the
    session's model comes first (its row's window, else the fallback's) and
    every other row follows so a same-provider model switch keeps its entry.
    """
    if not model or not catalog_provider_supported(provider):
        return ""
    windows: dict[str, int] = {}
    for model_id, ctx in rows:
        if model_id:
            windows.setdefault(str(model_id), int(ctx or 0))
    ordered = [model] + [m for m in windows if m != model]
    entries = [local_model_catalog_entry(m, windows.get(m, 0)) for m in ordered]
    return json.dumps({"models": entries}, ensure_ascii=False, indent=1) + "\n"


def parse_local_model_rows(text: str) -> list[tuple[str, int]]:
    """``_CODEX_LOCAL_MODEL_ROWS`` back into ``[(model_id, context_window)]``
    (fail-soft: garbage → no rows → the session's model alone)."""
    try:
        raw = json.loads(text or "[]")
        return [
            (str(r[0]), int(r[1] or 0)) for r in raw
            if isinstance(r, (list, tuple)) and len(r) >= 2 and r[0]
        ]
    except (TypeError, ValueError, IndexError) as e:
        logger.debug("codex local model rows unreadable (%s); catalog lists the session model only", e)
        return []


def local_model_rows_json(provider: str) -> str:
    """The provider's codex-cli model rows as ``_CODEX_LOCAL_MODEL_ROWS`` text.
    Reads the store — call it OFF the event loop (``resolve_subscription_env``
    runs under ``asyncio.to_thread``)."""
    rows: list[list] = []
    try:
        from storage.billing import subscription_store
        for m in subscription_store.list_models(layer="codex-cli"):
            if (m.get("provider") or "") == (provider or "") and m.get("model_id"):
                rows.append([m["model_id"], int(m.get("context_window") or 0)])
    except Exception as e:  # fail-soft: the session's model alone, fallback window
        logger.debug("codex local model rows lookup failed for %s: %s", provider, e)
    return json.dumps(rows)


def catalog_toml_line(catalog_path: str) -> str:
    """The root ``model_catalog_json`` key for config.toml (an absolute path
    on the host that runs Codex; escaped for a TOML basic string)."""
    esc = catalog_path.replace("\\", "\\\\").replace('"', '\\"')
    return f'model_catalog_json = "{esc}"'
