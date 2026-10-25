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
Task-run chats get the same upgrade (they list in the sidebar's task mode).
The service skips no id shape: a meeting's pump disarms itself by its driver
kind (``ChatStreamPump._title_armed``, ``session_kind.MEETING``), and an
interactive session never carries a meeting id.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

import psycopg

import config
from storage import database as task_store
from storage.chat.chat_titles import _without_preludes

if TYPE_CHECKING:
    from core.layers.providers import ProviderUsage
    from core.layers.providers.openai_adapter import OpenAIAdapter

logger = logging.getLogger("title_generator")


# Early-fire thresholds — the canonical values shared by BOTH trigger surfaces
# (the headless pump's TEXT/TOOL_USE handlers and the interactive funnel's
# tail-batch counters): fire the one-time upgrade once the first response
# crosses this many characters (~70 tokens — enough signal for a good title,
# early enough to feel instant), or — for tool-heavy agentic turns with little
# prose — once this many tool calls have happened. Shorter first turns title
# at turn end instead.
TITLE_CHAR_THRESHOLD = 280
TITLE_TOOL_THRESHOLD = 5


# Per-provider title model: the cheapest capable model each provider offers
# (Anthropic's is Haiku 5.5, tier 4, a tenth of Sonnet 5.5's price; it was
# Sonnet 5.5 while Haiku 4.5 had no successor). Ollama / LiteLLM resolve their
# model dynamically from the configured local Direct-LLM models.
_PROVIDER_TITLE_MODEL = {
    # gpt-oss-120b is a reasoning model, which is fine here: on Groq its
    # thinking rides a separate ``message.reasoning`` field (never content, so
    # titles stay clean) and _MAX_TOKENS leaves room for the thinking tokens —
    # same treatment as OpenAI's gpt-6-luna and Anthropic's Haiku 5.5, whose
    # adaptive thinking is on by default. generate_title() also requests
    # effort "low" so reasoning-capable title models think minimally.
    # A change here needs the hosted relay's row first (relay_vendors.py in
    # otodock-commercial): the relay rejects a model it does not price.
    "groq": "openai/gpt-oss-120b",
    "openai": "gpt-6-luna",
    "anthropic": "claude-haiku-5-5",
}
# Auto-resolution order when the admin hasn't pinned a model.
_LADDER = ["groq", "openai", "anthropic", "ollama"]


def _direct_provider(provider: str) -> dict | None:
    """The API engine's ``providers[]`` entry for ``provider`` — its label,
    whether it takes a key, its relay path — or None when it declares none
    (a local provider on hosted OtoDock)."""
    from core.execution_layer import provider_entry
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities("direct-llm")
    return provider_entry(caps, provider) if caps is not None else None


def _keyless(provider: str) -> bool:
    """A self-hosted endpoint: any active row serves it, no key needed."""
    entry = _direct_provider(provider)
    return bool(entry) and not cast("dict", entry).get("requires_key", True)

_TITLE_SYS = (
    "Generate a concise chat title (max 6 words) capturing the topic. "
    "Start with one relevant emoji. Output only the title — no quotes, no preamble."
)
# Generous cap: a title is ~10 tokens, but gpt-6-luna, gpt-oss-120b and
# claude-haiku-5-5 are REASONING models whose thinking tokens are billed as
# output and must fit under this cap too — a tiny cap (e.g. 24) lets reasoning
# exhaust the budget and yields an EMPTY title. A non-reasoning model (an
# admin's pin of a custom row without the flag) emits ~10 tokens and stops, so
# this is only a ceiling for it, never a cost.
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
    from storage.billing import subscription_store
    return subscription_store.list_platform_pool(layer="direct-llm", provider=provider)


def _provider_configured(provider: str) -> bool:
    """True if ``provider`` has a usable Direct-LLM platform subscription — a BYO
    key, a hosted relay sub, or (keyless local) any active sub. Does NOT mint a
    token, so it is safe for the admin GET / status path."""
    from storage.billing import subscription_store
    keyless = _keyless(provider)
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
        from storage.billing import subscription_store
        for m in subscription_store.list_models(layer="direct-llm"):
            if m.get("provider") == provider and m.get("enabled", True):
                return m.get("model_id") or ""
    except psycopg.Error:
        logger.debug("title-gen: local model lookup failed for %s", provider, exc_info=True)
    return ""


def _title_model_for(provider: str) -> str:
    if provider in _PROVIDER_TITLE_MODEL:
        return _PROVIDER_TITLE_MODEL[provider]
    if _keyless(provider):
        return _local_model_for(provider)
    return ""


def _pinned_model() -> str:
    """The title-model pin as it runs today (``config.live_model_id``), ''
    for Auto."""
    return config.live_model_id(
        (task_store.get_platform_setting(_SETTING_MODEL) or "").strip())


def _pin_unlisted(model: str) -> bool:
    """No builtin and no model row names ``model`` any more: the registry
    dropped it and no admin re-added it."""
    return not config.get_model_layers(model)


def _select_provider() -> tuple[str, str] | None:
    """``(provider, model)`` honoring the enable toggle + admin model pin +
    Auto-ladder. Uses ``_provider_configured`` (NO token mint) so it is safe to
    call from the admin GET. None when disabled or nothing is configured."""
    if task_store.get_platform_setting(_SETTING_ENABLED) == "0":
        return None
    # A persisted pin the boot remap never sees: a retired id follows its
    # successor at read time (an admin's gpt-5.6-luna titles on GPT-6 Luna),
    # and a pin no model row lists any more (a retired id with no successor)
    # is skipped for Auto rather than failing every title at the provider.
    selected = _pinned_model()
    if selected and not _pin_unlisted(selected):
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
    from storage.billing import subscription_store
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
        creds = subscription_pool.relay_llm_credentials("direct-llm", provider, "")
        if creds:
            return creds  # (minted_token, "{RELAY}/v1/relay/<provider>/...")
    # A keyless local endpoint: the adapter's placeholder key + the sub's endpoint
    # (a keyless provider is an OpenAI-compatible endpoint).
    if _keyless(provider):
        from core.layers.providers.registry import get_adapter
        data = subscription_store.get_credential_data(subs[0]["id"])
        return (cast("OpenAIAdapter", get_adapter(provider)).default_api_key() or ""), (data.get("endpoint_url", "") or "")
    return None


# The last title attempt's outcome, so the admin's Setup card says "Not
# working" (with why) instead of Active while every chat keeps its first
# words: a provider can be configured (a relay subscription, a key) and
# still resolve no credential or answer an error. Process-local: the proxy
# is one process, and a restart asks again at the next title or Check.
_last_attempt: dict | None = None
_attempt_lock = threading.Lock()
_FAIL_WARN_EVERY_S = 60.0
_fail_warned_at = 0.0


# What an attempt proved: a title made or refused by the provider
# (``title``), or only whether the credential resolves (``credential``: a
# title's lookup, or the admin's Check, which calls no model).
ATTEMPT_TITLE = "title"
ATTEMPT_CREDENTIAL = "credential"


def _note_attempt(ok: bool, provider: str, model: str, reason: str = "",
                  kind: str = ATTEMPT_TITLE) -> None:
    global _last_attempt, _fail_warned_at
    with _attempt_lock:
        _last_attempt = {
            "ok": ok, "kind": kind, "provider": provider, "model": model,
            "reason": reason, "at": datetime.now(timezone.utc).isoformat(),
        }
        if ok:
            return
        now = time.monotonic()
        if now - _fail_warned_at < _FAIL_WARN_EVERY_S:
            return
        _fail_warned_at = now
    logger.warning("title-gen: %s (%s): %s", provider, model, reason)


def resolve_title_provider() -> tuple[str, str, str, str] | None:
    """``(provider, model, api_key, base_url)`` to use for a title, or None to
    keep the deterministic title. MAY mint a relay token — never call from a GET.
    A provider that is selected but resolves no credential is noted."""
    sel = _select_provider()
    if not sel:
        return None
    provider, model = sel
    try:
        creds = _provider_credentials(provider)
    except Exception as e:
        _note_attempt(False, provider, model, f"the credential lookup failed: {str(e)[:200]}",
                      kind=ATTEMPT_CREDENTIAL)
        raise
    if not creds:
        _note_attempt(False, provider, model,
                      "the provider's credential did not resolve (the hosted relay "
                      "gave no token, or the subscription holds no key)",
                      kind=ATTEMPT_CREDENTIAL)
        return None
    api_key, base_url = creds
    return provider, model, api_key, base_url


def check_title_generation() -> dict:
    """The admin's Check: resolve the credentials now (it may mint a relay
    token, so it is a POST), note the outcome, answer the status. It calls
    no model, so a credential that resolves never clears the provider's own
    refusal of the last title (a revoked key still resolves): only a title
    made clears that."""
    try:
        resolved = resolve_title_provider()
    except Exception:  # noqa: BLE001  # the provider lookup spans settings and engines, it notes its own miss
        resolved = None   # noted by the lookup
    if resolved:
        last = _last_attempt
        refused = (last is not None and not last["ok"] and last.get("kind") == ATTEMPT_TITLE
                   and last["provider"] == resolved[0])
        if not refused:
            _note_attempt(True, resolved[0], resolved[1], "the credential resolves",
                          kind=ATTEMPT_CREDENTIAL)
    return title_generation_status()


def title_generation_status() -> dict:
    """Admin GET payload: the enable flag, the pinned model as it runs (a retired
    id's successor; ''=Auto), whether the
    feature is currently ACTIVE (enabled + a provider resolves, no mint), the
    effective provider/model, and the dropdown options (each configured provider's
    title model; the frontend prepends an Auto entry)."""
    enabled = task_store.get_platform_setting(_SETTING_ENABLED) != "0"
    selected = (task_store.get_platform_setting(_SETTING_MODEL) or "").strip()
    # What _select_provider runs for the pin: a retired id's successor, or
    # nothing (Auto) for a pin no model row lists any more.
    pinned = _pinned_model()
    unlisted = pinned if pinned and _pin_unlisted(pinned) else ""
    if unlisted:
        pinned = selected = ""
    options = []
    for provider in _LADDER:
        if _provider_configured(provider):
            model = _title_model_for(provider)
            if model:
                entry = _direct_provider(provider)
                options.append({
                    "provider": provider, "model": model,
                    "label": entry["label"] if entry else provider.title(),
                })
    sel = _select_provider()
    # A pin the ladder no longer offers (Haiku 4.5 since the Anthropic row
    # moved to Sonnet 5.5) runs whenever its provider is configured, so the
    # dropdown lists it, or the page would show Auto while the pinned model
    # titles every chat. The test is the provider, not _select_provider(),
    # which is None while titles are off: the disabled <select> shows the
    # pin that runs again when they are switched back on.
    pin_provider = config.get_model_provider(pinned) if pinned else ""
    pin_listed = bool(pinned and _provider_configured(pin_provider))
    if pin_listed and pinned not in {o["model"] for o in options}:
        entry = _direct_provider(pin_provider)
        options.append({"provider": pin_provider, "model": pinned,
                        "label": entry["label"] if entry else pin_provider.title()})
    return {
        "enabled": enabled,
        # The model that runs, so the <select> matches an option: the raw id
        # of a retired pin (gpt-5.6-luna) matches none and shows Auto.
        "selected_model": pinned if pin_listed else selected,
        "active": enabled and sel is not None,
        "active_provider": sel[0] if sel else "",
        "active_model": sel[1] if sel else "",
        "options": options,
        # The last title or Check since the proxy started (None before one):
        # a failure here means "configured but not working".
        "last_attempt": dict(_last_attempt) if _last_attempt else None,
        # A pin no model row lists any more ('' otherwise): titles run on Auto.
        "unlisted_pin": unlisted,
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
) -> "tuple[str, ProviderUsage]":
    """Call the provider's title model. Returns ``(title, ProviderUsage)``;
    raises on a provider error event (caller swallows)."""
    from core.layers.providers import ProviderUsage, get_adapter

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
        # "low" = minimal thinking on reasoning-capable title models (gpt-6-luna,
        # gpt-oss-120b, claude-sonnet-5-5); the adapters drop it for
        # non-reasoning models/providers.
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
            # A terminal's first row carries the injected preludes (the time,
            # a task run's parallel activity naming other chats): the title
            # is about this chat's own words.
            user_text = _without_preludes(content).strip()
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
        if not chat_id:
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
        try:
            title, usage = await generate_title(
                user_prompt, assistant_text, provider, model, api_key, base_url,
            )
        except Exception as e:
            _note_attempt(False, provider, model, f"the provider answered an error: {str(e)[:200]}")
            raise
        if not title:
            _note_attempt(False, provider, model, "the model answered an empty title")
            return  # keep deterministic; flag stays claimed (no retry storm)
        _note_attempt(True, provider, model)
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
            except Exception:  # noqa: BLE001  # the title broadcast is best effort, the title is stored
                logger.debug("title-gen: title broadcast failed for %s", chat_id, exc_info=True)
        _record_cost(chat, provider, model, usage)
    except Exception:
        logger.exception("title-gen: request_chat_title failed for %s", chat_id)


def _record_cost(chat: dict, provider: str, model: str, usage: "ProviderUsage") -> None:
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
