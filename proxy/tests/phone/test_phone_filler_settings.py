"""The phone settings seed: the thinking fillers (merged per-language lists +
the latency gate) and the call prompts (the adapter's texts, and the boot walk
that moves an unedited prompt of an earlier release to the current text).

Fillers are one merged list per language, playback is latency-gated
(delay + repeat damper), and enable/disable lives per route — there are no
global enable keys to seed.
"""

from __future__ import annotations

import json

from adapters.phone import PHONE_CONTEXT, PHONE_OUTBOUND
from services.phone import phone_config_seed as seed
from services.phone.phone_config_seed import seed_phone_config
from storage import database as task_store

# The inbound prompt 1.7.1 seeded (the newest of the three shipped texts): what
# every install of that release holds unless an admin edited it.
SEEDED_1_7_1_INBOUND = (
    "You are on a live phone call. This is voice — not chat.\n"
    "RULES:\n"
    "- ALWAYS respond in the SAME LANGUAGE the user speaks. Greek → Greek. English → English. Never switch language mid-call.\n"
    "- Keep responses SHORT: 1-3 sentences maximum. Summarize instead of listing details.\n"
    "- Talk naturally like a real person on the phone — use casual, conversational language.\n"
    "- Avoid stiff/formal phrasing. Say things the way you'd say them out loud.\n"
    "- NEVER read tables, lists, JSON, code, or formatted output aloud. Describe results naturally in plain speech.\n"
    "- If there are many items (services, devices, etc.), give a high-level summary, NOT individual details.\n"
    "- Don't spell out URLs, paths, or IPs unless asked.\n"
    "- Before ANY tool call, FIRST write one short spoken line saying what you're doing (e.g. 'Let me check that for you' — in the SAME language as the call). NEVER go silent into a tool — on a call, silence feels broken. During multi-step tool work, add a brief spoken progress line between steps, then summarize the result in 1-2 short sentences.\n"
    "- To end the call (e.g. user says goodbye or the conversation is clearly over), append [CALL_COMPLETE] at the end of your final message. The system will strip it before speaking.\n"
)
# The oldest shipped outbound text, also what the dashboard's former Restore
# Default button wrote.
SEEDED_FIRST_OUTBOUND = (
    "You are on a live phone call that YOU placed to complete a task. This is voice — not chat.\n"
    "RULES:\n"
    "- ALWAYS respond in the SAME LANGUAGE the other person speaks. Greek → Greek. English → English.\n"
    "- Keep responses SHORT: 1-3 sentences maximum. Summarize instead of listing details.\n"
    "- Talk naturally like a real person on the phone — use casual, conversational language.\n"
    "- Avoid stiff/formal phrasing. Say things the way you'd say them out loud.\n"
    "- NEVER read tables, lists, JSON, code, or formatted output aloud. Describe results naturally in plain speech.\n"
    "- If there are many items (services, devices, etc.), give a high-level summary, NOT individual details.\n"
    "- Don't spell out URLs, paths, or IPs unless asked.\n"
    "- When using tools: say 'One moment' before, then summarize the result in 1-2 short sentences.\n"
    "- Complete the task you were given, politely and professionally.\n"
    "- If you need information from your manager during the call, emit [QUESTION: your question here] in your response. The system will relay the question while the call stays active.\n"
    "- When the task is complete or clearly cannot be completed, end your final message with [CALL_COMPLETE].\n"
    "- The [CALL_COMPLETE] and [QUESTION:] markers are stripped before speaking — they're signals for the system.\n"
)


def test_the_seed_writes_the_adapters_call_prompts(temp_db):
    seed_phone_config()
    assert task_store.get_platform_setting("phone_context_inbound") == PHONE_CONTEXT
    assert task_store.get_platform_setting("phone_context_outbound") == PHONE_OUTBOUND
    assert "end_call" in PHONE_CONTEXT and "ask_initiating_agent" in PHONE_OUTBOUND


def test_the_current_call_prompts_are_in_no_shipped_set():
    for key, text in (("phone_context_inbound", PHONE_CONTEXT),
                      ("phone_context_outbound", PHONE_OUTBOUND)):
        assert seed._prompt_digest(text) not in seed.SHIPPED_CALL_PROMPTS[key]


def test_an_unedited_call_prompt_of_an_earlier_release_takes_the_current_text(temp_db):
    # A textarea save drops the trailing newline: the identity ignores it.
    task_store.set_platform_setting("phone_context_inbound", SEEDED_1_7_1_INBOUND.rstrip())
    task_store.set_platform_setting("phone_context_outbound", SEEDED_FIRST_OUTBOUND)
    assert seed.remap_shipped_call_prompts() == ["phone_context_inbound", "phone_context_outbound"]
    assert task_store.get_platform_setting("phone_context_inbound") == PHONE_CONTEXT
    assert task_store.get_platform_setting("phone_context_outbound") == PHONE_OUTBOUND
    assert seed.remap_shipped_call_prompts() == []            # idempotent


def test_an_edited_call_prompt_stays(temp_db):
    mine = SEEDED_1_7_1_INBOUND + "- Always offer the Tuesday discount.\n"
    task_store.set_platform_setting("phone_context_inbound", mine)
    assert seed.remap_shipped_call_prompts() == []
    assert task_store.get_platform_setting("phone_context_inbound") == mine


def test_seed_writes_merged_lists_and_latency_gates(temp_db):
    seed_phone_config()

    phrases = json.loads(task_store.get_platform_setting("phone_thinking_phrases"))
    assert all(isinstance(v, list) for v in phrases.values())
    assert phrases["en"] and phrases["el"]

    assert task_store.get_platform_setting("phone_thinking_filler_delay_s") == "0.5"
    assert task_store.get_platform_setting(
        "phone_thinking_filler_repeat_delay_s") == "2.0"
    # No global enable keys — the per-route toggles are authoritative.
    assert task_store.get_platform_setting("phone_backchannel_enabled") == ""
    assert task_store.get_platform_setting("phone_thinking_filler_enabled") == ""


def test_seed_preserves_admin_edited_values(temp_db):
    task_store.set_platform_setting(
        "phone_thinking_phrases", json.dumps({"en": ["righto"]}))
    task_store.set_platform_setting("phone_thinking_filler_delay_s", "1.25")

    seed_phone_config()

    assert json.loads(
        task_store.get_platform_setting("phone_thinking_phrases")) == {"en": ["righto"]}
    assert task_store.get_platform_setting("phone_thinking_filler_delay_s") == "1.25"
