"""The yes/no and numeric rule of ``config.py`` (``_parse_bool``, ``_parse_int``,
``_parse_float``) and the unknown-key warning (``unknown_config_keys``).

One rule for every key, where ``OIDC_ENABLED`` written as 1 and
``OTODOCK_AIR_GAPPED`` written as yes used to be read as off. A wrong word
stops the boot naming the key instead of falling back silently.
"""

from __future__ import annotations

import pytest

import config


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", " Yes ", "On"])
def test_every_yes_word_is_on(raw):
    assert config._parse_bool("K", raw, False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", " nO "])
def test_every_no_word_is_off(raw):
    assert config._parse_bool("K", raw, True) is False


@pytest.mark.parametrize("default", [True, False, None])
def test_an_empty_value_is_the_default(default):
    assert config._parse_bool("K", "", default) is default
    assert config._parse_bool("K", "   ", default) is default


def test_auto_keeps_a_tri_state_default_and_stops_a_plain_key():
    assert config._parse_bool("COOKIE_SECURE", "auto", None) is None
    with pytest.raises(SystemExit) as exc:
        config._parse_bool("OIDC_ENABLED", "auto", False)
    assert "OIDC_ENABLED='auto'" in str(exc.value)


@pytest.mark.parametrize("raw, want", [
    ("false     # a comment systemd keeps", False),   # EnvironmentFile keeps the inline comment
    ("true# c", True),                                 # a quoted value: the quotes go, the comment is glued on
    ("  on  #", True),
    ("# only a comment", None),
])
def test_an_inline_comment_is_not_part_of_a_yes_no_value(raw, want):
    assert config._parse_bool("K", raw, None) is want


def test_an_inline_comment_is_not_part_of_a_number():
    assert config._parse_int("K", "12   # twelve", 7) == 12
    assert config._parse_int("K", "12# twelve", 7) == 12
    assert config._parse_float("K", "2.5 # and a half", 0.25) == 2.5
    assert config._parse_int("K", "# nothing", 7) == 7


@pytest.mark.parametrize("raw", ["yse", "enabled", "2", "t"])
def test_another_word_stops_the_boot_naming_the_key(raw):
    with pytest.raises(SystemExit) as exc:
        config._parse_bool("OTODOCK_AIR_GAPPED", raw, False)
    msg = str(exc.value)
    assert msg.startswith("FATAL: OTODOCK_AIR_GAPPED=")
    assert "yes/no" in msg and "1/0, yes/no, on/off" in msg


@pytest.mark.parametrize("raw, want", [("12", 12), (" 12 ", 12), ("08", 8), ("+5", 5), ("-3", -3), ("", 7)])
def test_whole_numbers(raw, want):
    assert config._parse_int("K", raw, 7) == want


def test_minimum_clamps_a_whole_number():
    assert config._parse_int("K", "0", 4, minimum=1) == 1
    assert config._parse_int("K", "9", 4, minimum=1) == 9
    assert config._parse_int("K", "", 0, minimum=1) == 1


@pytest.mark.parametrize("raw", ["abc", "1e3", "1.5", "0x10", "nan"])
def test_a_non_number_stops_the_boot_naming_the_key(raw):
    with pytest.raises(SystemExit) as exc:
        config._parse_int("DB_POOL_MAX_SIZE", raw, 30)
    assert str(exc.value) == f"FATAL: DB_POOL_MAX_SIZE={raw!r} is not a whole number."


@pytest.mark.parametrize("raw, want", [("2.5", 2.5), (" 1 ", 1.0), ("1e3", 1000.0), ("", 0.25)])
def test_decimals(raw, want):
    assert config._parse_float("K", raw, 0.25) == want


def test_a_float_minimum_clamps_and_nan_and_inf_are_refused():
    assert config._parse_float("K", "0.1", 60.0, minimum=1.0) == 1.0
    for raw in ("nan", "inf", "-inf", "abc"):
        with pytest.raises(SystemExit) as exc:
            config._parse_float("WS_WRITE_STALL_S", raw, 60.0)
        assert str(exc.value) == f"FATAL: WS_WRITE_STALL_S={raw!r} is not a number."


def test_unknown_keys_are_the_ones_nothing_reads():
    present = ["OTODOCK_AIR_GAPED", "PROXY_PORT", "COMPOSE_FILE", "RATE_LIMIT_LOGIN_MAX",
               "OTODOCK_MCPS_CATALOG_RAW_BASE", "POSTGRES_PASSWORD", "MY_OWN_THING"]
    known = {"PROXY_PORT"} | config.FOREIGN_CONFIG_KEYS
    assert config.unknown_config_keys(present, known) == ["MY_OWN_THING", "OTODOCK_AIR_GAPED"]


def test_the_module_level_switches_use_the_rule():
    # The sites the rule replaced are plain booleans now, never a string compare.
    for name in ("OTODOCK_AIR_GAPPED", "OTODOCK_CLOUD", "OIDC_ENABLED", "AUTH_PROVIDER_BYPASS",
                 "DASHBOARD_ENABLED", "OIDC_CONFIRM_FRESH_LOGIN", "OIDC_CONFIRM_REQUIRE_AUTH_TIME",
                 "MCP_AUTO_INSTALL_SYSTEM_DEPS", "OTODOCK_MCP_BUILD_LOCAL", "RELEASE_CHECK_ENABLED",
                 "ALLOW_NEWER_DATABASE", "RUNNING_IN_DOCKER", "COOKIE_SECURE", "LOCAL_PBX_ENABLED"):
        assert isinstance(getattr(config, name), bool), name
    for name in ("PORT", "TURN_SILENCE_S", "DB_POOL_MAX_SIZE", "MAX_FILE_MB"):
        assert isinstance(getattr(config, name), int), name
    for name in ("DB_POOL_TIMEOUT_S", "LOOP_WATCHDOG_THRESHOLD_S", "BUDGET_FRACTION"):
        assert isinstance(getattr(config, name), float), name
