"""Capability-gating for built-in MCPs.

``mcp_registry.manifest_capability_available`` hides a capability-dependent MCP
from the assignable + runtime sets until its backing platform feature exists:
the transcribe MCP needs a usable STT provider; the phone MCP needs usable call
STT+TTS providers (the manifest's ``requires_capability`` field).

DB-free: the audio helpers are exercised by monkeypatching
``audio_provider_store.get_default_provider``.
"""

from types import SimpleNamespace

from services.mcp import mcp_registry
from services.media import audio_service


def _m(cap):
    return SimpleNamespace(name="x-mcp", requires_capability=cap)


# --- manifest_capability_available (the resolver + delegation) ---------------

def test_no_requirement_is_always_available():
    assert mcp_registry.manifest_capability_available(_m(None)) is True
    assert mcp_registry.manifest_capability_available(_m("")) is True


def test_audio_transcribe_delegates_to_audio_service(monkeypatch):
    monkeypatch.setattr(audio_service, "transcribe_capability_available", lambda: True)
    assert mcp_registry.manifest_capability_available(_m("audio_transcribe")) is True
    monkeypatch.setattr(audio_service, "transcribe_capability_available", lambda: False)
    assert mcp_registry.manifest_capability_available(_m("audio_transcribe")) is False


def test_phone_calls_delegates_to_audio_service(monkeypatch):
    monkeypatch.setattr(audio_service, "phone_calls_available", lambda: True)
    assert mcp_registry.manifest_capability_available(_m("phone_calls")) is True
    monkeypatch.setattr(audio_service, "phone_calls_available", lambda: False)
    assert mcp_registry.manifest_capability_available(_m("phone_calls")) is False


def test_unknown_capability_fails_closed():
    # An unrecognised token hides the MCP (fail closed) and must not raise.
    assert mcp_registry.manifest_capability_available(_m("nonexistent_feature")) is False


# --- the audio capability helpers --------------------------------------------

def test_transcribe_available_requires_usable_chat_stt(monkeypatch):
    seen = {}

    def no_provider(ptype, ctx):
        seen["args"] = (ptype, ctx)
        return None

    monkeypatch.setattr(audio_service.audio_provider_store, "get_default_provider", no_provider)
    assert audio_service.transcribe_capability_available() is False
    # It must consult the chat-default STT (what /v1/audio/transcribe uses).
    assert seen["args"] == ("stt", "chat")

    # A local STT (no credential slot) is usable → available.
    monkeypatch.setattr(
        audio_service.audio_provider_store, "get_default_provider",
        lambda ptype, ctx: {"id": 1, "credential_key": None},
    )
    assert audio_service.transcribe_capability_available() is True


def test_phone_calls_requires_both_call_providers(monkeypatch):
    table: dict[tuple[str, str], dict] = {}
    monkeypatch.setattr(
        audio_service.audio_provider_store, "get_default_provider",
        lambda ptype, ctx: table.get((ptype, ctx)),
    )
    # Neither configured.
    assert audio_service.phone_calls_available() is False
    # Only the call STT.
    table[("stt", "calls")] = {"id": 1, "credential_key": None}
    assert audio_service.phone_calls_available() is False
    # Both call providers present (local) → available.
    table[("tts", "calls")] = {"id": 2, "credential_key": None}
    assert audio_service.phone_calls_available() is True


def test_audio_tts_delegates_to_audio_service(monkeypatch):
    monkeypatch.setattr(audio_service, "tts_capability_available", lambda: True)
    assert mcp_registry.manifest_capability_available(_m("audio_tts")) is True
    monkeypatch.setattr(audio_service, "tts_capability_available", lambda: False)
    assert mcp_registry.manifest_capability_available(_m("audio_tts")) is False


def test_tts_available_checks_chat_then_calls_default(monkeypatch):
    table: dict[tuple[str, str], dict] = {}
    monkeypatch.setattr(
        audio_service.audio_provider_store, "get_default_provider",
        lambda ptype, ctx: table.get((ptype, ctx)),
    )
    # Nothing configured.
    assert audio_service.tts_capability_available() is False
    # A phone-first install: only the calls-context TTS default exists — the
    # generate endpoint falls back to it, so the capability must too.
    table[("tts", "calls")] = {"id": 2, "credential_key": None}
    assert audio_service.tts_capability_available() is True
    # A chat default takes precedence when present (and must be usable).
    table[("tts", "chat")] = {"id": 3, "credential_key": "audio-x"}
    monkeypatch.setattr(audio_service, "credential_resolver", lambda key: "")
    assert audio_service.tts_capability_available() is False
    monkeypatch.setattr(audio_service, "credential_resolver", lambda key: "secret")
    assert audio_service.tts_capability_available() is True
