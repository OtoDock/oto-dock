"""Provider usage-limit detection on task-run output.

A run killed by a provider limit streams the notice as NORMAL result text
(observed 2026-07-08: four in-flight runs all "completed" with "You've hit
your session limit · resets …" as their output). `_limit_notice` is the
happy-path detector that flips those runs to `failed`; these tests pin its
shapes and, critically, its non-matches.
"""


from services.scheduler.scheduler import _limit_notice


class TestMatches:
    def test_claude_session_limit(self):
        out = "You've hit your session limit · resets 3pm"
        assert _limit_notice(out) == out

    def test_claude_typographic_apostrophe(self):
        assert _limit_notice("You’ve hit your usage limit · resets 10am") is not None

    def test_claude_api_style_notice(self):
        assert _limit_notice("Claude AI usage limit reached|1751980800") is not None

    def test_generic_usage_limit_reached(self):
        assert _limit_notice("Usage limit reached. Try again later.") is not None

    def test_notice_after_real_work(self):
        """A run that produced output in earlier turns, then died on the limit:
        the notice is the final message, joined after real content."""
        out = "Here is the report you asked for...\n\nYou've hit your session limit · resets 6pm"
        line = _limit_notice(out)
        assert line is not None and line.startswith("You've hit your session limit")

    def test_case_insensitive(self):
        assert _limit_notice("YOU'VE HIT YOUR SESSION LIMIT") is not None


class TestNonMatches:
    def test_empty(self):
        assert _limit_notice("") is None

    def test_ordinary_output(self):
        assert _limit_notice("Deployed the fix and verified the logs.") is None

    def test_discusses_limits_mid_text(self):
        """A task whose real deliverable talks about usage limits must not match:
        the notice has to START the final line."""
        out = ("Analysis: several task runs failed because you've hit your "
               "session limit too often this week; consider a second account.")
        assert _limit_notice(out) is None

    def test_notice_not_last_line(self):
        """Real output AFTER the phrase means the run kept working — not a
        limit death."""
        out = ("You've hit your session limit was the error yesterday.\n"
               "Today everything completed normally.")
        assert _limit_notice(out) is None

    def test_limit_word_without_notice_shape(self):
        assert _limit_notice("Rate limit config updated to 100 rps.") is None
