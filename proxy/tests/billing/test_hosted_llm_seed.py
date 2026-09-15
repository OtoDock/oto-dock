"""M4 backend — default-on hosted LLM seeding + pool integration.

``seed_hosted_llm_subscriptions`` creates a relay platform sub per relay-backed
provider on the direct-llm layer, once (guarded by a platform-settings flag so an
admin disable persists). Seeded relay subs are then acquirable through the pool.
DB-backed via the autouse ``temp_db`` fixture.
"""

from __future__ import annotations

from storage import database as db
from storage.billing import subscription_store as ss


def _relay_providers() -> set[str]:
    return {
        s["provider"] for s in ss.list_subscriptions(
            layer="direct-llm", contribute_platform=True, include_disabled=True,
        )
        if s.get("auth_type") == "relay"
    }


def test_seed_creates_three_relay_subs():
    ss.seed_hosted_llm_subscriptions()
    assert _relay_providers() == {"anthropic", "openai", "groq"}
    assert db.get_platform_setting("hosted_llm_seeded") == "1"


def test_seed_is_idempotent():
    ss.seed_hosted_llm_subscriptions()
    ss.seed_hosted_llm_subscriptions()  # second call → no-op
    subs = [
        s for s in ss.list_subscriptions(
            layer="direct-llm", contribute_platform=True, include_disabled=True,
        )
        if s.get("auth_type") == "relay"
    ]
    assert len(subs) == 3  # not duplicated


def test_seed_respects_admin_disable():
    ss.seed_hosted_llm_subscriptions()
    # Admin disables OpenAI hosted by removing its relay sub.
    openai_sub = next(
        s for s in ss.list_subscriptions(
            layer="direct-llm", contribute_platform=True, include_disabled=True,
        )
        if s.get("auth_type") == "relay" and s["provider"] == "openai"
    )
    ss.delete_subscription(openai_sub["id"])
    assert _relay_providers() == {"anthropic", "groq"}
    # Re-seed (e.g. on restart) must NOT bring OpenAI back — the flag guards it.
    ss.seed_hosted_llm_subscriptions()
    assert _relay_providers() == {"anthropic", "groq"}


def test_seeded_relay_sub_is_acquirable():
    from services.engines import subscription_pool
    subscription_pool._session_subscriptions.clear()
    ss.seed_hosted_llm_subscriptions()

    handle = subscription_pool.acquire_subscription(
        "direct-llm", None, provider="anthropic",
    )
    assert handle is not None
    assert handle.auth_type == "relay"
    assert handle.api_key is None and handle.endpoint_url is None
