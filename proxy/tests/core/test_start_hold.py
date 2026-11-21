"""The bundled tools and the start's hold at a boot (``startup._boot_mcps``,
``_end_start_hold``, ``_boot_continue``): a boot scans the tools and builds
none, a tool the scan found stale is left out of sessions and named to
admins once, and what the start held for the compute machine goes out in
the boot's order."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

import startup
from core import drain
from services.infra import platform_marks
from services.mcp import docker_manager, mcp_registry, mcp_venv_bootstrap, tool_envs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    drain.reset_for_tests()
    tool_envs.reset_for_tests()
    monkeypatch.setattr(startup, "_bg_tasks", [])
    monkeypatch.setattr(mcp_registry, "scan_manifests", lambda: None)
    monkeypatch.setattr(docker_manager, "startup_docker_mcps", lambda: None)
    marks: list[str] = []
    monkeypatch.setattr(platform_marks, "note_held", lambda: marks.append("held"))
    monkeypatch.setattr(platform_marks, "note_resumed", lambda: marks.append("resumed"))
    yield marks
    drain.reset_for_tests()
    tool_envs.reset_for_tests()


def _plan(*names: str, labels: dict[str, str] | None = None):
    return mcp_venv_bootstrap.VenvPlan(labels=labels or {}, steps=[
        mcp_venv_bootstrap.VenvStep(n, mcp_venv_bootstrap.Path("/nowhere") / n, "python")
        for n in names])


def _boot_the_tools(caplog) -> list[str]:
    """Run the boot's tools pass and answer what it told admins."""
    async def scene():
        await startup._boot_mcps()
        await asyncio.sleep(0.05)
        for task in startup._bg_tasks:
            task.cancel()
    with caplog.at_level(logging.INFO):
        asyncio.run(scene())
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "environment built" in r.getMessage()]


def test_a_boot_with_every_tool_built_holds_nothing_and_says_nothing(monkeypatch, _fresh, caplog):
    monkeypatch.setattr(mcp_venv_bootstrap, "scan_bundled_venvs", _plan)
    monkeypatch.setattr(mcp_venv_bootstrap, "prune_unused_pythons", list)
    tool_envs.note_stale({"left-by-an-earlier-scan"})
    assert _boot_the_tools(caplog) == []
    assert (drain.holding(), tool_envs.stale_names(), _fresh) == (False, frozenset(), [])


def test_a_boot_builds_nothing_and_leaves_a_stale_tool_out_with_one_notice(monkeypatch, _fresh, caplog):
    """Nothing is built at a boot and no new work is held for it. The tools
    the scan found stale are left out of sessions, and the log names them
    and the command that builds them, once."""
    import config
    from services.mcp.mcp_session_filter import session_exclusion_reason
    built: list = []

    async def build(plan, causes=None):
        built.append(plan)
        return {}
    monkeypatch.setattr(mcp_venv_bootstrap, "scan_bundled_venvs",
                        lambda: _plan("a-mcp", "b-mcp", labels={"a-mcp": "A Tool"}))
    monkeypatch.setattr(mcp_venv_bootstrap, "prune_unused_pythons", list)
    monkeypatch.setattr(mcp_venv_bootstrap, "build_bundled_venvs", build)
    assert _boot_the_tools(caplog) == [_NOTICE.format(root=config.BASE_DIR.parent)]
    assert built == [] and drain.holding() is False and _fresh == []
    assert tool_envs.stale_names() == {"a-mcp", "b-mcp"}
    tool = SimpleNamespace(name="a-mcp", label="A Tool", exclude_from=[])
    assert session_exclusion_reason(tool, contexts=set(), external=False) == (
        "A Tool needs its environment built. An admin stops OtoDock, runs `scripts/dev-setup.sh` on the "
        "server and starts OtoDock again.")
    other = SimpleNamespace(name="c-mcp", label="C Tool", exclude_from=[])
    assert session_exclusion_reason(other, contexts=set(), external=False) is None


_NOTICE = ("These tools need their environment built: A Tool, b-mcp. No agent can use them until then. On the "
           "server, stop OtoDock, run {root}/scripts/dev-setup.sh and start OtoDock again.")


@pytest.fixture
def told(monkeypatch):
    """Every notification the platform fires, as its keywords."""
    from services.notifications import notification_manager
    fired: list[dict] = []

    async def fire(**kw):
        fired.append(kw)
        return [{"id": "d1"}]
    monkeypatch.setattr(notification_manager, "fire_notification", fire)
    return fired


def test_a_start_with_a_stale_tool_tells_the_admins_once(monkeypatch, _fresh, caplog, told):
    """A server without Docker used to build the tool at its boot. It builds
    nothing now, so the duty is the admin's: every admin is told once per
    start, in the words the log has, through the notification every admin
    notice goes through. The agent's sentence, the log and the notice name
    one command."""
    import config
    monkeypatch.setattr(mcp_venv_bootstrap, "scan_bundled_venvs",
                        lambda: _plan("a-mcp", "b-mcp", labels={"a-mcp": "A Tool"}))
    monkeypatch.setattr(mcp_venv_bootstrap, "prune_unused_pythons", list)
    logged = _boot_the_tools(caplog)
    assert told == []                                  # the boot's scan itself tells nobody: the start is not over
    assert asyncio.run(mcp_venv_bootstrap.tell_admins()) == "2 tools named to the admins"
    (notice,) = told
    assert notice == {"title": "Tools need their environment built", "body": logged[0], "severity": "warning",
                      "scope": "admin", "source": "platform", "source_id": "tools/environment"}
    assert notice["body"] == _NOTICE.format(root=config.BASE_DIR.parent)
    for words in (notice["body"], tool_envs.stale_reason("A Tool")):
        assert "scripts/dev-setup.sh" in words and "mcp_venv_bootstrap" not in words


def test_a_start_with_every_tool_built_tells_the_admins_nothing(monkeypatch, _fresh, caplog, told):
    monkeypatch.setattr(mcp_venv_bootstrap, "scan_bundled_venvs", _plan)
    monkeypatch.setattr(mcp_venv_bootstrap, "prune_unused_pythons", list)
    tool_envs.note_stale({"left-by-an-earlier-scan"}, "an earlier scan's words")
    assert _boot_the_tools(caplog) == []
    assert asyncio.run(mcp_venv_bootstrap.tell_admins()) is None and told == []


def _other_steps_stand_in(monkeypatch, seen: list) -> None:
    """Every step of the boot's continue but the tools' notice records its
    name and whether the platform still held new work, and does nothing."""
    from services.apps import link_upgrade
    from services.mcp import tool_listing
    from services.phone import outside_upgrade, wrapup
    from services.scheduler import continue_pass, run_recovery, scheduler

    async def sleep(_s):
        return None
    monkeypatch.setattr(startup.asyncio, "sleep", sleep)

    def step(name):
        async def run(*_a, **_k):
            seen.append((name, drain.holding()))
            return 0
        return run
    for mod, name in ((continue_pass, "arm_queues"), (continue_pass, "run_pending"),
                      (run_recovery, "report_failed_at_boot"), (continue_pass, "settle_waiters"),
                      (run_recovery, "start_pending_runs"), (scheduler, "fire_held"),
                      (scheduler, "redeliver_pending_wakes"), (scheduler, "fire_downtime"),
                      (outside_upgrade, "first_boot"), (outside_upgrade, "name_old_calls"),
                      (outside_upgrade, "add_conversations_tool"), (link_upgrade, "first_boot"),
                      (wrapup, "resume_after_boot"), (tool_listing, "relist_bundled_after_upgrade")):
        monkeypatch.setattr(mod, name, step(name))


def test_the_admins_are_told_in_the_continue_once_the_start_is_over(monkeypatch, _fresh, caplog, told):
    """Where the boot's other notices go out: after the start's hold ended,
    when the notifications run and the server listens. The real step, once."""
    monkeypatch.setattr(mcp_venv_bootstrap, "scan_bundled_venvs", lambda: _plan("a-mcp"))
    monkeypatch.setattr(mcp_venv_bootstrap, "prune_unused_pythons", list)
    _boot_the_tools(caplog)
    seen: list = []
    _other_steps_stand_in(monkeypatch, seen)
    held_when_told: list[bool] = []
    fire = mcp_venv_bootstrap.tell_admins

    async def tell():
        held_when_told.append(drain.holding())
        return await fire()
    monkeypatch.setattr(mcp_venv_bootstrap, "tell_admins", tell)

    async def scene():
        drain.hold_for_start()
        await startup._boot_continue()
    with caplog.at_level(logging.INFO):
        asyncio.run(scene())
    assert held_when_told == [False] and len(told) == 1 and told[0]["scope"] == "admin"
    assert "Startup, the tools whose environment is not built: 1 tools named to the admins" in caplog.text


def test_the_hold_ends_once_the_compute_machine_is_back(monkeypatch):
    from services.remote import compute_bootstrap
    gate: dict = {}
    order: list[str] = []

    async def back():
        order.append("wait-start")
        await gate["go"].wait()
        order.append("back")
    monkeypatch.setattr(compute_bootstrap, "await_back", back)
    drain.on_start_ended(lambda: order.append("hold-ended"))

    async def scene():
        gate["go"] = asyncio.Event()
        drain.hold_for_start()
        ending = asyncio.create_task(startup._end_start_hold())
        await asyncio.sleep(0.05)
        assert drain.holding(), "the hold ended before the machine was back"
        gate["go"].set()
        await ending
    asyncio.run(scene())
    assert order == ["wait-start", "back", "hold-ended"]
    assert drain.holding() is False


def test_a_stop_during_the_wait_drops_the_hold_with_no_hook(monkeypatch):
    from services.remote import compute_bootstrap
    seen: list[str] = []
    drain.on_start_ended(lambda: seen.append("ended"))

    async def never():
        await asyncio.Event().wait()
    monkeypatch.setattr(compute_bootstrap, "await_back", never)

    async def scene():
        drain.hold_for_start()
        ending = asyncio.create_task(startup._end_start_hold())
        await asyncio.sleep(0.02)
        assert drain.starting()
        ending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ending
    asyncio.run(scene())
    assert (drain.starting(), seen) == (False, [])


def test_the_continue_ends_the_hold_before_it_takes_anything_up(monkeypatch, _fresh):
    """Held messages, cut turns, held runs and continuations go out after the
    hold ends, in the boot's order: none of the steps runs while it holds."""
    seen: list = []
    _other_steps_stand_in(monkeypatch, seen)

    async def scene():
        drain.hold_for_start()
        await startup._boot_continue()
    asyncio.run(scene())
    names = [n for n, _ in seen]
    assert all(not held for _, held in seen), seen
    assert names.index("arm_queues") == 0
    assert names.index("run_pending") < names.index("fire_held") < names.index("fire_downtime")
    # The held mark is a drain's: the start writes none and clears none.
    assert _fresh == []


# ---------------------------------------------------------------------------
# The start and the install's compute machine (``startup._boot_compute``,
# ``drain.refusal``): nothing runs before it signed in, the doors say the
# platform is starting, and a start that has none to wait for says so.
# ---------------------------------------------------------------------------

def _compute_seams(monkeypatch, *, runs_own: bool, expects: bool, ensure=None):
    from services.remote import compute_bootstrap
    monkeypatch.setattr(compute_bootstrap, "runs_own", lambda: runs_own)
    monkeypatch.setattr(compute_bootstrap, "ensure", ensure or (lambda: "compute-1"))
    monkeypatch.setattr(compute_bootstrap, "load_addresses", lambda: None)
    monkeypatch.setattr(compute_bootstrap, "expect_back", lambda: expects)


def test_the_start_holds_new_work_for_the_compute_machine_and_its_doors_say_starting(monkeypatch):
    """While only the start holds new work a door says the platform is
    starting, never that it is updating. A drain, or the stop, keeps the
    door's own words."""
    import config
    monkeypatch.setattr(config, "UNREAD_KEYS_SET", ())
    _compute_seams(monkeypatch, runs_own=True, expects=True)
    assert drain.refusal("updating words") == "updating words"
    startup._boot_compute()
    assert drain.holding() and drain.starting()
    assert drain.refusal("updating words") == drain.STARTING_LINE == "OtoDock is starting. Try again in a minute."

    async def scene():
        await drain.drain(60)
        during = drain.refusal("updating words")
        await drain.resume()
        return during
    assert asyncio.run(scene()) == "updating words"
    drain.note_stopping()
    assert drain.refusal("updating words") == "updating words"
    drain.note_running()
    asyncio.run(drain.end_start_hold())
    assert not drain.holding() and drain.refusal("updating words") == "updating words"


def test_a_start_with_no_compute_machine_of_its_own_says_so_and_holds_nothing(monkeypatch, caplog):
    """No silent start: an install that names no compute machine logs that
    nothing can run and how to set one up. A retired key still in the file
    is named once, with what to do."""
    import logging

    import config
    monkeypatch.setattr(config, "UNREAD_KEYS_SET", ("OTODOCK_COMPUTE_SESSIONS",))
    _compute_seams(monkeypatch, runs_own=False, expects=False)
    with caplog.at_level(logging.WARNING):
        startup._boot_compute()
    said = [r.getMessage() for r in caplog.records]
    assert any("OTODOCK_COMPUTE_SESSIONS is set and does nothing since 2.0: chats, tasks and calls run on a compute machine. Delete the line." in m
               for m in said)
    assert any("names no compute machine of its own" in m and "Chats, tasks and calls cannot run until one signs in" in m
               and "scripts/dev-setup.sh --service" in m for m in said)
    assert not drain.holding()


def test_the_retired_exit_key_is_named_with_its_own_reason(monkeypatch, caplog):
    """An install of the test days that wrote ``OTODOCK_CREDENTIAL_EXIT``
    gets one line with that key's own reason: the exit always runs, and no
    reader of the key is left."""
    import logging

    import config
    from configuration import readers
    assert "OTODOCK_CREDENTIAL_EXIT" in readers.INTERNAL_CONFIG_KEYS
    assert not hasattr(config, "OTODOCK_CREDENTIAL_EXIT")
    monkeypatch.setattr(config, "UNREAD_KEYS_SET", ("OTODOCK_CREDENTIAL_EXIT",))
    _compute_seams(monkeypatch, runs_own=True, expects=False)
    with caplog.at_level(logging.WARNING):
        startup._boot_compute()
    said = [r.getMessage() for r in caplog.records]
    assert ("Config: OTODOCK_CREDENTIAL_EXIT is set and does nothing since 2.0: the credential exit always runs. "
            "Delete the line.") in said
    assert not any("compute machine" in m for m in said if "OTODOCK_CREDENTIAL_EXIT" in m)


def test_every_retired_key_has_its_own_reason():
    assert set(startup.UNREAD_KEY_SENTENCES) == {"OTODOCK_COMPUTE_SESSIONS", "OTODOCK_CREDENTIAL_EXIT"}


def test_a_credential_that_cannot_be_written_is_an_error_line_and_the_boot_goes_on(monkeypatch, caplog):
    import logging

    import config
    monkeypatch.setattr(config, "UNREAD_KEYS_SET", ())

    def _fails():
        raise OSError("read-only file system")
    _compute_seams(monkeypatch, runs_own=True, expects=True, ensure=_fails)
    with caplog.at_level(logging.WARNING):
        startup._boot_compute()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "credential was not written" in errors[0].getMessage()
    assert "chats, tasks and calls cannot run until this is fixed" in errors[0].getMessage()
    assert not [r for r in caplog.records if "names no compute machine" in r.getMessage()]
    assert drain.holding(), "the start still waits its bound for the machine"


def test_the_retired_compute_key_is_read_from_the_file_only_to_name_it(monkeypatch):
    """``OTODOCK_COMPUTE_SESSIONS`` is no switch any more: no reader of it is
    left but the list the boot names, and an empty value names nothing."""
    import config
    from configuration import readers
    assert "OTODOCK_COMPUTE_SESSIONS" in readers.INTERNAL_CONFIG_KEYS
    assert isinstance(config.UNREAD_KEYS_SET, tuple)
    assert set(config.UNREAD_KEYS_SET) <= {"OTODOCK_COMPUTE_SESSIONS", "OTODOCK_CREDENTIAL_EXIT"}
    from services.remote import compute_bootstrap, remote_status
    assert not hasattr(remote_status, "compute_sessions_on")
    monkeypatch.setattr(config, "OTODOCK_COMPUTE_LINK_DIR", "/run/otodock/link", raising=False)
    monkeypatch.setattr(config, "OTODOCK_COMPUTE_SESSIONS", "local", raising=False)
    assert compute_bootstrap.runs_own() is True
