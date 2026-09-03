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


def test_app_runtime_reports_scroll_pos():
    # Contract with dashboard AppFrame: the shim posts scroll_pos so the
    # host can slide the solo-app ✕ away with the content.
    from api.apps.apps import APP_RUNTIME
    assert "scroll_pos" in APP_RUNTIME
    assert "window.scrollY" in APP_RUNTIME
