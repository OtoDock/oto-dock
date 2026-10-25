"""Provider adapter registry.

Maps provider names to ProviderAdapter instances. Auto-registers
all built-in adapters on import.
"""

import logging

from core.layers.providers.base import ProviderAdapter

logger = logging.getLogger("otodock")

_ADAPTERS: dict[str, ProviderAdapter] = {}


def register_adapter(adapter: ProviderAdapter) -> None:
    """Register a provider adapter by its name."""
    _ADAPTERS[adapter.provider_name] = adapter
    logger.info("Registered LLM provider adapter: %s", adapter.provider_name)


def get_adapter(provider: str) -> ProviderAdapter:
    """Look up a provider adapter by name.

    Falls back to the generic OpenAI-compatible adapter (chat completions)
    for unknown providers, e.g. custom self-hosted endpoints that use the
    OpenAI API format — NOT the ``openai`` adapter, which speaks the
    Responses API against OpenAI's own endpoint.
    """
    adapter = _ADAPTERS.get(provider)
    if adapter:
        return adapter
    # Fall back to OpenAI-compatible for unknown providers
    compat_adapter = _ADAPTERS.get("openai_compatible")
    if compat_adapter:
        logger.warning(
            "No adapter for provider '%s', "
            "falling back to the OpenAI-compatible adapter", provider
        )
        return compat_adapter
    raise ValueError(f"No provider adapter registered for '{provider}'")


def list_adapters() -> dict[str, ProviderAdapter]:
    """Return all registered adapters."""
    return dict(_ADAPTERS)


# Auto-register all built-in adapters
from core.layers.providers.anthropic_adapter import AnthropicAdapter
from core.layers.providers.openai_adapter import OpenAIAdapter
from core.layers.providers.openai_compat_adapter import (
    GroqAdapter,
    OllamaAdapter,
    OpenAICompatibleAdapter,
)

register_adapter(AnthropicAdapter())
register_adapter(OpenAIAdapter())
register_adapter(GroqAdapter())
register_adapter(OllamaAdapter())
register_adapter(OpenAICompatibleAdapter())
