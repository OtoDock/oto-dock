"""The PostToolUse forwarder must ship the RESULT BODY, not an empty string.

Live-found on T1 (2026-07-08, session N): the hook read ``inp["tool_result"]``
but the CLI's PostToolUse input carries the result under ``tool_response``
(CLI 2.1.201) — so every headless Claude tool pill shipped an empty
``result_content`` ("ok" summaries, no Output section) while codex normal
chats and interactive transcripts showed full output. These tests lock the
key fallback and the stdout+stderr extraction.
"""

from __future__ import annotations

import importlib.util
import io
import json

from tests._paths import PROXY_DIR

_FWD = PROXY_DIR / "hooks" / "tool_result_forwarder.py"
_spec = importlib.util.spec_from_file_location("tool_result_forwarder_body", _FWD)
forwarder = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(forwarder)


class TestExtractResultText:
    def test_bash_stdout(self):
        assert forwarder._extract_result_text(
            {"stdout": "alpha\nbeta\n", "stderr": ""}
        ) == "alpha\nbeta\n"

    def test_bash_stdout_plus_stderr(self):
        # codex's aggregatedOutput interleaves both streams — keep the Claude
        # pills comparable.
        assert forwarder._extract_result_text(
            {"stdout": "alpha\n", "stderr": "oops\n"}
        ) == "alpha\noops\n"

    def test_stderr_only(self):
        assert forwarder._extract_result_text(
            {"stdout": "", "stderr": "boom\n"}
        ) == "boom\n"

    def test_content_blocks(self):
        assert forwarder._extract_result_text(
            {"content": [{"type": "text", "text": "hello"},
                         {"type": "image", "data": "…"}]}
        ) == "hello"

    def test_read_file_content(self):
        # Read responses nest the body under file.content — without the
        # fallback every Read pill summarized as "empty file" (2026-07-11).
        assert forwarder._extract_result_text(
            {"type": "text", "file": {"filePath": "/w/x.html",
                                      "content": "<div>hi</div>",
                                      "numLines": 1}}
        ) == "<div>hi</div>"
        assert forwarder._extract_summary(
            "Read", {}, "<div>hi</div>\nline2") == "2 lines"

    def test_plain_string(self):
        assert forwarder._extract_result_text("just text") == "just text"


def _run_main(monkeypatch, hook_input):
    """Drive main() with a fake stdin/env and capture the POSTed payload."""
    posted = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        posted.append(json.loads(req.data.decode()))
        return _Resp()

    monkeypatch.setattr(forwarder.sys, "stdin", io.StringIO(json.dumps(hook_input)))
    monkeypatch.setattr(forwarder.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
    monkeypatch.setenv("OTO_SESSION_ID", "sess-hook-test")
    monkeypatch.setenv("PROXY_URL", "http://proxy.test")
    monkeypatch.setenv("PROXY_API_KEY", "k")
    forwarder.main()
    return posted


class TestResponseKeyFallback:
    def test_tool_response_key_ships_the_body(self, monkeypatch):
        posted = _run_main(monkeypatch, {
            "tool_name": "Bash",
            "tool_input": {"command": "printf hi"},
            "tool_use_id": "toolu_1",
            "tool_response": {"stdout": "hi\n", "stderr": ""},
        })
        assert len(posted) == 1
        assert posted[0]["result_content"] == "hi\n"
        assert posted[0]["summary"] == "2 lines"
        assert posted[0]["tool_use_id"] == "toolu_1"

    def test_legacy_tool_result_key_still_accepted(self, monkeypatch):
        posted = _run_main(monkeypatch, {
            "tool_name": "Bash",
            "tool_input": {"command": "printf hi"},
            "tool_result": {"stdout": "hi\n"},
        })
        assert posted and posted[0]["result_content"] == "hi\n"

    def test_a_sub_agents_call_carries_its_agent_id(self, monkeypatch):
        # Claude Code sets the hook's agent_id on a sub-agent's call only.
        sub = _run_main(monkeypatch, {
            "tool_name": "Bash", "tool_input": {"command": "true"}, "tool_use_id": "toolu_2",
            "agent_id": "a363e519c4e437799", "agent_type": "general-purpose",
            "tool_response": {"stdout": "", "stderr": ""},
        })
        main = _run_main(monkeypatch, {
            "tool_name": "Bash", "tool_input": {"command": "true"}, "tool_use_id": "toolu_3",
            "tool_response": {"stdout": "", "stderr": ""},
        })
        assert sub[0]["agent_id"] == "a363e519c4e437799"
        assert main[0]["agent_id"] == ""

    def test_missing_result_still_posts_ok_summary(self, monkeypatch):
        posted = _run_main(monkeypatch, {
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
            "tool_response": {"stdout": "", "stderr": ""},
        })
        assert posted and posted[0]["result_content"] == ""
        assert posted[0]["summary"] == "ok"


_POINTER = ("[Image: source: /users/a/.claude/projects/-users-a/s/tool-results/"
            "mcp-maps-blob-1-x.png]")
_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


class TestMcpListResponses:
    """An MCP tool's ``tool_response`` is a LIST of content blocks (CLI
    2.1.289): its text reaches the pill, the CLI's image pointer does not."""

    def test_a_list_response_ships_its_text(self, monkeypatch):
        posted = _run_main(monkeypatch, {
            "tool_name": "mcp__maps__static_map",
            "tool_input": {},
            "tool_use_id": "toolu_2",
            "tool_response": [{"type": "text", "text": "[Resource from maps at gdrive://f/m.png]"},
                              _IMAGE, {"type": "text", "text": _POINTER},
                              {"type": "text", "text": "Route: 12 km"}],
        })
        assert len(posted) == 1
        assert posted[0]["result_content"] == "Route: 12 km"
        assert "result_blocks" not in posted[0]


def _run_with_answers(monkeypatch, capsys, hook_input, answers):
    """Drive main() with one JSON answer per POST; the payloads and stdout."""
    posted = []
    answers = list(answers)

    class _Resp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=0):
        posted.append(json.loads(req.data.decode()))
        return _Resp(json.dumps(answers.pop(0) if answers else {}).encode())

    monkeypatch.setattr(forwarder.sys, "stdin", io.StringIO(json.dumps(hook_input)))
    monkeypatch.setattr(forwarder.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
    monkeypatch.setenv("OTO_SESSION_ID", "sess-hook-test")
    monkeypatch.setenv("PROXY_URL", "http://proxy.test")
    monkeypatch.setenv("PROXY_API_KEY", "k")
    forwarder.main()
    return posted, capsys.readouterr().out


class TestResultFiles:
    """A tool the platform keeps results of: the proxy's answer asks for the
    blocks, a second POST carries them, and the line it answers becomes the
    hook's additionalContext."""

    _INPUT = {
        "tool_name": "mcp__maps__static_map", "tool_input": {}, "tool_use_id": "toolu_3",
        "tool_response": [_IMAGE, {"type": "text", "text": _POINTER},
                          {"type": "text", "text": "Route: 12 km"}],
    }

    def test_an_opted_in_tool_sends_its_blocks_and_prints_the_line(self, monkeypatch, capsys):
        posted, out = _run_with_answers(monkeypatch, capsys, self._INPUT, [
            {"status": "ok", "result_files": True},
            {"status": "ok", "additional_context": "Saved as /workspace/maps/x.png."},
        ])
        assert len(posted) == 2
        second = posted[1]
        assert second["files_only"] is True
        assert second["result_blocks"] == [_IMAGE]
        assert second["result_content"] == "Route: 12 km"
        assert second["tool_use_id"] == "toolu_3"
        assert json.loads(out) == {"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "Saved as /workspace/maps/x.png."}}

    def test_a_tool_not_kept_sends_once_and_prints_nothing(self, monkeypatch, capsys):
        posted, out = _run_with_answers(monkeypatch, capsys, self._INPUT, [{"status": "ok"}])
        assert len(posted) == 1 and out == ""

    def test_a_result_without_binary_blocks_sends_once(self, monkeypatch, capsys):
        text_only = {**self._INPUT, "tool_response": [{"type": "text", "text": "no picture"}]}
        posted, out = _run_with_answers(monkeypatch, capsys, text_only,
                                        [{"status": "ok", "result_files": True}])
        assert len(posted) == 1 and out == ""

    def test_a_failed_second_request_prints_nothing(self, monkeypatch, capsys):
        posted, out = _run_with_answers(monkeypatch, capsys, self._INPUT, [
            {"status": "ok", "result_files": True}, {"detail": "boom"}])
        assert len(posted) == 2 and out == ""

    def test_large_and_surplus_blocks_go_as_their_size(self, monkeypatch):
        monkeypatch.setattr(forwarder, "_FILE_B64_MAX", 16)
        monkeypatch.setattr(forwarder, "_FILES_MAX", 2)
        small = {"type": "image", "data": "AAAA", "mimeType": "image/png"}
        large = {"type": "image", "data": "A" * 40, "mimeType": "image/png"}
        blob = {"type": "resource", "resource": {"uri": "x://f.pdf", "mimeType": "application/pdf",
                                                 "blob": "AAAA"}}
        out = forwarder._binary_blocks([small, large, {"type": "text", "text": "t"}, blob, small])
        assert out[0] == small
        assert out[1] == {"type": "image", "mime": "image/png", "skipped_bytes": 30}
        assert out[2] == blob
        assert out[3] == {"type": "image", "mime": "image/png", "skipped_bytes": 3}

    def test_a_failed_call_sends_no_files(self, monkeypatch, capsys):
        failed = {**self._INPUT, "tool_response": [
            _IMAGE, {"type": "text", "text": "Error: the map service refused"}]}
        posted, out = _run_with_answers(monkeypatch, capsys, failed,
                                        [{"status": "ok", "result_files": True}])
        assert len(posted) == 1 and out == ""

    def test_an_answer_past_the_budget_prints_nothing(self, monkeypatch, capsys):
        import threading
        release = threading.Event()
        monkeypatch.setattr(forwarder, "_BUDGET_S", 1.2)
        posted = []

        class _Resp:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

        def slow_urlopen(req, timeout=0):
            body = json.loads(req.data.decode())
            posted.append(body)
            if body.get("files_only"):
                release.wait(5)
                return _Resp(json.dumps({"additional_context": "late"}).encode())
            return _Resp(json.dumps({"status": "ok", "result_files": True}).encode())

        monkeypatch.setattr(forwarder.sys, "stdin", io.StringIO(json.dumps(self._INPUT)))
        monkeypatch.setattr(forwarder.urllib.request, "urlopen", slow_urlopen)
        monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
        monkeypatch.setenv("OTO_SESSION_ID", "sess-hook-test")
        monkeypatch.setenv("PROXY_URL", "http://proxy.test")
        monkeypatch.setenv("PROXY_API_KEY", "k")
        try:
            forwarder.main()
        finally:
            release.set()
        assert len(posted) == 2
        assert capsys.readouterr().out == ""


# The failure event's input as Claude Code 2.1.325 sends it (probed): the
# result is the ``error`` text the model reads, and there is no ``tool_response``.
_STOPPED = {
    "hook_event_name": "PostToolUseFailure",
    "tool_name": "Bash",
    "tool_input": {"command": "python3 grow.py", "description": "Grow"},
    "tool_use_id": "toolu_stop",
    "error": "Exit code 1\n\nThat command used too much memory and was stopped.",
    "is_interrupt": False,
    "duration_ms": 1419,
}


class TestFailedCalls:
    """A failed call runs PostToolUseFailure, not PostToolUse: its card gets
    the text the model read, flagged, so the cost engine skips it."""

    def test_a_stopped_command_posts_its_sentence_flagged(self, monkeypatch):
        posted = _run_main(monkeypatch, _STOPPED)
        assert len(posted) == 1
        body = posted[0]
        assert body["result_content"] == _STOPPED["error"]
        assert "That command used too much memory and was stopped." in body["result_content"]
        assert body["is_error"] is True
        assert body["summary"] == "error: Exit code 1"
        assert body["tool_use_id"] == "toolu_stop"
        assert body["tool_command"] == "python3 grow.py"

    def test_a_failed_read_names_its_path(self, monkeypatch):
        posted = _run_main(monkeypatch, {
            "hook_event_name": "PostToolUseFailure", "tool_name": "Read",
            "tool_input": {"file_path": "/workspace/missing.txt"}, "tool_use_id": "toolu_r",
            "error": "File does not exist. Note: your current working directory is /workspace.",
        })
        assert posted[0]["is_error"] is True
        assert posted[0]["tool_paths"] == ["/workspace/missing.txt"]
        assert posted[0]["summary"].startswith("error: File does not exist.")

    def test_an_mcp_failure_is_an_error_whatever_its_words(self, monkeypatch, capsys):
        # A JSON-RPC error's text names no error: the event says it failed.
        posted, out = _run_with_answers(monkeypatch, capsys, {
            "hook_event_name": "PostToolUseFailure", "tool_name": "mcp__maps__static_map",
            "tool_input": {}, "tool_use_id": "toolu_m", "error": "rpc failure on purpose",
            "mcp_server": {"name": "maps"},
        }, [{"status": "ok", "result_files": True}])
        assert len(posted) == 1 and out == ""
        assert posted[0]["is_error"] is True
        assert posted[0]["summary"] == "error: rpc failure on purpose"

    def test_a_failed_display_call_is_forwarded(self, monkeypatch):
        # The display tools are skipped on success (the picture just appeared);
        # a failure shows why it did not.
        ok = _run_main(monkeypatch, {
            "tool_name": "mcp__display__send_url", "tool_input": {}, "tool_response": [],
        })
        failed = _run_main(monkeypatch, {
            "hook_event_name": "PostToolUseFailure", "tool_name": "mcp__display__send_url",
            "tool_input": {}, "error": "Error: the link is not reachable",
        })
        assert ok == []
        assert failed and failed[0]["is_error"] is True

    def test_the_question_cards_stay_skipped(self, monkeypatch):
        assert _run_main(monkeypatch, {
            "hook_event_name": "PostToolUseFailure", "tool_name": "AskUserQuestion",
            "tool_input": {}, "error": "refused",
        }) == []

    def test_a_long_error_is_capped_like_a_result(self, monkeypatch):
        error = "\n".join(f"line {i}" for i in range(800))
        posted = _run_main(monkeypatch, {**_STOPPED, "error": error})
        assert posted[0]["result_content"].endswith("... (300 more lines)")

    def test_an_error_that_is_not_text_becomes_text(self, monkeypatch):
        posted = _run_main(monkeypatch, {**_STOPPED, "error": {"message": "odd"}})
        assert posted[0]["is_error"] is True and "odd" in posted[0]["result_content"]

    def test_a_terminal_posts_nothing_for_a_failure(self, monkeypatch):
        posted = []
        monkeypatch.setattr(forwarder.urllib.request, "urlopen",
                            lambda req, timeout=0: posted.append(req))
        monkeypatch.setattr(forwarder.sys, "stdin", io.StringIO(json.dumps({
            **_STOPPED, "tool_name": "mcp__maps__static_map"})))
        monkeypatch.setenv("OTO_INTERACTIVE", "1")
        monkeypatch.setenv("OTO_SESSION_ID", "sess-hook-test")
        monkeypatch.setenv("PROXY_URL", "http://proxy.test")
        monkeypatch.setenv("PROXY_API_KEY", "k")
        forwarder.main()
        assert posted == []
