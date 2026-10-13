"""Model ids per provider: the current defaults, the vendor-retired ids and
their successors, and the former defaults a one-time walk clears.

Dependency-free, so the proxy's boot walk reads it without importing a
vendor SDK; each provider module imports its own entries, the single
source. Two kinds of move, the LLM registry's ``MODEL_SUCCESSORS`` rule
applied to audio:

- A RETIRED id (the vendor no longer serves it) resolves to its chain's end
  at every build (``live_model_id`` on the provider bases) and the walk
  rewrites the stored id on every boot: a row holding it would fail.
- A FORMER DEFAULT (still served; the rows stored it because creation
  seeded the default of its day) is cleared from a row once, so the live
  default applies; the proxy remembers which former defaults it walked
  (``audio_former_defaults_walked``), and an admin's later explicit pick of
  the same id stays. Rows no longer store a default at creation.
"""

from __future__ import annotations

#: The model ids that run when a row stores none: the pill's placeholders.
DEFAULT_MODELS: dict[tuple[str, str], dict[str, str]] = {
    ("stt", "deepgram"): {"model_id": "nova-3"},
    ("stt", "elevenlabs"): {"model_id": "scribe_v2_realtime", "batch_model_id": "scribe_v2"},
    ("tts", "cartesia"): {"model_id": "sonic-3.6"},
    ("tts", "elevenlabs"): {"model_id": "eleven_flash_v2_5"},
}

#: Retired id -> successor, per provider (Cartesia retired the three;
#: sonic-3.6 is the current line, backward compatible).
MODEL_SUCCESSORS: dict[tuple[str, str], dict[str, str]] = {
    ("tts", "cartesia"): {
        "sonic": "sonic-3.6",
        "sonic-2": "sonic-3.6",
        "sonic-turbo": "sonic-3.6",
    },
}

#: Former defaults the walk clears from a row once: an id a release SEEDED
#: into rows as the default of its day, so the rows follow the new default.
#: An id that only ever reached a row by an admin's hand (Cartesia's
#: sonic-3.5: the seed never wrote it, the pill showed it as a placeholder)
#: is an explicit pick and stays.
FORMER_DEFAULTS: dict[tuple[str, str], tuple[str, ...]] = {
    ("tts", "cartesia"): (),
}

#: The ``advanced`` keys that hold a model id.
MODEL_ID_KEYS = ("model_id", "batch_model_id")


def successor_of(successors: dict[str, str], model_id: str) -> str:
    """The end of ``model_id``'s successor chain (cycle-safe); the id itself
    when no entry applies."""
    cur = model_id or ""
    seen: set[str] = set()
    while cur in successors and cur not in seen:
        seen.add(cur)
        cur = successors[cur]
    return cur
