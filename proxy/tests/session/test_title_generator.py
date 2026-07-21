"""Tests for services.title_generator — LLM chat-title generation.

Covers title cleanup (emoji kept, quotes/whitespace), provider selection (enable
toggle, admin pin, auto-ladder), the atomic once-claim, and the
request_chat_title orchestration (task-/meeting- skip, generate→update→broadcast
→meter, exactly-once, disabled no-op, empty-title no-retry). The provider /
credential layer and the LLM call are monkeypatched so no subscriptions or
network are needed; the DB calls hit the real test database (temp_db).
"""

import asyncio

from core.layers.providers.base import ProviderUsage


# ---------------------------------------------------------------------------
# _clean_title
# ---------------------------------------------------------------------------

def test_clean_title_keeps_emoji_and_strips_quotes():
    from services import title_generator as tg
    assert tg._clean_title('"🚀 Deploy pipeline"') == "🚀 Deploy pipeline"
    assert tg._clean_title("“Hello world”") == "Hello world"
    assert tg._clean_title("  spaced   out \n title ") == "spaced out title"


def test_clean_title_clips_length():
    from services import title_generator as tg
    out = tg._clean_title("🚀 " + "word " * 40)
    assert len(out) <= 60
    assert out.startswith("🚀")


# ---------------------------------------------------------------------------
# _select_provider — enable toggle, admin pin, auto-ladder
# ---------------------------------------------------------------------------

def _configure(monkeypatch, configured):
    """Make ``configured`` the set of 'configured' providers (no DB / relay)."""
    from services import title_generator as tg
    monkeypatch.setattr(tg, "_provider_configured", lambda p: p in configured)


def test_select_disabled_returns_none(temp_db, monkeypatch):
    from storage import database as db
    from services import title_generator as tg
    _configure(monkeypatch, {"groq"})
    db.set_platform_setting("title_generation_enabled", "0")
    assert tg._select_provider() is None


def test_select_none_configured(temp_db, monkeypatch):
    from services import title_generator as tg
    _configure(monkeypatch, set())
    assert tg._select_provider() is None


def test_select_auto_ladder_prefers_groq(temp_db, monkeypatch):
    from services import title_generator as tg
    _configure(monkeypatch, {"groq", "anthropic"})
    assert tg._select_provider() == ("groq", "openai/gpt-oss-120b")


def test_select_auto_ladder_falls_to_anthropic(temp_db, monkeypatch):
    from services import title_generator as tg
    _configure(monkeypatch, {"anthropic"})
    assert tg._select_provider() == ("anthropic", "claude-haiku-4-5")


def test_select_admin_pin_overrides_ladder(temp_db, monkeypatch):
    from storage import database as db
    from services import title_generator as tg
    _configure(monkeypatch, {"groq", "openai"})
    db.set_platform_setting("title_generation_model", "gpt-5.4-mini")
    assert tg._select_provider() == ("openai", "gpt-5.4-mini")


def test_select_pin_unconfigured_falls_back_to_ladder(temp_db, monkeypatch):
    from storage import database as db
    from services import title_generator as tg
    _configure(monkeypatch, {"groq"})  # openai NOT configured
    db.set_platform_setting("title_generation_model", "gpt-5.4-mini")
    assert tg._select_provider() == ("groq", "openai/gpt-oss-120b")


def test_status_shape(temp_db, monkeypatch):
    from services import title_generator as tg
    _configure(monkeypatch, {"groq", "anthropic"})
    st = tg.title_generation_status()
    assert st["enabled"] is True
    assert st["active"] is True
    assert st["active_provider"] == "groq"
    assert {o["provider"] for o in st["options"]} == {"groq", "anthropic"}


# ---------------------------------------------------------------------------
# claim_title_generation — atomic once-flag
# ---------------------------------------------------------------------------

def test_claim_title_generation_once(temp_db):
    from storage import database as db
    db.create_chat("c1", "user-1", "agent-x")
    assert db.claim_title_generation("c1") is True
    assert db.claim_title_generation("c1") is False
    assert db.get_chat("c1")["title_generated"] is True


def test_claim_missing_chat_false(temp_db):
    from storage import database as db
    assert db.claim_title_generation("nope") is False


# ---------------------------------------------------------------------------
# request_chat_title — orchestration
# ---------------------------------------------------------------------------

def _patch_provider(monkeypatch, title="🚀 Cool Title"):
    """resolve→groq; generate→(title, usage). Capture broadcast + usage rows."""
    from services import title_generator as tg
    captured = {"broadcast": [], "usage": []}

    monkeypatch.setattr(tg, "resolve_title_provider",
                        lambda: ("groq", "openai/gpt-oss-120b", "key", ""))

    async def _fake_generate(*a, **k):
        return title, ProviderUsage(input_tokens=120, output_tokens=8)
    monkeypatch.setattr(tg, "generate_title", _fake_generate)

    from services.notifications import notification_manager
    monkeypatch.setattr(notification_manager, "broadcast_chat_title",
                        lambda u, c, t, agent="": captured["broadcast"].append((u, c, t, agent)))
    from services.billing import usage_service
    monkeypatch.setattr(usage_service, "record_turn_usage",
                        captured["usage"].extend)
    return captured


def test_request_skips_meeting_only(temp_db, monkeypatch):
    from services import title_generator as tg
    calls = {"resolve": 0}

    def _count():
        calls["resolve"] += 1
        return None
    monkeypatch.setattr(tg, "resolve_title_provider", _count)

    asyncio.run(tg.request_chat_title("meeting-99"))
    assert calls["resolve"] == 0  # short-circuits before resolve
    # Task chats are NOT skipped anymore (they title like any sidebar chat).
    asyncio.run(tg.request_chat_title("task-99"))
    assert calls["resolve"] == 1


def test_request_generates_updates_broadcasts_and_meters(temp_db, monkeypatch):
    from storage import database as db
    from services import title_generator as tg
    db.create_chat("c1", "user-1", "agent-x")
    db.add_chat_message("c1", "user", "how do I deploy the proxy with zero downtime?")
    db.add_chat_message("c1", "assistant", "Use a blue-green rollout behind the proxy...")
    cap = _patch_provider(monkeypatch, "🚀 Zero-downtime deploy")

    asyncio.run(tg.request_chat_title("c1"))

    chat = db.get_chat("c1")
    assert chat["title"] == "🚀 Zero-downtime deploy"
    assert chat["title_generated"] is True
    # agent rides the broadcast so shared-only chats fan out to the agent's
    # users (chat_status_targets) instead of the synthetic owner's nobody.
    assert cap["broadcast"] == [("user-1", "c1", "🚀 Zero-downtime deploy", "agent-x")]
    assert len(cap["usage"]) == 1
    row = cap["usage"][0]
    assert row["source_type"] == "title-generation"
    assert row["source_key"] == "title_generation"
    assert row["message_count"] == 0
    assert row["provider"] == "groq"


def test_request_is_exactly_once(temp_db, monkeypatch):
    from storage import database as db
    from services import title_generator as tg
    db.create_chat("c1", "user-1", "agent-x")
    db.add_chat_message("c1", "user", "first question here")
    db.add_chat_message("c1", "assistant", "first answer here")
    cap = _patch_provider(monkeypatch, "🎯 First Title")

    asyncio.run(tg.request_chat_title("c1"))
    asyncio.run(tg.request_chat_title("c1", assistant_excerpt="x"))  # claim already taken

    assert db.get_chat("c1")["title"] == "🎯 First Title"
    assert len(cap["usage"]) == 1  # generated only once


def test_request_disabled_keeps_deterministic_and_does_not_claim(temp_db, monkeypatch):
    from storage import database as db
    from services import title_generator as tg
    db.create_chat("c1", "user-1", "agent-x")
    db.update_chat("c1", title="my first prompt")
    db.add_chat_message("c1", "user", "my first prompt")
    monkeypatch.setattr(tg, "resolve_title_provider", lambda: None)

    asyncio.run(tg.request_chat_title("c1"))

    chat = db.get_chat("c1")
    assert chat["title"] == "my first prompt"
    assert chat["title_generated"] is False  # not claimed → retriable later


def test_manual_rename_blocks_llm_upgrade(temp_db, monkeypatch):
    """A user rename sets title_generated=TRUE (api/agents/chats.py), so the LLM title
    never overwrites a manual one."""
    from storage import database as db
    from services import title_generator as tg
    db.create_chat("c1", "user-1", "agent-x")
    db.add_chat_message("c1", "user", "something")
    db.add_chat_message("c1", "assistant", "reply")
    db.update_chat("c1", title="My Manual Title", title_generated=True)  # the rename
    cap = _patch_provider(monkeypatch, "🤖 LLM Title")

    asyncio.run(tg.request_chat_title("c1"))

    assert db.get_chat("c1")["title"] == "My Manual Title"  # untouched
    assert cap["usage"] == []


def test_request_empty_title_keeps_claim_no_broadcast(temp_db, monkeypatch):
    from storage import database as db
    from services import title_generator as tg
    db.create_chat("c1", "user-1", "agent-x")
    db.add_chat_message("c1", "user", "hello there")
    db.add_chat_message("c1", "assistant", "hi")
    cap = _patch_provider(monkeypatch, "")  # model returned nothing usable

    asyncio.run(tg.request_chat_title("c1"))

    assert db.get_chat("c1")["title_generated"] is True  # claimed (no retry storm)
    assert cap["broadcast"] == []
    assert cap["usage"] == []


def test_request_titles_task_chats(temp_db, monkeypatch):
    # Task-run chats list in the sidebar's task mode — the LLM upgrade applies
    # to every one of them (delegate workers and scheduled runs alike).
    from storage import database as db
    from services import title_generator as tg
    db.create_chat("task-run-d1", "user-1", "agent-x", origin="delegated",
                   title="Lane A")
    db.add_chat_message("task-run-d1", "user", "research the market for X")
    db.add_chat_message("task-run-d1", "assistant",
                        "Here is what the market for X looks like...")
    _patch_provider(monkeypatch, "🔬 X market research")

    asyncio.run(tg.request_chat_title("task-run-d1"))
    assert db.get_chat("task-run-d1")["title"] == "🔬 X market research"

    # Plain scheduled-run task chat: titles too now.
    db.create_chat("task-run-p1", "user-1", "agent-x")
    db.add_chat_message("task-run-p1", "user", "cron job prompt")
    db.add_chat_message("task-run-p1", "assistant", "done")
    _patch_provider(monkeypatch, "⏰ Cron job check")
    asyncio.run(tg.request_chat_title("task-run-p1"))
    assert db.get_chat("task-run-p1")["title"] == "⏰ Cron job check"


def test_deterministic_title_rule():
    from services.title_generator import deterministic_title
    assert deterministic_title("check the backups and report status now please") \
        == "check the backups and report status…"
    assert deterministic_title("short prompt") == "short prompt"
    assert deterministic_title("") == "New Chat"
    assert deterministic_title(
        "[Current time: 2026-07-12 10:00 EEST]\ncheck the backups"
    ) == "check the backups"


def test_task_chat_first_message_stamps_deterministic_title(temp_db):
    # The scheduler creates task chats untitled; the first user message stamps
    # the deterministic title at persistence (storage-level backstop). Later
    # rounds and pre-titled chats (delegate workers) are untouched.
    from storage import database as db
    db.create_chat("task-run-t1", "task::agent-x", "agent-x")
    db.add_chat_message("task-run-t1", "user",
                        "summarize the overnight alerts from every monitored host")
    assert db.get_chat("task-run-t1")["title"] \
        == "summarize the overnight alerts from every…"
    db.add_chat_message("task-run-t1", "user", "second round prompt")
    assert db.get_chat("task-run-t1")["title"] \
        == "summarize the overnight alerts from every…"

    db.create_chat("task-run-t2", "task::agent-x", "agent-x", title="Lane B")
    db.add_chat_message("task-run-t2", "user", "worker prompt")
    assert db.get_chat("task-run-t2")["title"] == "Lane B"

    # Normal chats keep the chat layer's send-time titling — no stamp here.
    db.create_chat("c-plain", "user-1", "agent-x")
    db.add_chat_message("c-plain", "user", "hello")
    assert (db.get_chat("c-plain")["title"] or "") == ""
