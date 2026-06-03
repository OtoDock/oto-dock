"""Language registry + automatic TTS-language detector tests (pure)."""

from audio.streaming.lang import SUPPORTED_LANGUAGES, base_lang, detect_tts_language


def test_supported_languages_set():
    assert [c for c, _ in SUPPORTED_LANGUAGES] == [
        "en-US", "en-GB", "el-GR", "de-DE", "es-ES", "fr-FR", "it-IT",
    ]


def test_base_lang():
    assert base_lang("en-US") == "en"
    assert base_lang("el-GR") == "el"
    assert base_lang("de") == "de"
    assert base_lang("") == ""


def test_detect_greek_by_script():
    assert detect_tts_language("Καλημέρα, τι κάνεις σήμερα;") == "el"


def test_detect_latin_languages():
    assert detect_tts_language("The cat is on the table and you are here.") == "en"
    assert detect_tts_language("Ich bin nicht sicher, ob das richtig ist und sie kommt.") == "de"
    assert detect_tts_language("No sé qué es esto, pero está muy bien para los niños.") == "es"
    assert detect_tts_language("Je ne sais pas, mais c'est une très bonne idée pour vous.") == "fr"
    assert detect_tts_language("Non so che cosa sia questo, ma è una cosa per gli altri.") == "it"


def test_detect_default_when_no_signal():
    assert detect_tts_language("") == "en"
    assert detect_tts_language("xyz 123 qqq") == "en"  # no stopword hits → default
    assert detect_tts_language("zzz", default="de") == "de"
