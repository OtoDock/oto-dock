"""A Codex session's text and settings are its own (lane SESSION-STARTS).

A Codex home serves every session of its scope; a file every start rewrote
(``AGENTS.md``, the tool sections of ``config.toml``) gave a running or
starting session its neighbour's text and tools. Each session's own part now
rides a channel no other session writes: the terminal's profile, the
app-server thread's ``developerInstructions`` and ``config`` map.
"""

import json

import pytest
import tomllib

from core.layers.codex import session_text
from core.layers.codex.layer import CodexCLIExecutionLayer as _Layer
from core.sandbox.session_config_dir import write_if_changed as _W


def _start(home, session_id, text, token, *, interactive):
    """One session's start in ``home`` as ``_prepare`` writes it."""
    mcp = (f'[mcp_servers.echo]\ncommand = "python3"\n'
           f'env = {{ "PROXY_API_KEY" = "{token}" }}\n')
    return _Layer._write_config_toml(
        home, text, mcp, interactive=interactive, trusted_cwd="/users/a" if interactive else "",
        sandbox_codex_dir="/users/a/.codex", session_id=session_id,
        client_type="dashboard",
    )


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
@pytest.mark.parametrize("interactive", [False, True])
def test_two_sessions_of_one_home_keep_their_own_text_and_tools(tmp_path, order, interactive):
    """The security rule: either start order, either kind."""
    texts = {"A": "INSTR-A chat", "B": "INSTR-B judge"}
    started = {}
    for name in order:
        started[name] = _start(tmp_path, f"sid-{name}", texts[name], f"tok-{name}",
                               interactive=interactive)
    shared = (tmp_path / "config.toml").read_text()
    # The home's file holds no session's text and no session's tools.
    assert "INSTR-" not in shared and "mcp_servers" not in shared and "tok-" not in shared
    assert not (tmp_path / "AGENTS.md").exists()
    for name in ("A", "B"):
        text, config = started[name]
        assert text.startswith(texts[name])
        assert config["mcp_servers"]["echo"]["env"]["PROXY_API_KEY"] == f"tok-{name}"
        own = session_text.read_session(tmp_path, f"sid-{name}")
        assert own == {"developer_instructions": text, "config": {}}
        if interactive:
            profile = tomllib.loads((tmp_path / f"sid-{name}.config.toml").read_text())
            assert profile["developer_instructions"] == text
            assert profile["mcp_servers"]["echo"]["env"]["PROXY_API_KEY"] == f"tok-{name}"


def test_the_session_file_carries_the_map_only_for_a_compute_machine(tmp_path):
    """Only a compute machine reads the thread's map from the session's
    file. Its tokens stay out of the file otherwise."""
    for for_compute in (False, True):
        mcp = '[mcp_servers.echo]\ncommand = "python3"\nenv = { "PROXY_API_KEY" = "tok-A" }\n'
        _, config = _Layer._write_config_toml(
            tmp_path, "INSTR-A", mcp, sandbox_codex_dir="/users/a/.codex", session_id="sid-A",
            for_compute=for_compute)
        own = session_text.read_session(tmp_path, "sid-A")
        assert own["config"] == (config if for_compute else {})
        assert ("tok-A" in (tmp_path / "sid-A.session.json").read_text()) is for_compute


def test_a_start_removes_the_homes_shared_text(tmp_path):
    """A released home's AGENTS.md (and an override) would be read by every
    session of the home: the first start on the new writer removes them."""
    (tmp_path / "AGENTS.md").write_text("an old session's text")
    (tmp_path / "AGENTS.override.md").write_text("planted")
    _start(tmp_path, "sid-A", "INSTR-A", "tok", interactive=False)
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "AGENTS.override.md").exists()
    # An empty file stays: it is the sandbox mask's mountpoint, and Codex
    # skips it.
    (tmp_path / "AGENTS.md").write_text("")
    _start(tmp_path, "sid-B", "INSTR-B", "tok", interactive=False)
    assert (tmp_path / "AGENTS.md").read_text() == ""


@pytest.mark.asyncio
async def test_the_terminal_argv_carries_the_profile(monkeypatch):
    from types import SimpleNamespace

    from core.layers.codex import spawn
    config = SimpleNamespace(model="m", permission_mode="default", resume_handle="", resume=False,
                             interactive_first_prompt="", sandbox_host_claude_dir="", chat_id="")
    ready = SimpleNamespace(sandbox_mode="workspace-write", codex_effort="", local_endpoint="")
    argv, _ = await spawn._CodexSpawn._tui_argv(object(), "sid-A", config, ready, "/users/a")
    assert argv[argv.index("-p") + 1] == "sid-A"


def test_the_profile_escapes_every_control_character(tmp_path):
    text = "line\nquote\" back\\ tab\t del\x7f nul\x00 end"
    session_text.write_session_files(tmp_path, "s-1", text=text, config={}, profile="", writer=_W)
    assert tomllib.loads((tmp_path / "s-1.config.toml").read_text())["developer_instructions"] == text


def test_names_are_never_paths():
    for bad in ("../x", "a/b", "", "a b", "x" * 129):
        with pytest.raises(ValueError):
            session_text.checked_name(bad)
    assert session_text.checked_name("0e9c-AB_1") == "0e9c-AB_1"


def test_the_files_of_a_session(tmp_path):
    assert session_text.is_session_file("s-1.config.toml")
    assert session_text.is_session_file("s-1.session.json")
    assert session_text.is_session_file("t-1.text-sha256")
    assert not session_text.is_session_file("config.toml")
    assert session_text.is_own_file("s-1.session.json", "s-1")
    assert not session_text.is_own_file("s-2.session.json", "s-1")
    assert not session_text.is_own_file("s-1.text-sha256", "s-1")
    session_text.write_session_files(tmp_path, "s-1", text="T", config={}, profile="", writer=_W)
    session_text.forget("s-1")
    assert not list(tmp_path.glob("s-1.*"))
    session_text.forget("never-written")  # nothing to do


def test_the_hook_script_uses_the_modules_names():
    """The hook runs alone in the sandbox with its own copy of the names."""
    import importlib.util
    from pathlib import Path

    import config as app_config
    spec = importlib.util.spec_from_file_location(
        "session_text_hook", Path(app_config.HOOKS_DIR) / "session_text.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert hook.SESSION_SUFFIX == session_text.SESSION_SUFFIX
    assert hook.RECORD_SUFFIX == session_text.RECORD_SUFFIX
    assert hook._NAME_RE.pattern == session_text._NAME_RE.pattern


def test_hooks_json_runs_the_session_text_hook_untrimmed():
    from core.layers.codex.config_dir import build_hooks
    entry = build_hooks("/users/a/.codex")["hooks"]["SessionStart"][0]["hooks"][0]
    assert entry["command"].endswith('/users/a/.codex/session_text.py"')
    assert entry["additionalContextLimit"] == 0


def test_the_session_json_is_owner_only(tmp_path):
    session_text.write_session_files(tmp_path, "s-1", text="T", config={"a": 1}, writer=_W)
    assert (tmp_path / "s-1.session.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "s-1.session.json").read_text())["config"] == {"a": 1}


@pytest.mark.parametrize("interactive", [False, True])
def test_a_link_planted_in_the_home_is_never_followed(tmp_path, interactive):
    """The home is writable from inside the session, which knows its own id
    (a resumed chat reuses it): a link it plants at any name the start writes
    (the session's files, a temp beside them, the catalog) must leave the
    file it points at untouched."""
    import os

    from core.layers.codex.local_model_catalog import CATALOG_FILE_NAME
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("UNTOUCHED")
    names = [f"sid-A{session_text.SESSION_SUFFIX}", f"sid-A{session_text.PROFILE_SUFFIX}",
             CATALOG_FILE_NAME]
    for name in list(names):
        names.append(f".{name}.{os.getpid()}.tmp")
    for name in names:
        (home / name).symlink_to(outside)
    _Layer._write_config_toml(
        home, "INSTR-A", "", interactive=interactive, sandbox_codex_dir="/users/a/.codex",
        session_id="sid-A", local_endpoint="http://127.0.0.1:11434/v1", local_catalog_json='{"models": []}',
    )
    assert outside.read_text() == "UNTOUCHED"
    written = [f"sid-A{session_text.SESSION_SUFFIX}", CATALOG_FILE_NAME]
    if interactive:
        written.append(f"sid-A{session_text.PROFILE_SUFFIX}")
    for name in written:
        assert not (home / name).is_symlink() and (home / name).is_file(), name


def test_a_home_that_is_a_link_is_never_read_or_emptied_through(tmp_path):
    """The removals and the read reach their names from the home's own
    descriptor: a link at the home's name leads them nowhere."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    session_text.write_session_files(target, "sid-L", text="KEPT", config={}, writer=_W, profile="")
    (target / "AGENTS.md").write_text("kept")
    link = tmp_path / "home"
    link.symlink_to(target)

    assert session_text.read_session(link, "sid-L") is None
    session_text.remove_shared_text(link)
    session_text.drop(link, "sid-L")

    assert (target / "AGENTS.md").read_text() == "kept"
    assert (target / f"sid-L{session_text.SESSION_SUFFIX}").is_file()
    assert (target / f"sid-L{session_text.PROFILE_SUFFIX}").is_file()
    # The home itself, as before.
    assert session_text.read_session(target, "sid-L")["developer_instructions"] == "KEPT"
    session_text.remove_shared_text(target)
    session_text.drop(target, "sid-L")
    assert not (target / "AGENTS.md").exists()
    assert not (target / f"sid-L{session_text.SESSION_SUFFIX}").exists()
