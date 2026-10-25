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
