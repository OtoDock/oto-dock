"""LLM chat-title generation — provider-abstracted, sourced from the Direct-LLM
execution layer.

A new dashboard chat gets an instant *deterministic* title (first words of the
prompt) at send time. This service performs a ONE-TIME in-place upgrade to a
concise, emoji-prefixed LLM title generated from the first prompt + the first
assistant response. It is fired from two places — the headless stream pump
(``core/events/stream_pump.py``) and the interactive-CLI turn-complete funnel
(``core/session/interactive_session.py``) — and both converge on ``request_chat_title``,
which is idempotent via the atomic ``claim_title_generation`` once-flag.

Provider / model / credentials come from the Direct-LLM **platform**
subscriptions (no separate API key): an admin can pin a specific provider's title
model or leave it on Auto, which walks ``_LADDER`` (groq → openai → anthropic →
ollama) and uses the first configured provider. Credentials resolve
BYO-key-wins → hosted-relay mint, reusing ``subscription_pool.relay_llm_credentials``
(the same path the Direct-LLM layer and the phone turn-classifier use). Cost is
metered into ``usage_records`` (``source_type='title-generation'``) and surfaces
in usage analytics. Disabled / no-provider → the deterministic title stays.
Task-run chats get the same upgrade (they list in the sidebar's task mode);
only ``meeting-`` chats are skipped.
"""

import asyncio
import logging
import re

import config
from storage import database as task_store

logger = logging.getLogger("title_generator")

# The dashboard injects a "[Current time: …]" prelude ahead of interactive
# sends — strip it or it becomes the title (twin of ws/dashboard_chat.py's
# send-time recognizer; both must keep matching the injected shape).
_TIME_PRELUDE_RE = re.compile(r"^\[Current time: [^\]\n]{1,160}\][ \t]*(?:\r?\n+|$)")

# Early-fire thresholds — the canonical values shared by BOTH trigger surfaces
# (the headless pump's TEXT/TOOL_USE handlers and the interactive funnel's
# tail-batch counters): fire the one-time upgrade once the first response
# crosses this many characters (~70 tokens — enough signal for a good title,
# early enough to feel instant), or — for tool-heavy agentic turns with little
# prose — once this many tool calls have happened. Shorter first turns title
# at turn end instead.
TITLE_CHAR_THRESHOLD = 280
TITLE_TOOL_THRESHOLD = 5


def deterministic_title(text: str) -> str:
    """Stable chat title from a first user message — first ~6 words / 48 chars,
    whitespace-collapsed, ellipsis if truncated. The same rule the chat layer
    applies at send time (``ws/dashboard_chat.py::_deterministic_title``);
    exposed here so the storage layer can stamp scheduler-driven task chats
    without importing the WS controller."""
    stripped = _TIME_PRELUDE_RE.sub("", text or "", count=1)
    cleaned = " ".join(stripped.split())
    if not cleaned:
        return "New Chat"
    words = cleaned.split(" ")
    title = " ".join(words[:6])
    cut = len(words) > 6
    if len(title) > 48:
        title = title[:48].rstrip()
        cut = True
    return title + ("…" if cut else "")

# Per-provider title model (the cheap/fast tier). Ollama / LiteLLM resolve their
# model dynamically from the configured local Direct-LLM models.
_PROVIDER_TITLE_MODEL = {
    # gpt-oss-120b is a reasoning model, which is fine here: on Groq its
    # thinking rides a separate ``message.reasoning`` field (never content, so
    # titles stay clean) and _MAX_TOKENS leaves room for the thinking tokens —
    # same treatment as OpenAI's gpt-5.6-luna. generate_title() also requests
    # effort "low" so reasoning-capable title models think minimally.
    "groq": "openai/gpt-oss-120b",
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-haiku-4-5",
}
# Auto-resolution order when the admin hasn't pinned a model.
_LADDER = ["groq", "openai", "anthropic", "ollama"]
_PROVIDER_LABEL = {
    "groq": "Groq", "openai": "OpenAI", "anthropic": "Anthropic",
    "ollama": "Ollama (local)", "openai_compatible": "OpenAI-compatible endpoint",
}
_KEYLESS = ("ollama", "openai_compatible")

_TITLE_SYS = (
    "Generate a concise chat title (max 6 words) capturing the topic. "
    "Start with one relevant emoji. Output only the title — no quotes, no preamble."
)
# Generous cap: a title is ~10 tokens, but gpt-5.6-luna and gpt-oss-120b are
# REASONING models whose thinking tokens are billed as output and must fit under
# this cap too — a tiny cap (e.g. 24) lets reasoning exhaust the budget and
# yields an EMPTY title. Non-reasoning models (claude-haiku) emit ~10 tokens
# and stop, so this is only a ceiling for them, never a cost.
_MAX_TOKENS = 1024
_INPUT_CHARS = 4000        # hard cap on the prompt+excerpt fed to the model (cost bound)
_EXCERPT_CHARS = 1500      # cap on the assistant excerpt specifically
_TITLE_MAX_CHARS = 60

# platform_settings keys.
_SETTING_ENABLED = "title_generation_enabled"   # "0" = off; unset / "" = on
_SETTING_MODEL = "title_generation_model"        # a model id, or "" = Auto


# --------------------------------------------------------------------------
# Provider / credential resolution (mirrors services/phone/phone_config.py)
# --------------------------------------------------------------------------

def _platform_direct_subs(provider: str) -> list[dict]:
    # Pool view (contribute_platform + active + owner-is-admin). This helper reads
    # the store directly — it bypasses acquire_subscription — so list_platform_pool
    # is what keeps a demoted admin's / a user's personal sub out of title generation.
    from storage import subscription_store
    return subscription_store.list_platform_pool(layer="direct-llm", provider=provider)


def _provider_configured(provider: str) -> bool:
    """True if ``provider`` has a usable Direct-LLM platform subscription — a BYO
    key, a hosted relay sub, or (keyless local) any active sub. Does NOT mint a
    token, so it is safe for the admin GET / status path."""
    from storage import subscription_store
    keyless = provider in _KEYLESS
    for sub in _platform_direct_subs(provider):
        if sub.get("auth_type") == "relay":
            return True
        if subscription_store.get_credential_data(sub["id"]).get("api_key", ""):
            return True
        if keyless:
            return True
    return False


def _local_model_for(provider: str) -> str:
    """First enabled Direct-LLM model for a keyless local provider (ollama/openai_compatible)."""
    try:
        from storage import subscription_store
        for m in subscription_store.list_models(layer="direct-llm"):
            if m.get("provider") == provider and m.get("enabled", True):
                return m.get("model_id") or ""
    except Exception:
        logger.debug("title-gen: local model lookup failed for %s", provider, exc_info=True)
    return ""


def _title_model_for(provider: str) -> str:
    if provider in _PROVIDER_TITLE_MODEL:
        return _PROVIDER_TITLE_MODEL[provider]
    if provider in _KEYLESS:
        return _local_model_for(provider)
    return ""


def _select_provider() -> tuple[str, str] | None:
    """``(provider, model)`` honoring the enable toggle + admin model pin +
    Auto-ladder. Uses ``_provider_configured`` (NO token mint) so it is safe to
    call from the admin GET. None when disabled or nothing is configured."""
    if task_store.get_platform_setting(_SETTING_ENABLED) == "0":
        return None
    selected = (task_store.get_platform_setting(_SETTING_MODEL) or "").strip()
    if selected:
        provider = config.get_model_provider(selected)
        if _provider_configured(provider):
            return provider, selected
        # The pinned provider is no longer configured → fall through to Auto so
        # titles keep working rather than silently stopping.
    for provider in _LADDER:
        if _provider_configured(provider):
            model = _title_model_for(provider)
            if model:
                return provider, model
    return None


def _provider_credentials(provider: str) -> tuple[str, str] | None:
    """``(api_key, base_url)`` for a provider — BYO-key-wins → relay-mint → keyless
    local. ``base_url`` '' means the adapter's vendor default. None when
    unresolved. MAY mint a relay token (do not call from a GET)."""
    from storage import subscription_store
    subs = _platform_direct_subs(provider)
    if not subs:
        return None
    # BYO-wins: a stored key beats the hosted relay (lower latency, no credits).
    for sub in subs:
        data = subscription_store.get_credential_data(sub["id"])
        key = data.get("api_key", "")
        if key:
            return key, (data.get("endpoint_url", "") or "")
    # Hosted relay → mint a SYSTEM token (user_sub="") + relay endpoint, reusing
    # the same path the Direct-LLM layer + phone classifier use.
    if any(s.get("auth_type") == "relay" for s in subs):
        from services.engines import subscription_pool
        creds = subscription_pool.relay_llm_credentials(provider, "")
        if creds:
            return creds  # (minted_token, "{RELAY}/v1/relay/<provider>/...")
    # Keyless local (ollama / openai_compatible): default key + the sub's endpoint.
    if provider in _KEYLESS:
        data = subscription_store.get_credential_data(subs[0]["id"])
        default_key = "ollama" if provider == "ollama" else "not-needed"
        return default_key, (data.get("endpoint_url", "") or "")
    return None


def resolve_title_provider() -> tuple[str, str, str, str] | None:
    """``(provider, model, api_key, base_url)`` to use for a title, or None to
    keep the deterministic title. MAY mint a relay token — never call from a GET."""
    sel = _select_provider()
    if not sel:
        return None
    provider, model = sel
    creds = _provider_credentials(provider)
    if not creds:
        return None
    api_key, base_url = creds
    return provider, model, api_key, base_url


def title_generation_status() -> dict:
    """Admin GET payload: the enable flag, the pinned model (''=Auto), whether the
    feature is currently ACTIVE (enabled + a provider resolves, no mint), the
    effective provider/model, and the dropdown options (each configured provider's
    title model; the frontend prepends an Auto entry)."""
    enabled = task_store.get_platform_setting(_SETTING_ENABLED) != "0"
    selected = (task_store.get_platform_setting(_SETTING_MODEL) or "").strip()
    options = []
    for provider in _LADDER:
        if _provider_configured(provider):
            model = _title_model_for(provider)
            if model:
                options.append({
                    "provider": provider, "model": model,
                    "label": _PROVIDER_LABEL.get(provider, provider.title()),
                })
    sel = _select_provider()
    return {
        "enabled": enabled,
        "selected_model": selected,
        "active": enabled and sel is not None,
        "active_provider": sel[0] if sel else "",
        "active_model": sel[1] if sel else "",
        "options": options,
    }


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def _clean_title(raw: str) -> str:
    t = (raw or "").strip()
    # Drop a single pair of wrapping quotes the model sometimes adds.
    if len(t) >= 2 and t[0] in "\"'“”‘’" and t[-1] in "\"'“”‘’":
        t = t[1:-1].strip()
    t = " ".join(t.split())   # collapse newlines / runs of whitespace
    if len(t) > _TITLE_MAX_CHARS:
        t = t[:_TITLE_MAX_CHARS].rstrip()
    return t


async def generate_title(
    user_prompt: str, assistant_excerpt: str,
    provider: str, model: str, api_key: str, base_url: str,
) -> tuple[str, object]:
    """Call the provider's cheap title model. Returns ``(title, ProviderUsage)``;
    raises on a provider error event (caller swallows)."""
    from core.layers.providers import get_adapter, ProviderUsage

    content = (user_prompt or "").strip()
    excerpt = (assistant_excerpt or "").strip()
    if excerpt:
        content += "\n\nAssistant response:\n" + excerpt[:_EXCERPT_CHARS]
    content = content[:_INPUT_CHARS]

    adapter = get_adapter(provider)
    pieces: list[str] = []
    usage = ProviderUsage()
    async for ev in adapter.stream_response(
        api_key=api_key,
        model=model,
        system_prompt=_TITLE_SYS,
        messages=[{"role": "user", "content": content}],
        tools=[],
        max_tokens=_MAX_TOKENS,
        endpoint_url=(base_url or None),
        # "low" = minimal thinking on reasoning-capable title models (gpt-5.6-luna,
        # gpt-oss-120b); the adapters drop it for non-reasoning models/providers.
        effort="low",
    ):
        if ev.type == "text_delta":
            pieces.append(ev.text or "")
        elif ev.type == "usage" and ev.usage:
            usage = ev.usage
        elif ev.type == "error":
            raise RuntimeError(ev.text or "provider error")
    return _clean_title("".join(pieces)), usage


def _first_turn_texts(chat_id: str) -> tuple[str, str]:
    """``(first user message, first assistant message)`` from chat_messages, both
    plain text. Oldest-first; skips empty / event-only rows."""
    user_text = ""
    assistant_text = ""
    for m in task_store.get_chat_messages(chat_id):
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "user" and not user_text:
            user_text = content
        elif role == "assistant" and not assistant_text:
            assistant_text = content
        if user_text and assistant_text:
            break
    return user_text, assistant_text


async def request_chat_title(chat_id: str, *, assistant_excerpt: str = "") -> None:
    """Fire-and-forget one-time LLM title upgrade for a dashboard chat. Idempotent:
    safe to call from both pump fire points, the interactive funnel, and any later
    turn — the atomic claim ensures exactly one generation. Never raises."""
    try:
        if not chat_id or chat_id.startswith("meeting-"):
            return
        resolved = await asyncio.to_thread(resolve_title_provider)
        if not resolved:
            return  # disabled / no provider → keep the deterministic title
        provider, model, api_key, base_url = resolved
        chat = await asyncio.to_thread(task_store.get_chat, chat_id)
        if not chat:
            return
        # Validate BEFORE claiming: a fire with no persisted user prompt yet
        # (e.g. an artifact-framed first turn, or an early trigger racing the
        # prompt persist) must leave the claim intact so a later real turn can
        # still title the chat — a claim burned here would be permanent.
        user_prompt, db_assistant = await asyncio.to_thread(_first_turn_texts, chat_id)
        if not user_prompt:
            return  # nothing to title from yet
        # Once-only: the first caller to flip the flag wins; the rest no-op.
        # The winner also gets the claim-time title — the CAS baseline below.
        claimed, baseline_title = await asyncio.to_thread(
            task_store.claim_title_generation, chat_id,
        )
        if not claimed:
            return
        assistant_text = (assistant_excerpt or "").strip() or db_assistant
        title, usage = await generate_title(
            user_prompt, assistant_text, provider, model, api_key, base_url,
        )
        if not title:
            return  # keep deterministic; flag stays claimed (no retry storm)
        # CAS on the claim-time title: a manual rename that landed after the
        # claim wins — never overwrite it, never broadcast the stale title.
        # The generation cost is metered either way (the tokens were spent).
        wrote = await asyncio.to_thread(
            task_store.update_chat_title_cas, chat_id, title, baseline_title,
        )
        if wrote:
            try:
                from services.notifications import notification_manager
                notification_manager.broadcast_chat_title(
                    chat.get("user_sub") or "", chat_id, title,
                    agent=chat.get("agent") or "",
                )
            except Exception:
                logger.debug("title-gen: title broadcast failed for %s", chat_id, exc_info=True)
        _record_cost(chat, provider, model, usage)
    except Exception:
        logger.exception("title-gen: request_chat_title failed for %s", chat_id)


def _record_cost(chat: dict, provider: str, model: str, usage: object) -> None:
    """One usage_records row (``source_type='title-generation'``, ``message_count=0``
    — a pure cost line that does not inflate turn counts). Skipped automatically
    for $0 local models (``record_turn_usage`` drops cost<=0 & message_count<=0)."""
    try:
        from core.layers.providers import get_adapter
        from services.billing import usage_service
        cost = get_adapter(provider).calculate_cost(model, usage)
        usage_service.record_turn_usage([{
            "user_sub": chat.get("user_sub"),
            "agent": chat.get("agent") or "",
            "scope": "user",
            "source_type": "title-generation",
            "source_id": chat.get("id"),
            "cost_usd": cost,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read": usage.cache_read_tokens,
            "cache_write": usage.cache_write_tokens,
            "message_count": 0,
            "provider": provider,
            "model": model,
            "source_key": "title_generation",
        }])
    except Exception:
        logger.exception("title-gen: usage record failed for %s", chat.get("id"))
