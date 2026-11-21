"""The install's own compute machine (``services/remote/compute_bootstrap``):
its row made once, its credential file written by the install and minted
again only when it does not verify (a restore onto a new host, a lost
volume) or names another marker, the human route refusing it, and the
start's hold waiting for a compute machine the reloaded sessions run on."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import config as app_config
from services.remote import compute_bootstrap as cb
from storage import database, remote_store


@pytest.fixture
def own(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_SESSIONS", "compute", raising=False)
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_LINK_DIR", str(tmp_path / "link"), raising=False)
    monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path / "agents", raising=False)
    cb.reset_for_tests()
    yield tmp_path
    cb.reset_for_tests()


def _file(tmp_path) -> dict:
    return json.loads((tmp_path / "link" / cb.CREDENTIAL_NAME).read_text())


def test_the_row_and_the_credential_are_made_once_and_kept(own):
    machine_id = cb.ensure()
    assert machine_id and cb.own_machine_id() == machine_id
    row = remote_store.get_remote_machine(machine_id)
    assert row["pairing_scope"] == "compute" and row["name"] == cb.OWN_MACHINE_NAME
    held = _file(own)
    assert held["machine_id"] == machine_id
    assert remote_store.verify_machine_secret(machine_id, held["machine_secret"])
    assert held["marker"] == (own / "agents" / ".otodock-platform").read_text().strip()
    assert oct(os.stat(own / "link" / cb.CREDENTIAL_NAME).st_mode & 0o777) == "0o600"
    # A second boot keeps all of it.
    assert cb.ensure() == machine_id and _file(own) == held


def test_a_credential_the_row_does_not_verify_is_minted_again(own):
    """A restore onto a new host brings the row's old hash and no file (or a
    file of another install): the boot mints again, so the compute machine
    signs in at its next start."""
    machine_id = cb.ensure()
    old = _file(own)
    remote_store.mint_compute_secret(machine_id)  # the restored hash is another
    assert not remote_store.verify_machine_secret(machine_id, old["machine_secret"])
    cb.ensure()
    new = _file(own)
    assert new["machine_secret"] != old["machine_secret"]
    assert remote_store.verify_machine_secret(machine_id, new["machine_secret"])
    (own / "link" / cb.CREDENTIAL_NAME).unlink()
    cb.ensure()
    assert remote_store.verify_machine_secret(machine_id, _file(own)["machine_secret"])


def test_a_credential_naming_another_marker_is_written_again(own):
    cb.ensure()
    (own / "agents" / ".otodock-platform").write_text("0123456789abcdef0123456789abcdef\n")
    cb.ensure()
    assert _file(own)["marker"] == "0123456789abcdef0123456789abcdef"


def test_a_symlinked_marker_never_puts_its_targets_content_in_the_credential(own):
    """The agents folder is writable from the compute machine: a marker made a
    link to the proxy's secrets must not be read through, and no value read
    from that folder reaches the credential the compute machine reads unless
    it is a token."""
    secrets_file = own / "config.env"
    secrets_file.write_text("PROXY_API_KEY=probe-master-key\n")
    cb.ensure()
    marker = own / "agents" / ".otodock-platform"
    marker.unlink()
    marker.symlink_to(secrets_file)
    cb.ensure()
    cb.ensure()
    held = (own / "link" / cb.CREDENTIAL_NAME).read_text()
    assert "probe-master-key" not in held
    assert not marker.is_symlink() and marker.is_file()
    assert secrets_file.read_text() == "PROXY_API_KEY=probe-master-key\n"
    assert _file(own)["marker"] == marker.read_text().strip()


@pytest.mark.parametrize("plant", ["directory", "long", "words", "fifo"])
def test_a_marker_that_is_not_a_token_in_a_regular_file_is_replaced(own, plant):
    (own / "agents").mkdir(parents=True)
    marker = own / "agents" / ".otodock-platform"
    if plant == "directory":
        marker.mkdir()
    elif plant == "long":
        marker.write_text("a" * 4096)
    elif plant == "words":
        marker.write_text("PROXY_API_KEY=probe-master-key\n")
    else:
        os.mkfifo(marker)
    token = cb.ensure_marker()
    assert len(token) == 32 and all(c in "0123456789abcdef" for c in token)
    assert marker.is_file() and not marker.is_symlink()
    assert marker.read_text().strip() == token
    assert cb.ensure_marker() == token


def test_a_released_installs_empty_marker_gains_a_token_kept_after(own):
    (own / "agents").mkdir(parents=True)
    (own / "agents" / ".otodock-platform").touch()
    token = cb.ensure_marker()
    assert token and cb.ensure_marker() == token


def test_without_a_compute_service_of_its_own_nothing_is_made(own, monkeypatch):
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_LINK_DIR", "", raising=False)
    assert cb.runs_own() is False
    assert cb.ensure() == ""
    assert database.get_platform_setting(cb.OWN_MACHINE_KEY) == ""


def test_the_retired_word_local_no_longer_keeps_the_install_from_its_compute_machine(own, monkeypatch):
    """An install that kept ``OTODOCK_COMPUTE_SESSIONS=local`` from an
    internal build: the key is not obeyed, its compute machine is made."""
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_SESSIONS", "local", raising=False)
    assert cb.runs_own() is True and cb.ensure() != ""


def test_the_pairing_route_refuses_the_installs_own_machine(own):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.remote import compute_machines
    from auth.providers import UserContext, get_current_user
    machine_id = cb.ensure()
    user = UserContext(sub="sub-admin", email="a@test.com", name="A", role="admin", agents=[], agent_roles={})

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(compute_machines.router)
    app.dependency_overrides[get_current_user] = _stub
    r = TestClient(app).post(f"/v1/admin/remote-machines/compute/{machine_id}/pair")
    assert r.status_code == 404
    assert remote_store.verify_machine_secret(machine_id, _file(own)["machine_secret"])


def test_the_remove_route_refuses_the_installs_own_machine(own):
    """The page shows it no Remove, and the route refuses it too: removed, the
    running machine would knock with a row that is gone."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.remote import remote_machines
    from auth.providers import UserContext, get_current_user
    machine_id = cb.ensure()
    user = UserContext(sub="sub-admin", email="a@test.com", name="A", role="admin", agents=[], agent_roles={})

    async def _stub():
        return user

    app = FastAPI()
    app.include_router(remote_machines.router)
    app.dependency_overrides[get_current_user] = _stub
    r = TestClient(app).delete(f"/v1/admin/remote-machines/{machine_id}")
    assert r.status_code == 400 and r.json()["detail"] == "This machine's sign-in is kept by the install itself."
    assert remote_store.get_remote_machine(machine_id)


def _rows_for_any(monkeypatch) -> None:
    """The store answers a row for every machine and names no own machine
    (these tests run without a database)."""
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda m: {"id": m})
    monkeypatch.setattr(cb, "own_machine_id", lambda: "")


def test_the_start_waits_for_a_compute_machine_the_reloaded_sessions_run_on(monkeypatch):
    from core.session import session_state
    _rows_for_any(monkeypatch)
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_SESSIONS", "compute", raising=False)
    monkeypatch.setattr(session_state, "compute_machines_placed", lambda _keep=None: {"c-1"})
    cb.reset_for_tests()

    async def _run():
        assert cb.expect_back() is True
        waiting = asyncio.create_task(cb.await_back(5))
        await asyncio.sleep(0)
        await cb.on_alive_report(SimpleNamespace(machine_id="c-2", complete=True))
        await cb.on_alive_report(SimpleNamespace(machine_id="c-1", complete=False))
        await asyncio.sleep(0)
        assert not waiting.done()
        await cb.on_alive_report(SimpleNamespace(machine_id="c-1", complete=True))
        await asyncio.wait_for(waiting, 1)

    asyncio.run(_run())
    cb.reset_for_tests()


def test_the_start_waits_only_for_compute_machines_that_can_come_back(own, monkeypatch):
    """A saved session may name a compute row deleted since, or the install's
    own machine on a boot that runs none (``local``, no credential folder):
    waiting for those would hold every boot's new work for the full bound."""
    from core.session import session_state
    machine_id = cb.ensure()
    remote_store.create_remote_machine("c-admin", "Another", None, pairing_scope="compute")
    monkeypatch.setattr(session_state, "compute_machines_placed", lambda _keep=None: {"c-gone", "c-admin", machine_id})
    assert cb.expect_back() is True and cb._expected == {"c-admin", machine_id}
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_LINK_DIR", "", raising=False)
    assert cb.expect_back() is True and cb._expected == {"c-admin"}
    monkeypatch.setattr(session_state, "compute_machines_placed", lambda _keep=None: {"c-gone", machine_id})
    assert cb.expect_back() is False
    cb.reset_for_tests()


def test_a_report_before_the_wait_ends_it_and_says_so_with_the_machines_id(monkeypatch, caplog):
    """The order seen on a restart: the machine signs in and reports before the
    start's hold reaches its wait. The wait returns at once and the log names
    the machine whole (every compute id starts with "compute-")."""
    from core.session import session_state
    _rows_for_any(monkeypatch)
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_SESSIONS", "compute", raising=False)
    monkeypatch.setattr(session_state, "compute_machines_placed", lambda _keep=None: {"compute-1bcf29cf16c6"})
    cb.reset_for_tests()
    caplog.set_level("INFO", logger="otodock")

    async def _run():
        assert cb.expect_back() is True
        await cb.on_alive_report(SimpleNamespace(machine_id="compute-1bcf29cf16c6", complete=True))
        await asyncio.wait_for(cb.await_back(5), 1)

    asyncio.run(_run())
    said = [r.getMessage() for r in caplog.records]
    assert any("waiting for the compute machine compute-1bcf29cf16c6" in m for m in said), said
    assert any("the compute machine compute-1bcf29cf16c6 is back" in m for m in said), said
    cb.reset_for_tests()


def test_the_wait_is_bounded_and_an_install_with_no_compute_machine_awaits_none(monkeypatch):
    from core.session import session_state
    _rows_for_any(monkeypatch)
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_LINK_DIR", "", raising=False)
    monkeypatch.setattr(session_state, "compute_machines_placed", lambda _keep=None: {"c-1"})

    async def _bounded():
        assert cb.expect_back()
        await cb.await_back(0.05)  # returns, the expectation dropped
        assert not cb._expected

    asyncio.run(_bounded())
    # No session placed on a machine, and no compute machine of its own.
    monkeypatch.setattr(session_state, "compute_machines_placed", lambda _keep=None: set())
    assert cb.expect_back() is False
    cb.reset_for_tests()


def test_the_start_holds_for_the_installs_own_compute_machine_with_no_session_on_it(own, monkeypatch):
    """Nothing runs before the install's own compute machine signed in: the
    start holds new work for it with no session to adopt, for the short
    bound, and for the long one when the reloaded index names a session on
    a machine. Its complete report ends the wait."""
    from core.session import session_state
    machine_id = cb.ensure()
    monkeypatch.setattr(session_state, "compute_machines_placed", lambda _keep=None: set())
    assert cb.expect_back() is True
    assert cb._expected == {machine_id} and cb._bound_s == cb.OWN_WAIT_S
    monkeypatch.setattr(session_state, "compute_machines_placed", lambda _keep=None: {machine_id})
    assert cb.expect_back() is True and cb._bound_s == cb.BOOT_WAIT_S

    async def _reported():
        assert cb.expect_back()
        waited = asyncio.ensure_future(cb.await_back())
        await asyncio.sleep(0)
        assert not waited.done()
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                            lambda: SimpleNamespace(is_compute=lambda mid: mid == machine_id))
        woken: list = []
        monkeypatch.setattr("core.concurrency.compute_signed_in", woken.append)
        await cb.on_alive_report(SimpleNamespace(machine_id=machine_id, complete=False))
        assert not waited.done() and woken == []
        await cb.on_alive_report(SimpleNamespace(machine_id=machine_id, complete=True))
        await asyncio.wait_for(waited, 1.0)
        assert woken == [machine_id] and not cb.waiting()

    asyncio.run(_reported())
    cb.reset_for_tests()


def test_a_short_restart_of_the_installs_own_machine_is_told_from_one_that_is_away(own, monkeypatch):
    """``back_soon``: the install's own compute machine is not connected and
    dropped within its reconnect window. Connected, away longer, never
    seen, or an install with no compute service of its own: not."""
    machine_id = cb.ensure()
    state = {"connected": False, "recent": True}
    manager = SimpleNamespace(is_connected=lambda mid: mid == machine_id and state["connected"],
                              machine_dropped_recently=lambda mid: mid == machine_id and state["recent"])
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: manager)
    assert cb.back_soon() is True
    state["connected"] = True
    assert cb.back_soon() is False
    state.update(connected=False, recent=False)
    assert cb.back_soon() is False
    state["recent"] = True
    monkeypatch.setattr(app_config, "OTODOCK_COMPUTE_LINK_DIR", "", raising=False)
    assert cb.back_soon() is False


def test_a_chat_waits_only_when_nothing_can_start_it_and_the_machine_is_coming_back(own, monkeypatch):
    """``chat_waits`` is the builders' refusal and the short restart both:
    a chat a machine can take, a chat on a paired computer and a machine
    away longer are not kept waiting."""
    from core.session import session_manager
    refused = {"c-1"}
    monkeypatch.setattr(session_manager, "no_compute_for", lambda chat: chat["id"] in refused)
    soon = {"on": True}
    monkeypatch.setattr(cb, "back_soon", lambda: soon["on"])
    assert cb.chat_waits({"id": "c-1"}) is True
    assert cb.chat_waits({"id": "c-2"}) is False        # a machine can take it
    assert cb.chat_waits(None) is False and cb.chat_waits({}) is False
    soon["on"] = False
    refused.add("c-3")
    assert cb.chat_waits({"id": "c-3"}) is False        # away longer: refused, not kept


def test_a_compute_machines_complete_report_arms_every_chat_with_waiting_messages(own, monkeypatch):
    """The sign-in: a compute machine's complete report arms every chat that
    holds waiting messages, as a boot does. The reconnect pass arms only the
    chats pinned to a machine, and a compute machine is never a pin, so a
    message queued while it was away would otherwise wait for a touch. A
    report that is not complete, and a paired computer's, arm nothing, and
    a failed read ends nothing."""
    from services.scheduler import continue_pass
    machine_id = cb.ensure()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                        lambda: SimpleNamespace(is_compute=lambda mid: mid == machine_id))
    monkeypatch.setattr("core.concurrency.compute_signed_in", lambda machine_id="": None)
    armed: list = []

    async def _arm_queues():
        armed.append(1)
        if len(armed) == 2:
            raise RuntimeError("the store did not answer")
        return 2
    monkeypatch.setattr(continue_pass, "arm_queues", _arm_queues)

    async def _reports():
        await cb.on_alive_report(SimpleNamespace(machine_id=machine_id, complete=False))
        await cb.on_alive_report(SimpleNamespace(machine_id="paired-1", complete=True))
        assert armed == []
        await cb.on_alive_report(SimpleNamespace(machine_id=machine_id, complete=True))
        assert armed == [1]
        await cb.on_alive_report(SimpleNamespace(machine_id=machine_id, complete=True))
        assert armed == [1, 1]
    asyncio.run(_reports())


@pytest.mark.asyncio
async def test_a_removed_machines_terminals_close_with_its_sessions_before_its_link(monkeypatch):
    """A terminal on a removed machine closes with it, rather than sitting in
    the reconnect window with its "reconnecting" banner."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    order: list[str] = []

    async def _headless(sid):
        order.append(f"headless:{sid}")
    remote = SimpleNamespace(session_ids_on=lambda mid: {"h-1"}, close_session=_headless)
    monkeypatch.setattr("core.session.session_manager._get_remote_layer", lambda: remote)
    monkeypatch.setattr("core.remote.remote_pty.machine_pty_session_ids",
                        lambda mid: {"t-1"} if mid == "rack-2" else set())

    async def _terminal(sid, *, reason="closed", **_kw):
        order.append(f"terminal:{sid}:{reason}")
        return True
    monkeypatch.setattr("core.session.interactive_session.close_session", _terminal)

    async def _ws_close(**_kw):
        order.append("4006")
    conn = SimpleNamespace(ws=SimpleNamespace(close=_ws_close))
    cm = SimpleNamespace(get_connection=lambda mid: conn, deregister=AsyncMock())
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    await cb.unlink("rack-2")
    assert order == ["headless:h-1", "terminal:t-1:machine_removed", "4006"]
