"""CartesiaTTS contract tests (no network: construction, the pure surface, and
the voice catalog against a fake modelled on the SDK's ``voices.list``)."""

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace

import pytest
from audio.providers.tts import cartesia as cartesia_mod
from audio.providers.tts.base import UnsupportedProviderOperation
from audio.providers.tts.cartesia import CartesiaTTS
from cartesia import AsyncCartesia
from cartesia.lib._tts import AsyncTTSResourceConnection


def test_capabilities_and_billing():
    assert CartesiaTTS.capabilities.supports_streaming
    assert not CartesiaTTS.capabilities.is_local
    assert CartesiaTTS.billing_unit() == "char"
    assert CartesiaTTS.cost_per_unit() > 0


def test_from_row_loads_voices():
    row = {
        "provider_type": "tts",
        "provider_name": "cartesia",
        "credential_key": "audio-cartesia",
        "voices": {"en": "v-en", "el": "v-el"},
        "advanced": {},
    }
    tts = CartesiaTTS.from_row(row, lambda key: "secret")
    assert tts.voices == {"en": "v-en", "el": "v-el"}


def test_select_voice():
    tts = CartesiaTTS(api_key="x", voices={"en": "v-en", "el": "v-el"})
    assert tts.select_voice("el") == "v-el"
    assert tts.voice_id == "v-el"
    # A language the admin configured no voice for speaks the shipped native
    # voice for it, ahead of the admin's English voice (an install seeded
    # with en and el then speaks German natively).
    assert tts.select_voice("de") == CartesiaTTS.default_voices["de"]
    # A language nobody ships: the admin's English voice (a multilingual
    # voice still pronounces the text), then the shipped English one over a
    # transient current voice_id (deterministic regardless of prior traffic).
    assert tts.select_voice("pt") == "v-en"
    tts2 = CartesiaTTS(api_key="x", voice_id="v-init", voices={"el": "v-el"})
    assert tts2.select_voice("pt") == CartesiaTTS.default_voices["en"]


def test_select_voice_falls_back_to_shipped_defaults():
    # No voices configured at all -> the shipped public-library defaults keep
    # every language speaking out of the box, each natively.
    tts = CartesiaTTS(api_key="x")
    for lang in ("en", "el", "de", "es", "fr", "it"):
        assert tts.select_voice(lang) == CartesiaTTS.default_voices[lang]
    assert tts.select_voice("pt") == CartesiaTTS.default_voices["en"]
    # The admin's voice for a language always wins over the shipped one.
    tts2 = CartesiaTTS(api_key="x", voices={"el": "v-el"})
    assert tts2.select_voice("el") == "v-el"


def test_a_retired_model_id_builds_its_successor():
    tts = CartesiaTTS(api_key="x", advanced={"model_id": "sonic-turbo"})
    assert tts._model_id == "sonic-3.6"
    assert CartesiaTTS(api_key="x", advanced={"model_id": "sonic-3.5"})._model_id == "sonic-3.5"
    assert CartesiaTTS(api_key="x")._model_id == "sonic-3.6"


def test_has_voice_for_names_a_real_voice_not_the_fallback():
    # The language lock asks this before it moves a call: a configured voice
    # or a shipped default counts; the English fallback select_voice takes
    # does not.
    tts = CartesiaTTS(api_key="x", voices={"en": "v-en", "el": "v-el"})
    assert tts.has_voice_for("el") and tts.has_voice_for("en")
    assert tts.has_voice_for("de") and tts.has_voice_for("it")
    assert not tts.has_voice_for("xx")


def test_a_pinned_voice_wins_for_every_language_and_unpins():
    # A phone route's own voice: the maps are not consulted while it is
    # pinned; has_voice_for still answers for the languages the line speaks.
    tts = CartesiaTTS(api_key="x", voices={"en": "v-en", "el": "v-el"})
    tts.pin_voice(" v-route ")
    assert tts.voice_id == "v-route"
    assert tts.select_voice("el") == "v-route" and tts.select_voice("de") == "v-route"
    assert tts.has_voice_for("el") and not tts.has_voice_for("xx")
    tts.pin_voice("")
    assert tts.select_voice("el") == "v-el"


def test_repr_redacts_api_key():
    tts = CartesiaTTS(api_key="super-secret-token", voice_id="v-en")
    r = repr(tts)
    assert "super-secret-token" not in r
    assert "v-en" in r


class _Ctx:
    """The SDK context's surface the provider drives."""

    def __init__(self):
        self.pushed = []
        self.sent = []
        self.ended = 0

    async def push(self, text):
        self.pushed.append(text)

    async def send(self, **kw):
        self.sent.append(kw)

    async def no_more_inputs(self):
        self.ended += 1

    async def receive(self):
        for event in getattr(self, "events", ()):
            yield event


async def test_flush_is_the_base_no_op():
    # Cartesia keeps the base no-op flush: it starts generating at closing
    # punctuation, and a vendor error on an empty-transcript flush would end
    # the context and silence the rest of the reply.
    tts = CartesiaTTS(api_key="x", voice_id="v-en")
    tts._ctx = _Ctx()
    await tts.send_text_chunk("Goodbye.")
    await tts.flush()
    assert tts._ctx.sent == [] and tts._ctx.pushed == ["Goodbye."]
    await tts.send_text_chunk("Thanks.", is_last=True)
    assert tts._ctx.ended == 1


# ── Delivery settings ──────────────────────────────────────────────


class _Socket:
    """The websocket under the SDK's real connection: records each frame the
    connection sends; its listener's ``recv`` waits until the test closes."""

    def __init__(self):
        self.frames = []
        self.state = None

    async def send(self, data):
        self.frames.append(json.loads(data))

    async def recv(self, decode=False):
        await asyncio.Event().wait()

    async def close(self, code=1000, reason=""):
        pass


async def _frames(advanced, *, one_shot=False):
    """The frames a streamed (or one-shot) utterance sends for ``advanced``."""
    socket = _Socket()
    tts = CartesiaTTS(api_key="x", voice_id="v-en", advanced=advanced)
    tts._conn = AsyncTTSResourceConnection(socket)
    try:
        if one_shot:
            await asyncio.wait_for(tts.synthesize("Hello.", language="en"), 0.2)
        else:
            tts.start_streaming_context(language="en")
            await tts.send_text_chunk("Hello there.")
            await tts.send_text_chunk("Goodbye.", is_last=True)
    except TimeoutError:
        pass        # synthesize waits for audio the fake never sends
    finally:
        await tts._conn.close()
    return socket.frames


async def test_every_frame_carries_the_delivery_settings_cartesia_accepts():
    frames = await _frames({"speed": "1.1", "volume": 9, "emotion": " Calm "})
    # Two chunks and the end of input, each with the settings, the out of
    # range volume held to Cartesia's top and the text "1.1" as a number.
    assert len(frames) == 3 and frames[-1]["transcript"] == ""
    for frame in frames:
        assert frame["generation_config"] == {"speed": 1.1, "volume": 2.0, "emotion": "calm"}


async def test_a_row_without_delivery_settings_sends_none():
    frames = await _frames({"model_id": "sonic-3.6"})
    assert frames and all("generation_config" not in frame for frame in frames)


async def test_a_one_shot_clip_carries_the_settings_too():
    frames = await _frames({"emotion": "sad"}, one_shot=True)
    assert frames and all(frame["generation_config"] == {"emotion": "sad"} for frame in frames)


async def test_a_value_cartesia_cannot_take_is_dropped_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="audio.providers.tts.cartesia"):
        frames = await _frames({"speed": "fast", "volume": float("nan"), "emotion": "robotic"})
    assert all("generation_config" not in frame for frame in frames)
    assert "robotic" in caplog.text and "fast" in caplog.text


async def test_a_refusal_from_cartesia_is_logged(caplog):
    tts = CartesiaTTS(api_key="x", voice_id="v-en")
    tts._ctx = _Ctx()
    tts._ctx.events = [SimpleNamespace(
        type="error", status_code=400, title="Invalid request",
        error_code=None, message="speed out of range",
    )]
    with caplog.at_level(logging.WARNING, logger="audio.providers.tts.cartesia"):
        assert [chunk async for chunk in tts.receive_audio()] == []
    assert "Invalid request" in caplog.text and "speed out of range" in caplog.text


def test_validate_advanced_names_each_bad_field():
    ok = {"model_id": "sonic-3.6", "speed": 0.6, "volume": "2", "emotion": "Happy"}
    assert CartesiaTTS.validate_advanced(ok) == {}
    errors = CartesiaTTS.validate_advanced({
        "model_id": "eleven_v3", "speed": 1.6, "volume": "loud", "emotion": "robotic",
    })
    assert set(errors) == {"model_id", "speed", "volume", "emotion"}
    assert errors["speed"] == "must be between 0.6 and 1.5"
    assert errors["volume"] == "must be a number"
    assert CartesiaTTS.default_advanced_settings() == {}


def test_the_emotions_the_tool_offers_are_ones_cartesia_accepts():
    # The text-to-speech tool offers Cartesia's six primary emotions.
    for word in ("neutral", "calm", "content", "sad", "angry", "scared"):
        assert CartesiaTTS.validate_advanced({"emotion": word}) == {}


def test_the_sdk_context_takes_the_delivery_settings():
    # A pin bump that renames the keyword fails here, not on a live call.
    assert "generation_config" in inspect.signature(AsyncTTSResourceConnection.context).parameters


# ── Voice catalog ──────────────────────────────────────────────────


def _voice(vid, *, owner=False, language="en", gender="feminine", description=""):
    """A voice as the SDK's ``Voice`` model carries it."""
    return SimpleNamespace(
        id=vid, name=vid.title(), description=description, language=language,
        gender=gender, is_owner=owner, is_public=not owner, country=None,
        created_at=None, preview_file_url=None,
    )


class _Library:
    """``AsyncCartesia`` as the catalog drives it. ``voices.list`` is a plain
    method returning an async iterator, as the SDK's is; it answers like the
    API (own voices for ``is_owner``, every voice otherwise, filtered by
    ``q``, ``gender`` and the language query) in pages of ``limit``."""

    def __init__(self, voices, *, honours_language=True):
        self.all = voices
        self.honours_language = honours_language
        self.calls = []
        self.pages = 0
        self.closed = False
        self.voices = self

    def list(self, **kwargs):
        self.calls.append(kwargs)
        hits = [v for v in self.all if v.is_owner or not kwargs.get("is_owner")]
        if kwargs.get("q"):
            hits = [v for v in hits if kwargs["q"].lower() in f"{v.name} {v.description}".lower()]
        if kwargs.get("gender"):
            hits = [v for v in hits if v.gender == kwargs["gender"]]
        language = (kwargs.get("extra_query") or {}).get("language")
        if language and self.honours_language:
            base = cartesia_mod._base_language(language)
            hits = [v for v in hits if v.language == base]
        limit = kwargs.get("limit") or 10

        async def walk():
            for start in range(0, len(hits), limit):
                self.pages += 1
                for v in hits[start:start + limit]:
                    yield v
        return walk()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


def _catalog(monkeypatch, voices, **kw):
    library = _Library(voices, **kw)
    monkeypatch.setattr(cartesia_mod, "AsyncCartesia", lambda **_: library)
    return CartesiaTTS(api_key="x"), library


async def test_the_catalog_lists_own_voices_first_then_public_ones_to_the_cap(monkeypatch):
    own = [_voice(f"own-{i}", owner=True) for i in range(3)]
    public = [_voice(f"pub-{i}") for i in range(80)]
    # The public walk answers own voices too: each is listed once.
    tts, library = _catalog(monkeypatch, public[:10] + own + public[10:])
    voices = await tts.list_voices()
    assert [v.id for v in voices[:3]] == ["own-0", "own-1", "own-2"]
    assert {v.category for v in voices[:3]} == {"own"}
    assert {v.category for v in voices[3:]} == {"public"}
    assert len(voices) == cartesia_mod._CATALOG_SIZE
    assert len({v.id for v in voices}) == len(voices)
    assert library.closed


async def test_an_account_with_more_own_voices_than_the_cap_lists_only_its_own(monkeypatch):
    tts, library = _catalog(monkeypatch, [_voice(f"own-{i}", owner=True) for i in range(60)] + [_voice("pub")])
    voices = await tts.list_voices()
    assert len(voices) == cartesia_mod._CATALOG_SIZE
    assert {v.category for v in voices} == {"own"}
    assert len(library.calls) == 1


async def test_an_account_with_no_voice_of_its_own_lists_public_ones(monkeypatch):
    tts, _ = _catalog(monkeypatch, [_voice("pub-1"), _voice("pub-2")])
    assert [v.id for v in await tts.list_voices()] == ["pub-1", "pub-2"]


async def test_a_catalog_voice_reads_in_plain_words(monkeypatch):
    tts, _ = _catalog(monkeypatch, [
        _voice("v1", gender="masculine", description="Warm narrator. " * 30),
        _voice("v2", gender="gender_neutral", language=None),
        _voice("v3", gender=None, description="Calm"),
    ])
    v1, v2, v3 = await tts.list_voices()
    assert v1.description.startswith("male Warm narrator.")
    assert len(v1.description) == len("male ") + 200
    assert v1.languages == ["en"] and v1.owner_id == "" and v1.preview_url == ""
    assert v2.description == "neutral" and v2.languages == []
    assert v3.description == "Calm"


async def test_the_search_maps_the_shared_words_onto_cartesias(monkeypatch):
    tts, library = _catalog(monkeypatch, [
        _voice("warm-m", gender="masculine", description="warm narrator"),
        _voice("warm-f", gender="feminine", description="warm narrator"),
        _voice("warm-el", gender="masculine", language="el", description="warm narrator"),
        _voice("cold-m", gender="masculine", description="cold narrator"),
    ])
    found = await tts.search_voice_library(
        search="  warm ", language="en-GB", gender="male", age="old", category="famous",
    )
    assert [v.id for v in found] == ["warm-m"]
    assert found[0].owner_id == "" and found[0].description == "male warm narrator"
    # The whole tag reaches the API: a locale narrows to its accent there.
    assert library.calls[-1]["extra_query"] == {"language": "en-GB"}
    assert library.closed


async def test_a_blank_search_sends_no_text_and_no_language(monkeypatch):
    tts, library = _catalog(monkeypatch, [_voice("a"), _voice("b", language="el")])
    found = await tts.search_voice_library(search="   ")
    assert [v.id for v in found] == ["a", "b"]
    assert "q" not in library.calls[-1] and "extra_query" not in library.calls[-1]


async def test_the_language_holds_when_the_api_ignores_it(monkeypatch):
    tts, _ = _catalog(monkeypatch, [
        _voice("en-1"), _voice("el-1", language="el"), _voice("none", language=None),
        _voice("el-2", language="el-GR"),
    ], honours_language=False)
    found = await tts.search_voice_library(language="el")
    # A voice with no language is kept: nothing says it cannot speak Greek.
    assert [v.id for v in found] == ["el-1", "none", "el-2"]


async def test_the_search_pages_over_the_matches_and_stops_once_full(monkeypatch):
    voices = [_voice(f"en-{i}") if i % 2 else _voice(f"el-{i}", language="el") for i in range(400)]
    tts, library = _catalog(monkeypatch, voices, honours_language=False)
    second = await tts.search_voice_library(language="en", page=1, page_size=10)
    # Page 1 counts English matches 10 to 19, not raw voices.
    assert [v.id for v in second] == [f"en-{i}" for i in range(21, 41, 2)]
    assert library.pages == 1
    assert await tts.search_voice_library(page=9, page_size=100) == []


async def test_a_search_walks_a_bounded_part_of_the_library(monkeypatch):
    voices = [_voice(f"el-{i}", language="el") for i in range(3000)]
    tts, library = _catalog(monkeypatch, voices, honours_language=False)
    assert await tts.search_voice_library(language="en") == []
    assert library.pages == cartesia_mod._SEARCH_WALK // cartesia_mod._PAGE_LIMIT


class _EndlessLibrary(_Library):
    """A library whose page cursor is ignored: every page is the same voice."""

    def list(self, **kwargs):
        self.calls.append(kwargs)
        voice = self.all[0]

        async def walk():
            while True:
                self.pages += 1
                for _ in range(kwargs.get("limit") or 10):
                    yield voice
        return walk()


async def test_the_catalog_ends_on_a_library_that_repeats_its_page(monkeypatch):
    library = _EndlessLibrary([_voice("same")])
    monkeypatch.setattr(cartesia_mod, "AsyncCartesia", lambda **_: library)
    voices = await CartesiaTTS(api_key="x").list_voices()
    assert [v.id for v in voices] == ["same"]
    assert library.pages == cartesia_mod._SEARCH_WALK // cartesia_mod._PAGE_LIMIT


async def test_an_unknown_gender_is_refused_by_name(monkeypatch):
    tts, _ = _catalog(monkeypatch, [_voice("a")])
    with pytest.raises(UnsupportedProviderOperation, match="male, female or neutral"):
        await tts.search_voice_library(gender="robot")


async def test_cartesia_voices_need_no_adding():
    with pytest.raises(UnsupportedProviderOperation, match="need no adding"):
        await CartesiaTTS(api_key="x").add_library_voice("owner", "voice")


async def test_every_keyword_the_catalog_sends_exists_in_the_sdk(monkeypatch):
    # A pin bump that renames a keyword fails here, not on a live call.
    tts, library = _catalog(monkeypatch, [_voice("a", owner=True), _voice("b")])
    await tts.list_voices()
    await tts.search_voice_library(search="x", language="en", gender="female", page_size=5)
    accepted = set(inspect.signature(AsyncCartesia(api_key="x").voices.list).parameters)
    sent = {key for call in library.calls for key in call}
    assert {"is_owner", "limit", "q", "gender", "extra_query"} <= sent <= accepted
