"""The manifest's bounds and the prompt button's new-chat option.

Load-bearing: 32 actions and no more; the bounds that carry a manifest
moved with the cap; ``new_chat`` is stored only when asked for, so every
manifest without it signs as before; a press is counted per person across
chats, so a button that starts a new chat each time stays bounded.
"""

from __future__ import annotations

import json

import pytest

from api.apps import app_actions
from api.sharing import external
from services.apps import app_deploy, manifest as _mf
from services.community import template_sig
from storage import database as task_store
from ws import artifact_interactions as _ai

AGENT = "caps-agent"


def _feed(i: int) -> dict:
    return {"id": f"a{i}", "label": f"Action {i}", "type": "send_prompt", "prompt": f"Do {i}"}


def test_an_app_declares_thirty_two_actions_and_no_more():
    actions_json, err = _mf.validate_actions([_feed(i) for i in range(32)], AGENT, shared=True)
    assert actions_json is not None and len(json.loads(actions_json)) == 32, err
    refused, err = _mf.validate_actions([_feed(i) for i in range(33)], AGENT, shared=True)
    assert refused is None and err == "at most 32 actions"
    # Thirty-two full prompts are past the size bound: the bound still holds.
    big = [{**_feed(i), "prompt": "x" * 4000} for i in range(32)]
    assert _mf.validate_actions(big, AGENT, shared=True) == (None, "manifest too large")
    assert _mf.validate_actions(big[:15], AGENT, shared=True)[0] is not None


def test_the_bounds_that_carry_a_manifest_moved_with_the_cap():
    assert _mf.MAX_ACTIONS == 32 == app_actions.BATCH_MAX_CALLS == external.BATCH_MAX_CALLS
    assert _mf.MAX_MANIFEST_BYTES == 64 * 1024 < app_deploy.APP_JSON_MAX_BYTES


def test_new_chat_is_stored_only_when_asked_for_and_nothing_else_signs_differently():
    plain = [{"id": "edit", "label": "Edit a new video", "type": "send_prompt", "prompt": "Start"}]
    before, _ = _mf.validate_actions(plain, AGENT, shared=True)
    for value in (False, None):
        same, err = _mf.validate_actions([{**plain[0], "new_chat": value}], AGENT, shared=True)
        assert same == before, err                          # the same text, so the same signature
    assert "new_chat" not in (before or "")
    asked, err = _mf.validate_actions([{**plain[0], "new_chat": True}], AGENT, shared=True)
    assert asked is not None and json.loads(asked)[0]["new_chat"] is True, err
    row = {"actions": asked, "files": "", "egress": ""}
    assert task_store.manifest_sig(row) != task_store.manifest_sig({**row, "actions": before})
    for bad in ("yes", 1, "true", [True]):
        refused, err = _mf.validate_actions([{**plain[0], "new_chat": bad}], AGENT, shared=True)
        assert refused is None and "new_chat is true or false" in err
    # Only a prompt button takes it: on any other type the key is dropped.
    feed, _ = _mf.validate_actions([{"id": "f", "label": "F", "type": "data_feed", "feed": "sessions",
                                     "new_chat": True}], AGENT, shared=True)
    assert "new_chat" not in (feed or "")


class _Clock:
    now = 500.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(_ai, "time", c)
    _ai._rate.clear()
    yield c
    _ai._rate.clear()


def test_a_press_is_counted_per_person_across_chats(clock):
    assert _ai.check_app_press("chat-1", "alice", "app")
    assert not _ai.check_app_press("chat-2", "alice", "app")   # a new chat, the same second
    assert _ai.check_app_press("chat-2", "bob", "app")          # another person is not held
    clock.now += 61.0
    passed = 0
    for i in range(20):                                         # a new chat at every press
        clock.now += 1.1
        passed += _ai.check_app_press(f"fresh-{i}", "alice", "app")
    assert passed == _ai.WINDOW_LIMIT                           # twelve a minute, whatever the chat
    assert _ai.check_app_press("fresh-99", "alice", "another-app")
    clock.now += 61.0
    assert _ai.check_app_press("fresh-100", "alice", "app")


def test_presses_that_each_start_a_chat_leave_no_window_behind(clock):
    for i in range(700):                                        # a new chat, so a new key, at every press
        clock.now += 61.0
        assert _ai.check_app_press(f"fresh-{i}", "alice", "app")
    assert len(_ai._rate) <= 513                                # the windows of chats gone quiet are dropped


def test_a_media_link_is_a_read_for_a_templates_owner_check():
    assert "files.media" not in template_sig.OWNER_METHODS and "files.write" in template_sig.OWNER_METHODS
