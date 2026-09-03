"""Marker regexes for the caller-agent protocol in the call pipeline."""

import re

# [CALL_COMPLETE] / [DUPLEX_COMPLETE] marker — the agent signals the
# conversation should end (calls and dashboard duplex share the pipeline's
# farewell machinery: TTS drains, a still-engaged caller resumes instead).
# One regex on purpose: either marker works in either mode — agents carry
# habits between the two prompts, and both mean exactly "hang up now".
_CALL_COMPLETE_RE = re.compile(r"\[(?:CALL|DUPLEX)_COMPLETE\]")

# [QUESTION: ...] marker — the agent asks the calling (manager) agent.
_QUESTION_RE = re.compile(r"\[QUESTION:\s*(.+?)\]", re.DOTALL)
