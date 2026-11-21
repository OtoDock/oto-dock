"""Lint guard: no voice code may log a person's words at INFO+ (Rule #1).

Verbatim transcripts are PII, and so is what the agent says back on a call.
They must route through ``audio.log_policy`` (``log_transcript`` /
``STTProvider._log_transcript``), which logs them at DEBUG unless
``OTO_AUDIO_LOG_TRANSCRIPTS=1``. A bare ``logger.info(...{transcript}...)``
(or warning/error) is forbidden in the audio package, the phone service and
the proxy's voice paths (``_REACH``). DEBUG logging of transcripts is
allowed (the gate permits it).

The guard reads each module's syntax tree, like the phone PIN guard
(``phone/tests/test_pin_log_hygiene.py``), so an argument wrapped onto the
next line is read with its call. A call at INFO or above fails when any of
its arguments (the message's f-string values, the lazy arguments, an
``extra=`` value) names an identifier, an attribute or a string key
(``alt["transcript"]``, ``data.get("transcript")``) that holds words: one
whose parts between underscores include ``_WORD_PARTS``, or one named
``final``. A message that only says the word carries no value and passes.
What never reaches the line passes too: a value inside ``len()``, the
condition of an ``x if cond else y``, a name that ends in a unit of time
(``_s``, ``_ms``) or opens with ``t_`` (a clock reading), and the
expressions of ``_NOT_WORDS``.
"""

import ast
import logging
import pathlib

import pytest
from audio import log_policy
from audio.log_policy import log_transcript

# The repository's root (this file is audio/providers/tests/test_*.py).
REPO = pathlib.Path(__file__).resolve().parents[3]

# Where a person's words pass: the audio package, the phone service, and the
# proxy's voice paths (dictation and voice mode, the duplex session, calls).
_REACH = (
    "audio",
    "phone",
    "proxy/api/audio",
    "proxy/api/duplex",
    "proxy/api/phone",
    "proxy/services/media/audio_service.py",
    "proxy/services/media/duplex_service.py",
    "proxy/services/phone",
    "proxy/ws/audio.py",
    "proxy/ws/duplex.py",
    "proxy/ws/duplex_attach.py",
    "proxy/ws/phone.py",
    "proxy/ws/phone_attach.py",
    "proxy/ws/phone_management.py",
)

# The one function that may log the words, at the level the policy picks.
_OWNER = "audio/log_policy.py"

_WORD_PARTS = {"transcript", "transcripts", "text", "texts", "joined", "utterance", "utterances"}

# Text that is never a person's words: a filler phrase is set by an admin
# and every caller hears the same one.
_NOT_WORDS = {"clip.text"}

_LOUD = {"info", "warning", "warn", "error", "exception", "critical"}


def _key(node: ast.AST | None) -> str:
    """A string constant used as a key, else nothing."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _holds_words(ident: str) -> bool:
    parts = ident.lower().strip("_").split("_")
    if parts[-1] in ("s", "ms") or (len(parts) > 1 and parts[0] == "t"):
        return False
    return ident.lower() == "final" or bool(_WORD_PARTS & set(parts))


def _names_words(expr: ast.AST) -> bool:
    stack = [expr]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len":
            continue
        if isinstance(n, ast.IfExp):
            stack += [n.body, n.orelse]
            continue
        if isinstance(n, ast.Attribute) and ast.unparse(n) in _NOT_WORDS:
            continue
        if isinstance(n, ast.Name):
            ident = n.id
        elif isinstance(n, ast.Attribute):
            ident = n.attr
        elif isinstance(n, ast.Subscript):
            ident = _key(n.slice)  # alt["transcript"]
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get":
            ident = _key(n.args[0] if n.args else None)  # data.get("transcript")
        else:
            ident = ""
        if ident and _holds_words(ident):
            return True
        stack.extend(ast.iter_child_nodes(n))
    return False


def _loud_calls(tree: ast.AST):
    """Every logging call at INFO or above: ``x.info(...)`` and its siblings,
    and ``x.log(level, ...)`` at a level other than ``logging.DEBUG``."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr in _LOUD:
            yield node, node.args
        elif node.func.attr == "log" and node.args:
            level = ast.unparse(node.args[0])
            if level not in ("logging.DEBUG", "DEBUG", "10"):
                yield node, node.args[1:]


def offenders_in(source: str, label: str = "<src>") -> list[str]:
    out = []
    for call, args in _loud_calls(ast.parse(source)):
        values = list(args) + [kw.value for kw in call.keywords]
        if any(_names_words(v) for v in values):
            out.append(f"{label}:{call.lineno}: {ast.unparse(call)[:160]}")
    return out


# Folders that hold no source of ours: the tests, an environment made inside the
# tree (``scripts/dev-setup.sh --phone`` makes ``phone/venv``) and a package
# install's build output (``audio/build``).
_NOT_SOURCE = {"tests", "venv", ".venv", "build", "node_modules", "__pycache__"}


def _modules():
    for entry in _REACH:
        path = REPO / entry
        assert path.exists(), f"{entry} is gone: name where its code went in _REACH"
        for py in [path] if path.is_file() else sorted(path.rglob("*.py")):
            rel = py.relative_to(REPO).as_posix()
            if _NOT_SOURCE & set(py.relative_to(REPO).parts) or rel == _OWNER:
                continue
            yield rel, py


def test_no_bare_transcript_logs():
    offenders = []
    for rel, py in _modules():
        offenders += offenders_in(py.read_text(encoding="utf-8"), rel)
    assert not offenders, "A person's words logged at INFO+ (use log_transcript):\n" + "\n".join(offenders)


def test_the_reach_holds_the_call_pipeline():
    read = {rel for rel, _ in _modules()}
    assert {"phone/pipeline/turn.py", "phone/pipeline/core.py", "audio/providers/stt/deepgram.py",
            "proxy/ws/audio.py"} <= read


def test_the_guard_reads_a_call_wrapped_over_lines():
    for src in (
        'logger.info("heard %s", transcript)',
        'logger.info(\n    "heard %s",\n    transcript,\n)',
        'logger.warning(f"final: {self._final_transcript}")',
        'log.error("x", extra={"t": result.transcript})',
        'logger.log(logging.INFO, "x %s",\n           alt.transcript)',
        'logger.exception("failed on %r", transcripts[0])',
        'logger.info("%s", alt["transcript"])',
        'logger.info("%s", data.get("transcript"))',
        'logger.warning(f"got {msg[\'final_transcript\']}")',
        'logger.info("[%s] Turn dispatching (%s segments): %s", peer, count, joined)',
        'logger.info("[%s] STT final flush (%s): %s", peer, source, final)',
        'logger.info("re-listen completed the turn: %s", text)',
        'logger.info("pre-tool text: %s", self.state._parallel_pre_tool_text[:60])',
        'logger.info("%s", msg["text"])',
        'logger.info("%s", text if text else "-")',
    ):
        assert offenders_in(src), src
    for src in (
        'logger.debug("heard %s", transcript)',
        'logger.log(logging.DEBUG, "x %s", transcript)',
        'logger.info("transcript received (%d chars)", len(text))',
        'log_transcript(logger, "final", transcript)',
        'logger.info("%s", data.get("is_final"))',
        'logger.info("transcript keys: %s", sorted(data))',
        'logger.info("timed out (%ss)", self.stt.transcript_wait_timeout_s)',
        'logger.info("%.0fms", (t_text - t0) * 1000)',
        'logger.info("first text in %.0fms", first_text_ms)',
        'logger.info("%s", "" if text else ", no words yet")',
        'logger.info("Backchannel: playing %r", clip.text)',
    ):
        assert offenders_in(src) == [], src


@pytest.mark.parametrize("opted_in", [False, True])
def test_log_transcript_keeps_an_event_without_its_words(caplog, monkeypatch, opted_in):
    monkeypatch.setattr(log_policy, "LOG_TRANSCRIPTS", opted_in)
    caplog.set_level("DEBUG", logger="t")
    log = logging.getLogger("t")
    log_transcript(log, "Turn dispatching (2 segments)", "call me back tomorrow", event=True)
    log_transcript(log, "Deepgram final", "call me back tomorrow")
    log_transcript(log, "Turn dispatching (1 segments)", "", event=True)
    lines = [(r.levelname, r.getMessage()) for r in caplog.records]
    if opted_in:
        assert lines == [("INFO", "Turn dispatching (2 segments): call me back tomorrow"),
                         ("INFO", "Deepgram final: call me back tomorrow")]
    else:
        assert lines == [("INFO", "Turn dispatching (2 segments): 21 chars"),
                         ("DEBUG", "Turn dispatching (2 segments): call me back tomorrow"),
                         ("DEBUG", "Deepgram final: call me back tomorrow")]
