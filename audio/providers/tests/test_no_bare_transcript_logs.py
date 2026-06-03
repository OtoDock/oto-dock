"""Lint guard: no provider may log a transcript at INFO+ (Rule #1).

Verbatim transcripts are PII. They must route through ``audio.log_policy``
(``log_transcript`` / ``STTProvider._log_transcript``), which logs at DEBUG
unless ``OTO_AUDIO_LOG_TRANSCRIPTS=1``. A bare ``logger.info(...{transcript}...)``
(or warning/error) in a provider is forbidden. DEBUG logging of transcripts is
allowed (the gate permits it).
"""

import pathlib
import re

# audio/providers/  (this file is audio/providers/tests/test_*.py)
PROVIDERS_DIR = pathlib.Path(__file__).resolve().parents[1]

# logger.info/warning/error on the same line as the word "transcript".
# Routed calls use self._log_transcript(...) / log_transcript(logger, ...),
# which don't match this pattern. "transcribe" does not contain "transcript".
_BANNED = re.compile(r"logger\.(info|warning|error)\([^\n]*\btranscript\b")


def test_no_bare_transcript_logs_in_providers():
    offenders = []
    for py in PROVIDERS_DIR.rglob("*.py"):
        if "tests" in py.parts:
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _BANNED.search(line):
                offenders.append(f"{py.relative_to(PROVIDERS_DIR.parent)}:{lineno}: {line.strip()}")
    assert not offenders, "Bare transcript logging found (use log_transcript):\n" + "\n".join(offenders)
