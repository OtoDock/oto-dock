"""The ENGINES table (satellite/engines.py): the one place this satellite
names the engines it can run. The values the proxy and the installed
satellite.conf read are frozen wire / file vocabulary and are pinned here;
the proxy's own contract test reads the same module and checks its
descriptors agree.

Run: python -m pytest satellite/tests/test_engines.py -v
"""

import pytest

from satellite import engines
from satellite.engines import BY_BINARY, BY_CREDENTIAL_KIND, BY_PIN_KEY, ENGINES, engine_for


def test_the_wire_ids_are_the_two_engines_every_released_satellite_ran():
    assert set(ENGINES) == {"claude-code-cli", "codex-cli"}
    for path, row in ENGINES.items():
        assert row.execution_path == path


def test_the_frozen_rows():
    claude = ENGINES["claude-code-cli"]
    assert (claude.binary, claude.installed_name, claude.pin_key) == ("claude", "claude-code", "claude_code")
    assert (claude.config_dir, claude.credential_kind, claude.credential_filename) == (
        ".claude", "claude", ".credentials.json")
    assert (claude.conf_section, claude.conf_key) == ("cli", "claude_bin")
    assert claude.turn_replay is True and claude.revives_dead_process is False
    codex = ENGINES["codex-cli"]
    assert (codex.binary, codex.installed_name, codex.pin_key) == ("codex", "codex", "codex")
    assert (codex.config_dir, codex.credential_kind, codex.credential_filename) == (
        ".codex", "codex", "auth.json")
    assert (codex.conf_section, codex.conf_key) == ("codex", "codex_bin")
    assert codex.turn_replay is False and codex.revives_dead_process is True


def test_the_lookup_tables_cover_exactly_the_rows():
    assert set(BY_CREDENTIAL_KIND) == {"claude", "codex"}     # credentials_update.kind
    assert set(BY_PIN_KEY) == {"claude_code", "codex"}        # cli_pins keys
    assert set(BY_BINARY) == {"claude", "codex"}              # cli_status keys / spawn binaries
    for row in ENGINES.values():
        assert BY_CREDENTIAL_KIND[row.credential_kind] is row
        assert BY_PIN_KEY[row.pin_key] is row
        assert BY_BINARY[row.binary] is row


def test_every_session_class_resolves_and_names_its_row():
    # The dotted paths resolve, and each class declares the wire id of the
    # row that names it (the class attribute the session manager echoes on
    # every frame).
    for path, row in ENGINES.items():
        headless = row.headless_cls()
        pty = row.pty_cls()
        assert headless.execution_path == path, (path, headless)
        assert pty.execution_path == path, (path, pty)
        assert hasattr(headless, "run_turn") and hasattr(headless, "is_alive"), path


def test_an_unknown_id_fails_closed():
    with pytest.raises(ValueError, match="acme-cli"):
        engine_for("acme-cli")
    with pytest.raises(ValueError):
        engine_for("")


def test_the_table_is_a_leaf():
    # config.py, cli_versions.py and ws_client.py read the table; the session
    # classes import config — so the table imports nothing of the package.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(engines))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"importlib", "dataclasses", "functools", "__future__"}, imported
