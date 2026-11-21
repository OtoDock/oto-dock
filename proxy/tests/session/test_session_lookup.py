"""Is the session live, and on which layer: the one lookup beside
``session_manager.find_layer_for_session``, and the one reading of a chat's
pin for its layer (``layer_for_chat``)."""

from __future__ import annotations

import pytest

from core.session import session_manager


class _Remote:
    """The remote layer's slice the lookup reads, by single keys."""

    def __init__(self, sessions: dict, grace: set = frozenset()):
        self.sessions = sessions
        self.grace = set(grace)

    def owns_session(self, sid):
        return sid in self.sessions

    def machine_of_session(self, sid):
        return self.sessions.get(sid, "")

    def is_session_grace_held(self, sid):
        return sid in self.grace

    def session_ids_on(self, machine_id):
        raise AssertionError("the lookup never iterates the remote layer's sessions")


@pytest.fixture
def places(monkeypatch):
    up: set = set()
    manager = type("M", (), {"is_connected": staticmethod(up.__contains__)})()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: manager)

    def _install(remote):
        monkeypatch.setattr(session_manager, "_remote_layer", remote)
    return up, _install


def test_nobody_holds_it(places):
    _up, install = places
    install(_Remote({}))
    assert session_manager.live_session("s-none") is None
    assert session_manager.live_session("") is None


def test_the_proxy_holds_it(places, monkeypatch):
    _up, install = places
    install(None)
    layer = session_manager.holding_engines()["direct-llm"]
    monkeypatch.setattr(layer, "owns_session", lambda sid: sid == "s-local")
    live = session_manager.live_session("s-local")
    assert live is not None and live.layer is layer
    assert live.machine_id == "" and live.linked and live.reachable


def test_a_machine_holds_it_and_answers_or_not(places):
    up, install = places
    remote = _Remote({"s-far": "m-1", "s-back": "m-2"}, grace={"s-back"})
    install(remote)
    away = session_manager.live_session("s-far")
    assert away is not None and away.layer is remote and away.machine_id == "m-1"
    assert not away.linked and not away.grace and not away.reachable
    up.add("m-1")
    assert session_manager.live_session("s-far").reachable
    back = session_manager.live_session("s-back")
    assert not back.linked and back.grace and back.reachable


def test_the_lookup_never_creates_the_remote_layer(places, monkeypatch):
    _up, install = places
    install(None)
    monkeypatch.setattr(session_manager, "_get_remote_layer",
                        lambda: pytest.fail("the lookup must not create the remote layer"))
    assert session_manager.live_session("s-none") is None


def test_one_reading_of_a_chats_pin_resolves_an_empty_pin_now(monkeypatch):
    """An unpinned chat (its machine was removed, or no warm-up placed it)
    takes the agent's place now, at every reader: the kick's layer and the
    socket's no longer part on it."""
    seen = []
    monkeypatch.setattr(session_manager, "get_execution_layer",
                        lambda agent, **kw: seen.append((agent, kw["execution_target"])) or "layer")
    for chat in ({"execution_target": ""}, {}, None):
        assert session_manager.layer_for_chat("a", chat, user_sub="s", role="editor") == "layer"
    session_manager.layer_for_chat("a", {"execution_target": "local"}, user_sub="s", role="editor")
    session_manager.layer_for_chat("a", {"execution_target": "m-1"}, user_sub="s", role="editor")
    assert seen == [("a", ""), ("a", ""), ("a", ""), ("a", "local"), ("a", "m-1")]


def test_the_kick_and_the_socket_read_a_chats_layer_the_same_way(monkeypatch):
    """Both of the socket's readers go through ``layer_for_chat``: an empty
    pin is no longer the platform for one and the agent's place for the
    other."""
    import inspect

    from ws import dashboard
    for fn in (dashboard._role_and_layer, dashboard.DashboardConnection._resolve_layer_for_chat):
        src = inspect.getsource(fn)
        assert "layer_for_chat(" in src and "placement.LOCAL" not in src
