"""Provider implementations and the discovery registry.

Import providers via :mod:`audio.providers.registry` (lazy by name) rather than
reaching into the per-category modules directly — that keeps a provider's
import-time model load from firing on every proxy startup.
"""
