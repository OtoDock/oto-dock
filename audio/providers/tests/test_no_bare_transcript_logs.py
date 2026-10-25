"""Lint guard: no provider may log a transcript at INFO+ (Rule #1).

Verbatim transcripts are PII. They must route through ``audio.log_policy``
(``log_transcript`` / ``STTProvider._log_transcript``), which logs at DEBUG
unless ``OTO_AUDIO_LOG_TRANSCRIPTS=1``. A bare ``logger.info(...{transcript}...)``
(or warning/error) in a provider is forbidden. DEBUG logging of transcripts is
allowed (the gate permits it).

The guard reads each module's syntax tree, like the phone PIN guard
(``phone/tests/test_pin_log_hygiene.py``), so an argument wrapped onto the
next line is read with its call. A call at INFO or above fails when any of
its arguments (the message's f-string values, the lazy arguments, an
``extra=`` value) names an identifier, an attribute or a string key
(``alt["transcript"]``, ``data.get("transcript")``) holding
``transcript``; a message that only says the word carries no value and
passes.
"""

import ast
import pathlib

# audio/providers/  (this file is audio/providers/tests/test_*.py)
PROVIDERS_DIR = pathlib.Path(__file__).resolve().parents[1]

_LOUD = {"info", "warning", "warn", "error", "exception", "critical"}


def _key(node: ast.AST | None) -> str:
    """A string constant used as a key, else nothing."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _names_transcript(expr: ast.AST) -> bool:
    for n in ast.walk(expr):
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
        if "transcript" in ident.lower():
            return True
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
        if any(_names_transcript(v) for v in values):
            out.append(f"{label}:{call.lineno}: {ast.unparse(call)[:160]}")
    return out


def test_no_bare_transcript_logs_in_providers():
    offenders = []
    for py in PROVIDERS_DIR.rglob("*.py"):
        if "tests" in py.parts:
            continue
        rel = str(py.relative_to(PROVIDERS_DIR.parent))
        offenders += offenders_in(py.read_text(encoding="utf-8"), rel)
    assert not offenders, "Bare transcript logging found (use log_transcript):\n" + "\n".join(offenders)


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
    ):
        assert offenders_in(src), src
    for src in (
        'logger.debug("heard %s", transcript)',
        'logger.log(logging.DEBUG, "x %s", transcript)',
        'logger.info("transcript received (%d chars)", len(text))',
        'log_transcript(logger, "final", transcript)',
        'logger.info("%s", data.get("is_final"))',
        'logger.info("transcript keys: %s", sorted(data))',
    ):
        assert offenders_in(src) == [], src
