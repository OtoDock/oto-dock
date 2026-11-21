"""Operator-forced platform settings.

`config.forced_settings()` pins platform_settings keys on managed/cloud installs.
The enforcement lives in `storage.database`: forced values win on read, writes to
forced keys are no-ops (the operator owns them), and the underlying DB row is left
untouched so removing the force reveals the original value again.
"""

import json

import pytest

import config
from configuration import setting_switches
from storage import database as db


def test_read_overlay_wins(temp_db, monkeypatch):
    db.set_platform_setting("session_timeout", "3600")
    assert db.get_platform_setting("session_timeout") == "3600"

    monkeypatch.setattr(config, "forced_settings", lambda: {"session_timeout": "9999"})
    assert db.get_platform_setting("session_timeout") == "9999"


def test_write_to_forced_key_is_noop_and_preserves_db(temp_db, monkeypatch):
    db.set_platform_setting("session_retention_days", "180")
    monkeypatch.setattr(config, "forced_settings", lambda: {"session_retention_days": "30"})

    db.set_platform_setting("session_retention_days", "7")  # ignored — forced
    assert db.get_platform_setting("session_retention_days") == "30"

    # Underlying DB row is untouched: dropping the force reveals the original.
    monkeypatch.setattr(config, "forced_settings", lambda: {})
    assert db.get_platform_setting("session_retention_days") == "180"


def test_get_all_overlays_forced(temp_db, monkeypatch):
    db.set_platform_setting("smtp_host", "db.example.com")
    monkeypatch.setattr(config, "forced_settings", lambda: {"smtp_host": "forced.example.com", "extra": "v"})
    alls = db.get_all_platform_settings()
    assert alls["smtp_host"] == "forced.example.com"
    assert alls["extra"] == "v"


def test_parse_is_failsafe():
    assert config._parse_forced_settings("") == {}
    assert config._parse_forced_settings("   ") == {}
    assert config._parse_forced_settings("{not json") == {}
    assert config._parse_forced_settings("[1, 2]") == {}          # non-object
    assert config._parse_forced_settings('"a string"') == {}      # non-object
    assert config._parse_forced_settings('{"a": true, "b": false, "c": 180}') == {
        "a": "1", "b": "", "c": "180",
    }


# --- a forced JSON true or false on a switch -----------------------------------
# Every switch stores the words its own writer writes
# (configuration/setting_switches.py); a forced bool is stored in them, so each
# reader takes it as it takes the dashboard's own write.


def _force(monkeypatch, raw: str) -> dict[str, str]:
    forced = config._parse_forced_settings(raw)
    monkeypatch.setattr(config, "forced_settings", lambda: forced)
    return forced


def _admin_settings() -> dict:
    from fastapi.testclient import TestClient

    from app import app
    from auth.providers import create_session_jwt
    cookie = {"Cookie": f"session={create_session_jwt('user-admin', 'admin@t', 'Admin', 'admin')}"}
    r = TestClient(app).get("/v1/admin/platform-settings", headers=cookie)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("key", sorted(setting_switches.SWITCH_WORDS))
def test_a_json_bool_is_stored_in_the_switchs_own_words(key):
    on, off = setting_switches.SWITCH_WORDS[key]
    assert config._parse_forced_settings(f'{{"{key}": true}}') == {key: on}
    assert config._parse_forced_settings(f'{{"{key}": false}}') == {key: off}


def test_a_json_bool_on_another_key_keeps_its_old_words(capsys):
    assert config._parse_forced_settings('{"jwt_expiry_hours": false, "x": true}') == {
        "jwt_expiry_hours": "", "x": "1"}
    assert "jwt_expiry_hours is not an on/off setting" in capsys.readouterr().err


def test_a_quoted_value_is_kept_as_written():
    assert config._parse_forced_settings(
        '{"session_retention_enabled": "0", "store_review_enabled": "", "session_retention_days": "180"}'
    ) == {"session_retention_enabled": "0", "store_review_enabled": "", "session_retention_days": "180"}


def test_every_switch_of_the_admin_settings_follows_a_forced_bool(temp_db, monkeypatch):
    """Each boolean of the admin settings read that is a switch reads what
    was forced; a boolean the table does not name reads no platform setting
    of its own name (a switch added there needs its entry)."""
    fields = {k for k, v in _admin_settings().items() if isinstance(v, bool)}
    switches = fields & set(setting_switches.SWITCH_WORDS)
    assert len(switches) >= 16
    for value in (True, False):
        _force(monkeypatch, json.dumps(dict.fromkeys(switches, value)))
        got = _admin_settings()
        assert {k: got[k] for k in switches} == dict.fromkeys(switches, value)
    for word in ("0", "1", "false", "true"):
        monkeypatch.setattr(config, "forced_settings", lambda: {})
        before = _admin_settings()
        _force(monkeypatch, json.dumps(dict.fromkeys(fields - switches, word)))
        after = _admin_settings()
        assert {k: after[k] for k in fields - switches} == {k: before[k] for k in fields - switches}


def test_the_other_readers_follow_a_forced_bool(temp_db, monkeypatch):
    from services import title_generator
    from services.billing import relay_client
    from services.infra import api_key_manager, external_retention
    from services.media import audio_service, wake_keywords
    from services.notifications import smtp
    for value in (True, False):
        _force(monkeypatch, json.dumps({
            "otodock_api_relay_enabled": value, "api_keys_off": value, "external_retention_enabled": value,
            "title_generation_enabled": value, "audio_chat_enabled": value, "smtp_tls": value,
            "smtp_host": "mail.example.com", "audio_wake_word_enabled": value,
            "audio_chat_duplex_enabled": value, "audio_language_identify_enabled": value,
        }))
        monkeypatch.setattr(api_key_manager, "_keys_off_cache", (False, 0.0))
        assert relay_client.api_relay_enabled() is value
        assert api_key_manager.keys_switched_off() is value
        assert external_retention.settings_enabled() is value
        assert title_generator.title_generation_status()["enabled"] is value
        assert audio_service.chat_audio_enabled() is value
        assert smtp.load_smtp_config().use_tls is value
        with monkeypatch.context() as m:
            m.setattr("audio.providers.lid.ecapa.identifier_available", lambda: True)
            assert audio_service.language_identify_enabled() is value
        if not value:
            assert wake_keywords.build_for_user(None)["enabled"] is False
    duplex_off = "full-duplex mode is not enabled by the administrator"
    _force(monkeypatch, '{"audio_chat_enabled": true, "audio_chat_duplex_enabled": false}')
    assert audio_service.duplex_capability()["reason"] == duplex_off
    _force(monkeypatch, '{"audio_chat_enabled": true, "audio_chat_duplex_enabled": true}')
    assert audio_service.duplex_capability().get("reason") != duplex_off


def test_the_table_holds_the_writers_words():
    from api.audio.audio import _POLICY_KEYS
    from services.phone import phone_config_seed, phone_settings
    words = setting_switches.SWITCH_WORDS
    for key, rule in phone_settings.PHONE_SETTINGS.items():
        if rule is phone_settings._yes_no:
            assert words[f"phone_{key}"] == ("true", "false"), key
        elif rule is phone_settings._on_off:
            assert words[f"phone_{key}"] == ("on", "off"), key
    for key, value in phone_config_seed._DEFAULT_SETTINGS.items():
        if value in ("true", "false"):
            assert words[key] == ("true", "false"), key
        elif value in ("on", "off"):
            assert words[key] == ("on", "off"), key
    for field in ("chat_enabled", "show_experimental"):
        assert words[_POLICY_KEYS[field]] == ("true", "false")


def _switch_writers():
    """Every ``set_platform_setting`` call in the proxy whose value is a
    ternary of two literal words (a switch's writer, direct or through
    ``asyncio.to_thread``), with its key resolved: a literal, the literal
    tuple a ``for`` loop walks, or the expression read in its imported
    module. Yields (where, key, words); a key it cannot resolve yields None."""
    import ast
    import importlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] in ("tests", "venv") or "node_modules" in rel.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        loops = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name) \
                    and isinstance(node.iter, (ast.Tuple, ast.List)):
                for inner in ast.walk(node):
                    loops[id(inner)] = (node.target.id, [e.value for e in node.iter.elts
                                                         if isinstance(e, ast.Constant)])
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            args = list(node.args)
            if name == "to_thread" and args and isinstance(args[0], ast.Attribute) \
                    and args[0].attr == "set_platform_setting":
                args = args[1:]
            elif name != "set_platform_setting":
                continue
            if len(args) < 2 or not isinstance(args[1], ast.IfExp):
                continue
            body, orelse = args[1].body, args[1].orelse
            if not (isinstance(body, ast.Constant) and isinstance(orelse, ast.Constant)
                    and isinstance(body.value, str) and isinstance(orelse.value, str)):
                continue
            where, words, key = f"{rel}:{node.lineno}", {body.value, orelse.value}, args[0]
            if isinstance(key, ast.Constant):
                yield where, key.value, words
            elif isinstance(key, ast.Name) and loops.get(id(node), ("",))[0] == key.id:
                for value in loops[id(node)][1]:
                    yield where, value, words
            else:
                module = importlib.import_module(".".join(rel.with_suffix("").parts))
                names = {**vars(module), **_imported_names(tree)}
                try:
                    value = eval(compile(ast.Expression(key), where, "eval"), names)
                except (NameError, AttributeError, KeyError):
                    value = None
                yield where, value, words


def _imported_names(tree) -> dict:
    """The names a file's own imports bind, a function's included."""
    import ast
    import importlib
    out: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                try:
                    source = importlib.import_module(node.module)
                    out[alias.asname or alias.name] = getattr(source, alias.name, None) \
                        or importlib.import_module(f"{node.module}.{alias.name}")
                except ImportError:
                    continue
    return out


def test_every_switch_writer_has_its_row_in_the_table():
    """A new on/off setting written by a route of its own fails here until
    configuration/setting_switches.py names it with its writer's words."""
    seen = 0
    for where, key, words in _switch_writers():
        assert key is not None, f"{where}: the switch's key could not be read"
        assert key in setting_switches.SWITCH_WORDS, f"{where}: {key} has no row in SWITCH_WORDS"
        assert set(setting_switches.SWITCH_WORDS[key]) == words, f"{where}: {key} writes {sorted(words)}"
        seen += 1
    assert seen >= 19
