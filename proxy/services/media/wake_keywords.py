"""Wake-word keyword compilation — "hey <agent>" lines for the browser spotter.

The dashboard's on-device keyword spotter (WASM, English GigaSpeech BPE
model) accepts keywords ONLY as pre-encoded BPE token sequences validated
against the model's tokens.txt — raw text is a hard error in the spotter.
This module owns that encoding: agent DISPLAY NAMES (never slugs) are
uppercased (the BPE model is uppercase and does no case folding), encoded
with the model's own bpe.model via sentencepiece, and validated
piece-by-piece; a name that cannot be expressed in the vocab is silently
skipped — such agents are not name-wakeable and the platform word still
reaches them via the favorite.

Line format consumed by the spotter: ``<tokens> [:boost] [#threshold]
@<agent-slug>`` — the ``@`` tag is echoed back verbatim in a detection
result, so the routing target rides inside the keyword itself; ``:``/``#``
are optional per-line sensitivity modifiers (platform lines only). The
platform word "hey OtoDock" compiles to the user's favorite agent
(default_agent when it is among the user's ADDED agents, else the first
added agent alphabetically).

Scope: the keyword pool is the caller's ADDED agents (their user_agents
rows) — NOT everything an admin can access. Voice-waking an agent nobody
attached to this account is always wrong, so admins get the same
added-only pool as members; an admin with nothing added gets
enabled:false ("no wakeable agents") even though the Agents landing page
falls back to the platform-wide list for them.

All functions are synchronous — call via asyncio.to_thread.
"""

from __future__ import annotations

import logging
import unicodedata

from config import BASE_DIR
from storage import agent_store
from storage import database as task_store

logger = logging.getLogger(__name__)

ENCODER_DIR = BASE_DIR / "assets" / "kws" / "encoder"

# The platform word compiles as SEVERAL spelling variants, all tagged with
# the favorite. The spellings are LETTER-TOKEN chains on purpose — measured,
# not theorized (2026-08-15, real TTS clips of "Hey OtoDock" through the
# real wasm engine): the earlier word-phonetic respellings ("HEY OH TO
# DOCK" family) scored ZERO recall in every voice at every boost/threshold,
# while the acoustic model actually emits the letter tokens — an agent line
# ▁HE Y ▁O T O fired on every clip. The letter chains below recall 4/4 with
# zero false fires on control speech; they only work WITH the per-line
# boost/threshold riders (a single bare literal at the global threshold was
# the pre-riders failure mode):
#   HEY OTO DOCK  -> ▁HE Y ▁O T O ▁DO CK      ("oh-toh dock")
#   HEY OTTO DOCK -> ▁HE Y ▁O T T O ▁DO CK    (strongest under boost)
#   HEY AUTODOCK  -> ▁HE Y ▁A U T O D O CK    (the Greek "aw-to" reading)
#   HEY AUTO DOCK -> ▁HE Y ▁A U T O ▁DO CK
PLATFORM_PHRASES = (
    "HEY OTO DOCK",
    "HEY OTTO DOCK",
    "HEY AUTODOCK",
    "HEY AUTO DOCK",
)
_HEY = "HEY "

# (sentencepiece processor, frozenset of tokens.txt symbols); loaded once.
_encoder: tuple[object, frozenset[str]] | None = None
_encoder_failed = False


def _load_encoder() -> tuple[object, frozenset[str]] | None:
    """Load bpe.model + tokens.txt once; None (logged) when unavailable so a
    broken install degrades to wake-word-off instead of 500ing settings."""
    global _encoder, _encoder_failed
    if _encoder is not None:
        return _encoder
    if _encoder_failed:
        return None
    try:
        import sentencepiece as spm

        sp = spm.SentencePieceProcessor()
        sp.load(str(ENCODER_DIR / "bpe.model"))
        tokens: set[str] = set()
        with open(ENCODER_DIR / "tokens.txt", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if parts:
                    tokens.add(parts[0])
        _encoder = (sp, frozenset(tokens))
        return _encoder
    except Exception:
        logger.exception("wake-word keyword encoder unavailable")
        _encoder_failed = True
        return None


def normalize_phrase(text: str) -> str:
    """Uppercase + strip diacritics + drop anything outside A-Z/apostrophe.
    Returns '' when nothing English-phonetic survives (→ not wakeable)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    out: list[str] = []
    for ch in stripped.upper():
        if "A" <= ch <= "Z" or ch == "'":
            out.append(ch)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def encode_phrase(phrase: str) -> str | None:
    """BPE-encode a normalized phrase; None when any piece is outside the
    model vocabulary (the spotter would refuse the whole keywords string)."""
    enc = _load_encoder()
    if enc is None or not phrase:
        return None
    sp, tokens = enc
    pieces = sp.encode(phrase, out_type=str)
    if not pieces or any(p not in tokens for p in pieces):
        return None
    return " ".join(pieces)


def _float_setting(key: str, default: float) -> float:
    """A platform-settings float with a hard code fallback. The fallback also
    swallows non-positive values: per-line keyword modifiers are fed to an
    unguarded std::stof in the spotter (a junk value would abort the engine
    for EVERY user at init), and 0 means "fall back to global" downstream —
    never emit either."""
    raw = task_store.get_platform_setting(key) or ""
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _threshold() -> float:
    return _float_setting("audio_wake_word_threshold", 0.30)


def _resolve_favorite(user, added: list[str]) -> str | None:
    """default_agent (when added) → first added agent, alphabetically. A
    deleted/lost favorite falls through; an admin-starred agent they never
    ADDED also falls through (the star PUT validates with can_access_agent,
    so an admin can favorite outside their added set — accepted drift)."""
    pool = set(added)
    fav = getattr(user, "default_agent", "") or ""
    if fav in pool:
        return fav
    for slug in getattr(user, "agents", None) or []:
        if slug in pool:
            return slug
    return added[0] if added else None


def build_for_user(user) -> dict:
    """Compile the wake keyword payload for one user.

    The per-user opt-in is NOT checked here — the client only listens when
    the user's toggle is on; this endpoint only derives from the caller's
    ADDED agents (their user_agents rows — the roster they curated), never
    from the admin's platform-wide access. Non-browser callers: none exist
    (a session JWT can reach this route but simply gets enabled:false when
    it carries no added agents).
    """
    if task_store.get_platform_setting("audio_wake_word_enabled") == "false":
        return {
            "enabled": False,
            "reason": "wake word is turned off by the administrator",
            "threshold": 0.0, "keywords": "", "agents": [],
            "platform_target": None,
        }

    rows = agent_store.get_all_agents()  # sorted by slug — stable collisions
    # ADDED-only, admins included: can_access_agent short-circuits on
    # is_admin, which once let "hey <name>" wake an agent only OTHER
    # accounts had added. Strict for admins with nothing added (no
    # discovery-style fall-back-to-all): waking needs an explicit attach.
    added_set = set(getattr(user, "agents", None) or [])
    added = [r["slug"] for r in rows if r["slug"] in added_set]
    by_slug = {r["slug"]: r for r in rows}

    lines: list[str] = []
    seen_tokens: set[str] = set()
    agents_out: list[dict] = []
    for slug in added:
        display = by_slug[slug].get("display_name") or slug
        # Normalize the NAME alone first: a name with no English-phonetic
        # residue must be skipped, not collapsed to a bare "HEY" keyword
        # that would fire on every hey.
        name_part = normalize_phrase(display)
        encoded = encode_phrase(_HEY + name_part) if name_part else None
        wakeable = encoded is not None and encoded not in seen_tokens
        if wakeable and encoded is not None:
            seen_tokens.add(encoded)
            lines.append(f"{encoded} @{slug}")
        agents_out.append(
            {"slug": slug, "display_name": display, "wakeable": wakeable}
        )

    platform_target = _resolve_favorite(user, added)
    if platform_target:
        # Per-line sensitivity on the platform lines only: ':' boosts the
        # trie path score, '#' lowers the acoustic trigger threshold below
        # the global one; the '@' tag must stay LAST on the line. Bare agent
        # lines get 0-entries engine-side, which fall back to the global
        # keywordsScore/threshold. Emitted AFTER the agent lines on purpose
        # (agent keywords win dedupe collisions); the shared ▁HE Y prefix
        # picking up the boost was verified harmless for agent detection in
        # the Node smoke.
        boost = _float_setting("audio_wake_word_platform_boost", 2.0)
        thr = _float_setting("audio_wake_word_platform_threshold", 0.20)
        for phrase in PLATFORM_PHRASES:
            encoded = encode_phrase(phrase)
            if encoded and encoded not in seen_tokens:
                seen_tokens.add(encoded)
                lines.append(f"{encoded} :{boost:g} #{thr:g} @{platform_target}")

    if not lines:
        reason = (
            "no wakeable agents"
            if _load_encoder() is not None
            else "keyword encoder unavailable"
        )
        return {
            "enabled": False, "reason": reason, "threshold": 0.0,
            "keywords": "", "agents": agents_out, "platform_target": None,
        }

    return {
        "enabled": True,
        "reason": "ok",
        "threshold": _threshold(),
        "keywords": "\n".join(lines),
        "agents": agents_out,
        "platform_target": platform_target,
    }
