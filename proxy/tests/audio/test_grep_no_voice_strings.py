"""Guard: no stale voice-feature identifiers survive the voice→phone rename.

The voice server / routes / config / endpoints / credentials AND the session
discriminator (``client_type`` / ``source_type`` / ``context`` / ``exclude_from``
/ ``voice_mode`` / ``voice_context_override``) were all renamed to "phone". The
ONLY place "voice" legitimately survives is the audio *modality* — VAD ("Voice
Activity Detection") and TTS *voices* (``voice_id`` / ``voice_map``) — none of
which appear as a quoted ``"voice"`` token, so the patterns below don't touch
them. The ``audio/`` package (which owns those terms) is intentionally not
scanned.

Docs (``*.md``) are excluded — only code identifiers are guarded here.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT

# Stale identifiers that MUST be fully renamed (zero occurrences in code).
STALE_PATTERNS = [
    "voice_routes",                 # table → phone_routes
    "voice_store",                  # module → phone_route_store
    "voice_config",                 # modules → phone_config*
    "VoiceAdapter",                 # class → PhoneAdapter
    "VoiceRoute",                   # type / dataclass → PhoneRoute
    "/ws/voice",                    # ws routes → /ws/phone(-management)
    "VOICE_SERVER_URL",             # env → PHONE_SERVER_URL
    "voice_server_url",             # session token → phone_server_url
    "voice-deepgram",
    "voice-cartesia",
    "voice-groq",
    "voice-ami",                    # infra creds → audio-* / phone-ami
    "voice_call_type",              # warmup field → call_type
    "voice_route_id",               # warmup field → phone_route_id
    "assemble_voice",               # → assemble_phone_config
    "notify_voice",                 # → notify_phone_config_changed
    "build_voice_agent",            # → build_phone_agent_config
    "resolve_voice_execution",      # → resolve_phone_execution_target
    "seed_voice_config",            # → seed_phone_config
    "otodock-voice",                # systemd unit → otodock-phone
    "voice_route_outbound_select",  # manifest magic string → phone_route_outbound_select
    "useVoiceRoutes",
    "useVoiceSettings",
    "useVoiceCredential",           # dashboard hooks → usePhone*
    "ws_voice_handler",
    "ws_voice_management",          # handlers → ws_phone*
    # --- session discriminator (renamed to "phone" in the full pass) ---
    "voice_mode",                   # flag → phone_mode
    "voice_context_override",       # column / field → phone_context_override
    '"voice"',                      # client_type / source_type / context / exclude_from value
    "'voice'",                      # dashboard source_type value
]

# Phone-feature *prose* (comments / docstrings) that must not creep back in.
# Matched case-insensitively. Each describes the phone server / phone-call
# feature — never the audio modality (TTS voices, VAD) or the dashboard chat
# voice-mode, so none collide with a legitimate "voice".
STALE_PHRASES = [
    "voice server",
    "voice barge-in",
    "voice hangup",
    "voice-management",
    "voice path",
    "voice driver",
]

SCAN_DIRS = ["proxy", "phone", "dashboard/src", "mcps", "scripts"]
SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".json", ".sh", ".service", ".toml"}
# ``sessions`` = runtime-generated per-session MCP config caches (gitignored);
# they carry stale env until the proxy restarts + satellites re-pull. Not source.
# ``video-tools`` = the community MCP's AUDIO domain legitimately names a
# "voice" EQ/denoise preset — nothing to do with the phone-feature rename.
EXCLUDE_DIR_PARTS = {"venv", "node_modules", "__pycache__", ".git", "dist", "build",
                     ".pytest_cache", "sessions", "video-tools"}
EXCLUDE_FILES = {"test_grep_no_voice_strings.py"}


# A scan that read next to nothing would pass on nothing.
_MIN_FILES = 500


def _code_files() -> list:
    files = []
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        # Pruned on the way down: the virtualenvs hold most of the tree's files.
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_PARTS]
            for name in names:
                path = Path(root) / name
                if name in EXCLUDE_FILES or not path.is_file():
                    continue
                if path.suffix not in SCAN_EXTS and not name.endswith(".example"):
                    continue
                files.append(path)
    return files


@functools.cache
def _hits() -> dict[str, tuple[str, ...]]:
    """Every stale pattern and phrase with the lines that carry it, from one
    read of the tree for the whole module (a pattern matches with its case,
    a phrase without)."""
    files = _code_files()
    assert len(files) > _MIN_FILES, f"the scan read {len(files)} files: its roots moved"
    found: dict[str, list[str]] = {needle: [] for needle in (*STALE_PATTERNS, *STALE_PHRASES)}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lowered = text.lower()
        patterns = [p for p in STALE_PATTERNS if p in text]
        phrases = [p for p in STALE_PHRASES if p in lowered]
        if not patterns and not phrases:
            continue
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for pattern in patterns:
                if pattern in line:
                    found[pattern].append(f"{rel}:{lineno}: {line.strip()[:100]}")
            for phrase in phrases:
                if phrase in low:
                    found[phrase].append(f"{rel}:{lineno}: {line.strip()[:100]}")
    return {needle: tuple(lines) for needle, lines in found.items()}


@pytest.mark.parametrize("pattern", STALE_PATTERNS)
def test_no_stale_voice_identifier(pattern):
    hits = _hits()[pattern]
    assert not hits, (
        f"Stale voice-feature identifier {pattern!r} (renamed to phone in P1b):\n"
        + "\n".join(hits[:20])
    )


@pytest.mark.parametrize("phrase", STALE_PHRASES)
def test_no_stale_voice_phrase(phrase):
    hits = _hits()[phrase]
    assert not hits, (
        f'Stale phone-feature phrase {phrase!r} in code — use "phone", not "voice":\n'
        + "\n".join(hits[:20])
    )


def test_the_scan_finds_a_pattern_and_a_phrase_where_they_are(tmp_path, monkeypatch):
    """The scan's own proof on a planted tree: a pattern with its case, a
    phrase without, each at its line, an excluded folder left alone."""
    import sys

    module = sys.modules[__name__]
    (tmp_path / "proxy" / "venv").mkdir(parents=True)
    (tmp_path / "proxy" / "a.py").write_text(
        'ok = 1\nx = "voice"\ny = VoiceAdapter()\n# The Voice Server answers\nz = voiceadapter\n')
    (tmp_path / "proxy" / "venv" / "b.py").write_text("VoiceAdapter\n")
    (tmp_path / "proxy" / "notes.md").write_text("VoiceAdapter\n")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "_MIN_FILES", 0)
    _hits.cache_clear()
    try:
        found = {needle: lines for needle, lines in _hits().items() if lines}
    finally:
        _hits.cache_clear()
    assert found == {
        '"voice"': ('proxy/a.py:2: x = "voice"',),
        "VoiceAdapter": ("proxy/a.py:3: y = VoiceAdapter()",),
        "voice server": ("proxy/a.py:4: # The Voice Server answers",),
    }
