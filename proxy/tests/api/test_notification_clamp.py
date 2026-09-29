"""Notification length clamps (2026-08-18): a notification is a headline,
not a report — agents kept posting essay-length bodies that fill the whole
panel on phones. The MCP schema advertises the caps (the model self-corrects
at the tool layer); these server-side clamps are the floor for every caller."""

from api.notifications.notifications import (
    NOTIFICATION_BODY_MAX,
    NOTIFICATION_TITLE_MAX,
    _clamp_text,
)


def test_caps_are_the_operator_agreed_sizes():
    assert NOTIFICATION_TITLE_MAX == 100
    assert NOTIFICATION_BODY_MAX == 480


def test_short_text_passes_through_stripped():
    assert _clamp_text("  hello world  ", 480) == "hello world"


def test_exact_limit_is_untouched():
    s = "x" * 480
    assert _clamp_text(s, 480) == s


def test_over_limit_truncates_with_ellipsis():
    out = _clamp_text("word " * 200, 480)
    assert len(out) <= 480
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")  # trailing space rstripped before …


def test_app_runtime_contract_with_the_host():
    # Contract with dashboard AppFrame: calls coalesce into one app_actions
    # message keyed by call_id, results and open acks come back in-page as
    # window events, and the page never navigates itself (open_target).
    from api.apps.apps import APP_RUNTIME
    for token in ("type:'app_actions'", "call_id", "otodock:action-result",
                  "window.otodock.open", "type:'open_target'",
                  "otodock:open-ack", "content_height"):
        assert token in APP_RUNTIME, token
    assert "scroll_pos" not in APP_RUNTIME
