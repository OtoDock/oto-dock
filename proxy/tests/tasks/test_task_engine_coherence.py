"""Cross-engine model coherence for task/delegate runs (1.5, W1-B1/B2).

Root cause of the 2026-08 "empty task turn": a task with a LAYER override
(``override_execution_path='codex-cli'``) resolved its model from the
agent's default (``claude-sonnet-5``) — the provider then 400'd every turn
("The 'claude-sonnet-5' model is not supported when using Codex with a
ChatGPT account") and the run landed as a deceptive ``completed`` with
empty output at $0 (evidence: run-3662047d3d21, run-f8e386f90613; rollouts
show ``task_complete.error``).

Two seams pin the fix:
  * ``config.resolve_agent_model(agent, layer=...)`` skips an agent-default
    model foreign to the effective layer (used by task_config_builder,
    config_builder and the scheduler chat stamp);
  * ``ChatStreamPump.last_error`` records the terminal engine error so the
    scheduler stamps the run FAILED with the provider message instead of
    ``completed``/empty (consulted like the usage-limit notice).
"""

from __future__ import annotations

import asyncio


import config
from core.events.common_events import ERROR, PRODUCER_DONE, CommonEvent
from core.events.stream_pump import ChatStreamPump


def _seed_models():
    from storage import subscription_store
    subscription_store.sync_builtin_models("claude-code-cli", [
        {"value": "claude-sonnet-5", "label": "Sonnet 5", "provider": "anthropic"},
    ])
    subscription_store.sync_builtin_models("codex-cli", [
        {"value": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "provider": "openai"},
    ])


def _make_agent(slug: str = "coherence-agent") -> str:
    from storage import agent_store
    agent_store.create_agent(
        slug, "Coherence",
        execution_path="claude-code-cli",
        default_model="claude-sonnet-5",
    )
    return slug


def test_layer_override_skips_foreign_default_model(temp_db):
    _seed_models()
    slug = _make_agent()
    # Historical behavior unchanged without a layer:
    assert config.resolve_agent_model(slug) == "claude-sonnet-5"
    # Same layer → default honored:
    assert config.resolve_agent_model(slug, layer="claude-code-cli") == "claude-sonnet-5"
    # FOREIGN layer → the default is skipped, the layer's own first enabled
    # model is used (the W1-B1 fix — never a claude model on a codex turn):
    assert config.resolve_agent_model(slug, layer="codex-cli") == "gpt-5.6-sol"


def test_scheduler_chat_stamp_is_layer_aware(temp_db):
    # The chat row's model drives usage attribution at record time — it must
    # match the wire resolution for an overridden run.
    _seed_models()
    slug = _make_agent("coherence-agent-2")
    assert config.get_cli_model(slug, layer="codex-cli") == "gpt-5.6-sol"
    assert config.get_cli_model(slug, layer=None) == "claude-sonnet-5"


def test_pump_records_terminal_engine_error(temp_db):
    # The pump BREAKS on ERROR — a non-empty last_error is terminal by
    # construction, and the scheduler consults it exactly like the
    # usage-limit notice (run → failed + provider message).
    from storage import database as task_store
    cid = "chat-coherence-err"
    task_store.create_chat(cid, "user-admin", "coherence-agent", "default",
                           model="claude-sonnet-5",
                           execution_path="codex-cli")

    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        producer = asyncio.create_task(asyncio.sleep(0))
        pump = ChatStreamPump(
            chat_id=cid, session_id="s-coherence-err", producer=producer,
            event_queue=queue, perm_queue=None, source_type="task",
        )
        queue.put_nowait(CommonEvent(type=ERROR, data={
            "message": "The 'claude-sonnet-5' model is not supported when "
                       "using Codex with a ChatGPT account.",
        }))
        queue.put_nowait(CommonEvent(type=PRODUCER_DONE, data={}))
        task = pump.start()
        await asyncio.wait_for(task, timeout=10)
        return pump

    pump = asyncio.run(run())
    assert "not supported when using Codex" in pump.last_error


def test_pump_last_error_empty_on_clean_run(temp_db):
    from storage import database as task_store
    cid = "chat-coherence-clean"
    task_store.create_chat(cid, "user-admin", "coherence-agent", "default",
                           model="claude-sonnet-5",
                           execution_path="claude-code-cli")

    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        producer = asyncio.create_task(asyncio.sleep(0))
        pump = ChatStreamPump(
            chat_id=cid, session_id="s-coherence-clean", producer=producer,
            event_queue=queue, perm_queue=None, source_type="task",
        )
        queue.put_nowait(CommonEvent(type=PRODUCER_DONE, data={}))
        task = pump.start()
        await asyncio.wait_for(task, timeout=10)
        return pump

    pump = asyncio.run(run())
    # Empty output alone NEVER fails a run — only a recorded engine error.
    assert pump.last_error == ""


def test_delegation_continue_drops_poisoned_model_pin(temp_db):
    # A pre-fix lane whose chat row carries a model belonging to a DIFFERENT
    # layer must keep continuing: the re-derivation drops the model pin
    # instead of converting the poison into an explicit override that
    # validation rejects. Positive evidence only — a registry-unknown model
    # (custom/retired id) keeps its pin.
    from api.tasks.delegation import (
        _model_is_cross_layer_poison,
        _model_served_by_layer,
    )
    _seed_models()
    assert _model_served_by_layer("claude-sonnet-5", "claude-code-cli") is True
    assert _model_served_by_layer("claude-sonnet-5", "codex-cli") is False
    assert _model_is_cross_layer_poison("claude-sonnet-5", "codex-cli") is True
    assert _model_is_cross_layer_poison("claude-sonnet-5", "claude-code-cli") is False
    assert _model_is_cross_layer_poison("claude-opus-unknown", "claude-code-cli") is False
