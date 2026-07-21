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


def _iter_code_files():
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.name in EXCLUDE_FILES:
                continue
            if any(part in EXCLUDE_DIR_PARTS for part in path.parts):
                continue
            if path.suffix not in SCAN_EXTS and not path.name.endswith(".example"):
                continue
            yield path


@pytest.mark.parametrize("pattern", STALE_PATTERNS)
def test_no_stale_voice_identifier(pattern):
    hits = []
    for path in _iter_code_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern in line:
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:100]}")
    assert not hits, (
        f"Stale voice-feature identifier {pattern!r} (renamed to phone in P1b):\n"
        + "\n".join(hits[:20])
    )


@pytest.mark.parametrize("phrase", STALE_PHRASES)
def test_no_stale_voice_phrase(phrase):
    hits = []
    for path in _iter_code_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if phrase in line.lower():
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:100]}")
    assert not hits, (
        f'Stale phone-feature phrase {phrase!r} in code — use "phone", not "voice":\n'
        + "\n".join(hits[:20])
    )
