"""The sent-prompt note store the interactive tailers consume
(``core.session.transcript_tool_events``): one slot for the dashboard's
owner-less notes, a list for the platform's owned ones, the two keys, the
drops, the hour, and the lock the tail threads share with the loop.
"""
from __future__ import annotations

import threading
import time

import pytest

from core.session import transcript_tool_events as TE
from tests._timing import bound

STAMP = "[Current time: Friday, July 17, 2026 02:00 (2:00 AM) UTC (UTC+00:00)]"
PARALLEL = ("[Parallel activity — for awareness only, no action needed: sibling sessions: "
            "'Inbox' (generating). Check one only if your work depends on it "
            "(peek_session / get_task_result).]")


@pytest.fixture(autouse=True)
def _clean_store():
    TE._sent_prompts.clear()
    yield
    TE._sent_prompts.clear()


def test_typed_key_folds_whitespace_and_the_paste_tags_and_keeps_the_preludes():
    noted = f"{STAMP}\n\ndo the\n  thing"
    journaled = f'<pasted_content id="4">\n{STAMP} do the thing\n</pasted_content>'
    assert TE.typed_key(noted) == TE.typed_key(journaled) == f"{STAMP} do the thing"
    # The stamp is part of what was typed: a line without it is another line.
    assert TE.typed_key("do the thing") != TE.typed_key(noted)


def test_typed_key_meets_a_photo_message_the_tui_took_its_images_from():
    # The terminal's first message with photos: typed with the Read paths,
    # journaled by the Claude TUI with [Image #N] marks in front and the paths
    # gone from the paste.
    noted = (f"{STAMP}\n\nWhich colours are these?\n\nThe user has attached 2 image(s). "
             "Read and analyze them using the Read tool:\n- /users/u/workspace/uploads/photos/a.jpg\n"
             "- /users/u/workspace/uploads/photos/b.jpg\n")
    journaled = (f'[Image #1] [Image #2]\n\n<pasted_content id="217e">\n{STAMP}\nWhich colours are these?\n'
                 "The user has attached 2 image(s). Read and analyze them using the Read tool:\n- \n- \n"
                 '</pasted_content id="217e">\n')
    assert TE.typed_key(noted) == TE.typed_key(journaled) == f"{STAMP} Which colours are these?"
    TE.note_sent_prompt("c-photos", noted)
    assert TE.consume_sent_prompt("c-photos", journaled) is True
    # Files take the same block; the words before it still have to match.
    with_file = f"{STAMP}\n\nread it\n\nThe user has attached 1 file(s):\n- /users/u/workspace/x.pdf (PDF)\n"
    assert TE.typed_key(with_file) == f"{STAMP} read it"
    assert TE.typed_key(with_file) != TE.typed_key(noted)


_MB = 1_000_000


@pytest.mark.parametrize("text", [
    pytest.param(" \n" * (_MB // 2), id="whitespace"),
    pytest.param("<pasted_content" * (_MB // 15), id="unterminated paste tags"),
    pytest.param("[Image #1] " * (_MB // 11), id="image marks"),
    pytest.param(" " * _MB + "The user has attached 1 image(s).", id="a block behind a run"),
])
def test_a_crafted_1_mb_message_keys_in_linear_time(text):
    # Every cold first message is keyed on the event loop.
    started = time.monotonic()
    TE.typed_key(text)
    assert time.monotonic() - started < bound(0.5)


def test_prompt_key_drops_the_leading_preludes_too():
    assert TE.prompt_key(f"{STAMP}\n\n{PARALLEL}\n\ndo the thing") == "do the thing"
    assert TE.prompt_key(f'<pasted_content id="b1"> {STAMP} {PARALLEL} do the\n  work\nplease '
                         '</pasted_content>') == "do the work please"
    assert TE.prompt_key("do the thing") == "do the thing"


def test_an_owner_less_note_replaces_the_previous_one():
    # The dashboard notes a send raw, then stamped: one note, the stamped.
    TE.note_sent_prompt("c1", "hello")
    TE.note_sent_prompt("c1", f"{STAMP}\n\nhello")
    assert TE.consume_sent_prompt("c1", "hello") is False
    assert TE.consume_sent_prompt("c1", f"{STAMP} hello") is True
    assert TE.consume_sent_prompt("c1", f"{STAMP} hello") is False


def test_owned_notes_append_and_consume_one_each():
    TE.note_sent_prompt("c2", "check the log", owner="s1")
    TE.note_sent_prompt("c2", "the report", owner="s1")
    TE.note_sent_prompt("c2", "check the log", owner="s1")
    assert TE.consume_sent_prompt("c2", "check the log") is True
    assert TE.consume_sent_prompt("c2", "check the log") is True
    assert TE.consume_sent_prompt("c2", "check the log") is False
    assert TE.consume_sent_prompt("c2", "the report") is True
    assert "c2" not in TE._sent_prompts


def test_an_owned_note_never_replaces_the_dashboards_and_the_reverse():
    TE.note_sent_prompt("c3", "the person's send")
    TE.note_sent_prompt("c3", "the wake", owner="s1")
    TE.note_sent_prompt("c3", "the person's second send")
    assert TE.consume_sent_prompt("c3", "the person's send") is False
    assert TE.consume_sent_prompt("c3", "the wake") is True
    assert TE.consume_sent_prompt("c3", "the person's second send") is True


def test_a_line_matching_no_note_leaves_every_note():
    TE.note_sent_prompt("c4", "typed", owner="s1")
    assert TE.consume_sent_prompt("c4", "a person's own line") is False
    assert TE.consume_sent_prompt("c4", "typed") is True


def test_drop_sent_prompt_takes_the_newest_of_that_owner_with_that_key():
    TE.note_sent_prompt("c5", "go on", owner="s1")
    TE.note_sent_prompt("c5", "go on", owner="s2")
    assert TE.drop_sent_prompt("c5", "go on", owner="s1") is True
    assert TE.drop_sent_prompt("c5", "go on", owner="s1") is False
    assert TE.consume_sent_prompt("c5", "go on") is True        # s2's
    assert TE.consume_sent_prompt("c5", "go on") is False
    assert TE.drop_sent_prompt("c6", "nothing", owner="s1") is False


def test_drop_sent_prompts_takes_an_owners_notes_and_keeps_the_rest():
    TE.note_sent_prompt("c7", "the person's send")
    TE.note_sent_prompt("c7", "p", owner="s1")
    TE.note_sent_prompt("c7", "q", owner="s1")
    TE.note_sent_prompt("c7", "r", owner="s2")
    assert TE.drop_sent_prompts("c7", owner="s1") == 2
    assert TE.drop_sent_prompts("c7", owner="") == 0            # never the dashboard's
    assert TE.consume_sent_prompt("c7", "p") is False
    assert TE.consume_sent_prompt("c7", "q") is False
    assert TE.consume_sent_prompt("c7", "r") is True
    assert TE.consume_sent_prompt("c7", "the person's send") is True
    assert TE.drop_sent_prompts("c7", owner="s2") == 0


def test_a_note_goes_after_an_hour():
    TE.note_sent_prompt("c8", "stale", owner="s1")
    TE._sent_prompts["c8"][0].noted_at -= TE._SENT_PROMPT_TTL_S + 1
    TE.note_sent_prompt("c9", "fresh", owner="s1")              # any call prunes
    assert TE.consume_sent_prompt("c8", "stale") is False
    assert TE.consume_sent_prompt("c9", "fresh") is True


def test_an_empty_note_is_no_note():
    TE.note_sent_prompt("", "x", owner="s1")
    TE.note_sent_prompt("c10", "", owner="s1")
    assert TE._sent_prompts == {}


def test_concurrent_notes_and_consumes_under_the_lock():
    # The notes are made on the loop and consumed in tail threads: every
    # note is consumed exactly once, whatever the interleaving.
    per_thread, writers, readers = 200, 4, 4
    consumed = [0] * readers
    stop = threading.Event()

    def _write(i: int) -> None:
        for n in range(per_thread):
            TE.note_sent_prompt("c11", f"line {n % 7}", owner=f"s{i}")

    def _read(i: int) -> None:
        while not stop.is_set() or TE._sent_prompts.get("c11"):
            for n in range(7):
                if TE.consume_sent_prompt("c11", f"line {n}"):
                    consumed[i] += 1

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(writers)]
    threads += [threading.Thread(target=_read, args=(i,)) for i in range(readers)]
    for t in threads:
        t.start()
    for t in threads[:writers]:
        t.join()
    stop.set()
    for t in threads[writers:]:
        t.join()
    assert sum(consumed) == per_thread * writers
    assert "c11" not in TE._sent_prompts
