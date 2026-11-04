"""The title stripper (``storage/chat/chat_titles``): the twin of the
dashboard's ``cleanUserMessageText``, pinned case by case through the one
fixture both sides read, and the default titles taken from it."""

import json
import time

import pytest

from storage.chat.chat_titles import (
    clean_user_text,
    remove_tag_blocks,
    title_from_prompt,
)
from tests._paths import PROXY_DIR
from tests._timing import bound

_CASES = json.loads(
    (PROXY_DIR / "tests" / "fixtures" / "chat_wrappers.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_the_wrappers_fixture(case):
    assert clean_user_text(case["input"]) == case["stripped"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_a_title_is_never_taken_from_a_row_nobody_typed(case):
    title = title_from_prompt(case["input"])
    assert (title == "") == (case["stripped"] is None)


def test_a_delegated_task_is_titled_by_its_first_words():
    wrapped = ("[DELEGATED_WORK]\nDelegated by agent 'boss'. When you finish, write a report.\n"
               "[/DELEGATED_WORK]\n\nReview the plan for the billing page, then report back.")
    assert title_from_prompt(wrapped) == "Review the plan for the billing…"


def test_an_empty_or_wrapper_only_row_titles_nothing():
    assert title_from_prompt("") == ""
    assert title_from_prompt("[DELEGATED_WORK]only[/DELEGATED_WORK]") == ""
    assert title_from_prompt(None) == ""


def test_an_app_action_is_titled_app_and_label():
    framed = ('[Current time: Friday, October 09, 2026 06:00 (6:00 AM) UTC (UTC+00:00)]\n\n'
              '[action from app "Team Status" — Refresh]\n{"x": 1}')
    assert title_from_prompt(framed) == "Team Status — Refresh"


def test_a_long_first_word_is_cut_at_48_characters():
    assert title_from_prompt("x" * 60) == "x" * 48 + "…"


def test_both_tailers_title_nothing_from_a_row_nobody_typed():
    from core.session.codex_rollout_tailer import _title_from_prompt as codex_title
    from core.session.transcript_tailer import _title_from_prompt
    envelope = ("[BACKGROUND_TASK_RESULT]\nTask 'cards' (id: dyn-7, worker chat: x) "
                "finished with status=completed.\n\nOutput:\ndone")
    command = "<command-name>/model</command-name>\n<command-args></command-args>"
    wrapped = ("[DELEGATED_WORK]\nDelegated by agent 'boss'. Write a report.\n"
               "[/DELEGATED_WORK]\n\ncount the files")
    for title in (_title_from_prompt, codex_title):
        assert title(envelope) == ""
        assert title(command) == ""
        assert title(wrapped) == "count the files"


_MB = 1_000_000


@pytest.mark.parametrize("row", [
    pytest.param("<pasted_content>" + " " * _MB + "x", id="a paste open then 1 MB of spaces"),
    pytest.param("<system-reminder>" * (_MB // 17), id="unclosed reminders"),
    pytest.param("<command-name>" * (_MB // 14), id="unclosed command tags"),
    pytest.param("word " * (_MB // 5), id="plain long text"),
    pytest.param("<pasted_content>" + "</pasted_content" * (_MB // 16), id="unterminated close tags"),
    pytest.param("[Current time: x] " * (_MB // 18) + "hi", id="stacked preludes"),
    pytest.param("[Image #1] " * (_MB // 11) + "x", id="a run of image marks"),
    pytest.param("hi " + "The user has attached x " * (_MB // 24), id="near-miss attachment blocks"),
])
def test_a_crafted_1_mb_row_cleans_in_linear_time(row):
    # The send path runs the stripper on the event loop for every message:
    # a quadratic pattern made a 40 KB row take seconds.
    started = time.monotonic()
    clean_user_text(row)
    title_from_prompt(row)
    assert time.monotonic() - started < bound(0.5)


def test_tag_blocks_are_removed_as_the_lazy_pattern_would():
    assert remove_tag_blocks("a<x>1</x>b<y>2</y>c<x>open", ("x", "y")) == "abc<x>open"
    assert remove_tag_blocks("<y>no close <x>in</x> after", ("x", "y")) == "<y>no close  after"
    assert remove_tag_blocks("<x><y></x></y>", ("x", "y")) == "</y>"
    assert remove_tag_blocks("plain", ("x",)) == "plain"


def test_the_tailer_drops_a_crafted_command_record_in_linear_time():
    from core.session.transcript_tailer import _strip_command_noise
    started = time.monotonic()
    assert _strip_command_noise("<command-name>/x</command-name>" * (_MB // 31)) is None
    _strip_command_noise("<command-name>" * (_MB // 14))
    assert time.monotonic() - started < bound(0.5)


_STAMP = "[Current time: Monday, October 26, 2026 20:30 (8:30 PM) UTC (UTC+00:00)]"


def test_a_terminal_photo_message_titles_nothing_and_keeps_its_block_for_the_reader():
    # A photo alone typed into a live terminal, as each TUI journals it: the
    # Claude TUI's image marks ahead of the paste, Codex's line with the
    # platform's attachments block. The reader keeps the block (the only
    # trace of the photos in that row); the title never takes it.
    claude = (f'[Image #1] [Image #2]\n\n<pasted_content id="217e">\n{_STAMP}\n'
              "The user has attached 2 image(s). Read and analyze them using the Read tool:\n- \n- \n"
              '</pasted_content id="217e">\n')
    codex = (f"{_STAMP} The user has attached 1 image(s). Read and analyze them using the Read tool:\n"
             "- /users/u/workspace/uploads/photos/a.jpg\n")
    for line in (claude, codex):
        assert clean_user_text(line).startswith("The user has attached")
        assert title_from_prompt(line) == ""
    files = f"{_STAMP}\n\nThe user has attached 1 file(s):\n- /users/u/workspace/x.pdf (PDF)\n"
    assert title_from_prompt(files) == ""


def test_a_text_with_photos_is_titled_by_its_words_alone():
    line = (f'[Image #1]\n\n<pasted_content id="9a">\n{_STAMP}\nWhich colours are these?\n'
            "The user has attached 1 image(s). Read and analyze them using the Read tool:\n- \n"
            '</pasted_content id="9a">\n')
    assert title_from_prompt(line) == "Which colours are these?"
    headless = ("Compare the two charts please\n\nThe user has attached 2 image(s). Read and analyze "
                "them using the Read tool:\n- /a.png\n- /b.png\n")
    assert title_from_prompt(headless) == "Compare the two charts please"
