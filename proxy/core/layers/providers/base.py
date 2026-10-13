"""ProviderAdapter ABC and shared data classes.

Every LLM provider (Anthropic, OpenAI, Groq, Ollama, etc.) implements
ProviderAdapter. The Direct LLM runner calls adapter methods to stream
responses, format tool results, and extract usage — keeping the main
loop provider-agnostic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator

import config as app_config


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProviderUsage:
    """Token usage from a single API call (plus the provider-run web searches
    the call made — they carry a per-call fee on top of the tokens)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    web_search_requests: int = 0


@dataclass
class ProviderStreamEvent:
    """Universal stream event yielded by provider adapters.

    type values:
        text_delta      — streamed text fragment
        thinking_delta  — streamed reasoning fragment (UI only — never part
                          of the assistant content sent back to the provider)
        tool_start      — tool call began (name + id known)
        tool_input_delta — partial JSON for tool input
        tool_stop       — tool call content block finished
        tool_result     — a SERVER-side tool (Anthropic web_search / web_fetch
                          / code_execution, OpenAI web_search) returned its
                          result inside the stream; the runner emits the
                          dashboard's tool_end for it (text = a short result
                          preview)
        usage           — token usage stats (emitted once per API call)
        content         — the provider's final content for the history
                          (``raw_content``; see serialize_assistant_content)
        stop            — API call finished, includes stop_reason
        error           — provider error
    """
    type: str
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    tool_input_json: str = ""
    stop_reason: str = ""          # "end_turn" | "tool_use" | ...
    usage: ProviderUsage | None = None
    raw_content: Any = None        # provider-specific final content for serialization


class ProviderError(Exception):
    """Raised by provider adapters for API errors."""
    def __init__(self, message: str, status_code: int = 0, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

class ProviderAdapter(ABC):
    """Abstract interface for LLM provider backends in the Direct LLM path."""

    #: Whether ``format_tool_results`` places a result's ``images`` (an MCP
    #: tool's kept pictures, ``services/mcp/result_files.py``) where every
    #: model of the provider reads them. False where vision depends on the
    #: model behind the endpoint (a text-only model fails the whole request)
    #: or the API has no image inside a tool result.
    tool_result_images: bool = False

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider identifier: 'anthropic', 'openai', 'groq', etc."""

    @abstractmethod
    async def stream_response(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        endpoint_url: str | None = None,
        effort: str = "",
    ) -> AsyncIterator[ProviderStreamEvent]:
        """Stream a single API call. Yields ProviderStreamEvent objects.

        The caller (run_direct_stream) handles the tool-use loop, message
        history, and SSE event emission. This method handles client creation,
        provider-specific formatting, streaming, and usage extraction.
        """
        yield  # pragma: no cover  # make this an async generator

    @abstractmethod
    def format_tool_results(self, results: list[dict]) -> list[dict]:
        """Format MCP tool results into provider-specific messages.

        Args:
            results: [{"tool_use_id": str, "content": str}]
        Returns:
            List of message dicts to extend onto the conversation history.
            Anthropic: single user message with tool_result content blocks.
            OpenAI: separate {"role": "tool"} messages.
        """

    @abstractmethod
    def serialize_assistant_content(self, raw_content: Any) -> str | list[dict] | dict:
        """Serialize the provider's response content for message history.

        Returns:
            str: Simple text content → stored as {"role": "assistant", "content": text}
            list[dict]: Content blocks (Anthropic) → stored as {"role": "assistant", "content": blocks}
            dict: Message body (OpenAI) → merged as {"role": "assistant", **dict}
                  Used when tool_calls are present alongside content.
        """

    def get_builtin_tools(self) -> list[dict]:
        """Provider-specific server-side tools (e.g., Anthropic web_search).
        Default: none.
        """
        return []

    # ---------------------------------------------------------------------------
    # Vision content blocks (chat-attached photos)
    # ---------------------------------------------------------------------------

    def format_image_content_block(
        self,
        *,
        media_type: str,
        base64_data: str,
    ) -> dict:
        """Build a single image block in the provider's native message format.

        Used by ``run_direct_stream`` when the user attaches photos to a chat
        message. The Direct LLM path has no built-in Read tool — images are
        attached directly to the user message as content blocks so the model
        sees them via the API's native vision support.

        Default returns the OpenAI Chat Completions ``image_url`` block, which
        is what OpenAI, Groq, Ollama, and LiteLLM all accept. Anthropic
        overrides with its ``image`` / ``source.base64`` block format.

        Args:
            media_type: MIME type of the image, e.g. ``"image/jpeg"``,
                ``"image/png"``.
            base64_data: Raw base64 (no ``data:...;base64,`` prefix). The
                method synthesizes the data URL when the provider format
                requires it.

        Returns:
            A single content-block dict suitable to append to a user message's
            ``content`` list.
        """
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{base64_data}"},
        }

    async def list_available_models(
        self,
        api_key: str,
        endpoint_url: str | None = None,
    ) -> list[dict]:
        """Fetch available models from the provider's API.

        Returns: [{"model_id": str, "display_name": str}]
        Default: empty list (provider has no list endpoint).
        """
        return []

    # ---------------------------------------------------------------------------
    # Shared cost calculation (uses centralized config.MODEL_REGISTRY)
    # ---------------------------------------------------------------------------

    def calculate_cost(self, model: str, usage: ProviderUsage) -> float:
        """Calculate USD cost from usage using centralized model pricing (the
        tokens at the model's rates, ``config.price_usage``, which applies a
        model's long-context tier, plus the per-call fee of every provider-run
        web search, ``config.WEB_SEARCH_USD_PER_REQUEST``)."""
        return (
            app_config.price_usage(
                model, self.provider_name,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens)
            + usage.web_search_requests * app_config.WEB_SEARCH_USD_PER_REQUEST
        )
