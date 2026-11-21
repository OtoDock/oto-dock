"""The proxy's phone settings allowlist and the daemon's reads stay equal.

The proxy stores only the ``phone_*`` keys its allowlist names
(``proxy/services/phone/phone_settings.py``); the daemon reads its settings
through ``ConfigManager._s(...)``. A key the daemon reads and the proxy
refuses would be a setting nobody can save, so the two sets are compared
here: every daemon key (minus the audio-prefixed ones, which the audio
settings route stores, and the ones the config assembler injects from other
tables) is in the allowlist, and every allowlisted key is either read by
the daemon or seeded.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / "proxy" / "services" / "phone" / "phone_settings.py"
SEED = ROOT / "proxy" / "services" / "phone" / "phone_config_seed.py"
DAEMON = ROOT / "phone" / "config_manager.py"

#: Keys the assembler injects from the provider and server rows: never
#: stored as ``phone_*`` settings.
INJECTED = {"ami_host", "ami_port", "ami_username", "deepgram_endpointing_ms"}

_READ_RE = re.compile(r'self\._s\("([a-z_0-9]+)"')
_SEED_RE = re.compile(r'^\s*"(audio|phone)_([a-z_0-9]+)":', re.MULTILINE)


def _allowlisted() -> set[str]:
    tree = ast.parse(ALLOWLIST.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "PHONE_SETTINGS"):
            return {k.value for k in node.value.keys}
    raise AssertionError("PHONE_SETTINGS not found")


def _seeded() -> tuple[set[str], set[str]]:
    audio, phone = set(), set()
    for prefix, key in _SEED_RE.findall(SEED.read_text(encoding="utf-8")):
        (audio if prefix == "audio" else phone).add(key)
    return audio, phone


def test_every_key_the_daemon_reads_is_storable():
    allowed = _allowlisted()
    audio_seeded, phone_seeded = _seeded()
    daemon = set(_READ_RE.findall(DAEMON.read_text(encoding="utf-8")))
    assert daemon, "no reads found"
    phone_reads = daemon - audio_seeded - INJECTED
    missing = sorted(phone_reads - allowed)
    assert missing == [], f"the daemon reads these and the proxy refuses to store them: {missing}"
    assert sorted(phone_seeded - allowed) == [], "a seeded phone key the proxy refuses"


def test_every_allowlisted_key_is_read_or_seeded():
    allowed = _allowlisted()
    _, phone_seeded = _seeded()
    daemon = set(_READ_RE.findall(DAEMON.read_text(encoding="utf-8")))
    stray = sorted(allowed - daemon - phone_seeded)
    assert stray == [], f"allowlisted keys nobody reads or seeds: {stray}"
    assert not allowed & INJECTED


#: What a call's detector is built from (``pipeline/core.py``): the seven
#: values it takes and the offset its silence is made of.
_DETECTOR = ("vad_threshold", "vad_speech_pad_ms", "vad_min_energy_rms", "vad_silence_offset_ms",
             "bargein_threshold", "bargein_debounce_ms", "bargein_chunk_ratio", "bargein_silence_duration_ms")
_SEED_VALUE_RE = re.compile(r'^\s*"(?:audio|phone)_([a-z_0-9]+)":\s*"([^"]*)"', re.MULTILINE)


def test_the_seed_and_the_daemon_agree_on_the_detectors_values():
    """A fresh install runs the seed's values and a test builds the detector
    from the daemon's defaults. Nothing else holds the two equal, and a test
    on the detector means little if they drift apart."""
    from config_manager import ConfigManager

    seeded = dict(_SEED_VALUE_RE.findall(SEED.read_text(encoding="utf-8")))
    daemon = ConfigManager()
    for key in _DETECTOR:
        assert float(seeded[key]) == float(getattr(daemon, key)), key
    # The feed is no setting, for calls or for voice conversations: the seed
    # writes no such row and the daemon reads none.
    for key in ("vad_context", "voice_vad_context"):
        assert key not in seeded and not hasattr(daemon, key)
