"""The management socket's config pushes: the daemon applies the newest one,
whether or not its handler existed when the push landed.

The proxy answers the capabilities frame with a ``config_update`` carrying
the outbound PINs 150 ms after ``config_full``, both stamped with the same
second; ``main`` used to register its handler after the PIN seed await, so
that push stayed unread and the connect-time config (no outbound PIN) held
until the next route save.
"""

import asyncio
import json
import logging

import pytest

import main
from calls import pin_failures
from config_manager import ConfigManager
from proxy.management_ws import ManagementWSClient


class _Socket:
    """The proxy's frames as the client's ``async for`` reads them."""

    def __init__(self, frames):
        self._frames = [json.dumps(f) for f in frames]
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _config(version: int, *, pin: str) -> dict:
    return {"version": version, "routes": [{
        "id": "r-out", "direction": "outbound", "agent": "a", "enabled": True,
        "identity_mode": "user", "identity_user_sub": "local:u", "pin": pin,
    }]}


def _push(kind: str, version: int, *, pin: str) -> dict:
    return {"type": kind, "data": _config(version, pin=pin)}


def _client() -> ManagementWSClient:
    return ManagementWSClient("http://proxy:8400", "key")


def test_the_latest_push_is_kept_without_a_handler_and_handed_to_a_later_one():
    mgmt = _client()
    asyncio.run(mgmt._handle_messages(_Socket([
        _push("config_full", 5, pin=""), _push("config_update", 5, pin="123456"),
    ])))
    assert mgmt.latest_config()["routes"][0]["pin"] == "123456"
    assert mgmt.config_version == 5
    got: list[dict] = []
    mgmt.on_config_changed = got.append
    asyncio.run(mgmt._handle_messages(_Socket([_push("config_update", 6, pin="654321")])))
    assert [c["version"] for c in got] == [6] and mgmt.latest_config() is got[0]


class _Listeners:
    def __init__(self):
        self.applied: list[ConfigManager] = []

    async def apply(self, cfg):
        self.applied.append(cfg)


@pytest.fixture
def quiet_start(monkeypatch):
    """The handler's side effects as recorders; the seed globals reset so
    "started once" is this test's, not an earlier module's."""
    calls = {"fillers": 0, "classifier": 0, "pbx": 0, "seed_fetch": 0}

    async def _fillers(cfg):
        calls["fillers"] += 1

    def _classifier(cfg):
        calls["classifier"] += 1

    async def _fetch(window_s):
        calls["seed_fetch"] += 1
        return []

    monkeypatch.setattr(main, "prewarm_fillers", _fillers)
    monkeypatch.setattr(main, "prewarm_turn_classifier", _classifier)
    monkeypatch.setattr(main, "_schedule_pbx_refresh",
                        lambda cfg, logger: calls.__setitem__("pbx", calls["pbx"] + 1))
    monkeypatch.setattr(main, "fetch_pin_failures", _fetch)
    monkeypatch.setattr(main, "_pin_seed_task", None)
    monkeypatch.setattr(pin_failures, "store", pin_failures.PinFailureStore())
    return calls


def test_wire_config_applies_the_push_that_landed_before_the_handler(quiet_start):
    """``wait_for_config`` returned the connect-time config; the PIN push
    came while the start awaited something else: the first apply is that
    push, and the seed, the prewarms and the listener apply run once."""
    async def scenario():
        mgmt = _client()
        await mgmt._handle_messages(_Socket([
            _push("config_full", 5, pin=""), _push("config_update", 5, pin="123456"),
        ]))
        first = await mgmt.wait_for_config()      # the awaited value is the latest too
        assert first["routes"][0]["pin"] == "123456"
        cfg, listeners = ConfigManager(), _Listeners()
        main._wire_config(mgmt, cfg, listeners, logging.getLogger("test"))
        assert cfg.get_outbound_route("r-out").pin == "123456"
        seed = main._pin_seed_task
        assert seed is not None
        await asyncio.wait_for(seed, 2)
        await _settle()
        assert listeners.applied == [cfg] and pin_failures.store.seeded
        # A later push reaches cfg through the handler, and re-runs the side effects.
        await mgmt._handle_messages(_Socket([_push("config_update", 6, pin="")]))
        assert cfg.get_outbound_route("r-out").pin == ""
        await _settle()
        assert listeners.applied == [cfg, cfg]

    async def _settle():
        """The handler's tasks (one runs in a thread) finish before the counts are read."""
        me = asyncio.current_task()
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not me])

    asyncio.run(scenario())
    assert quiet_start["fillers"] == 2 and quiet_start["classifier"] == 2
    assert quiet_start["pbx"] == 2 and quiet_start["seed_fetch"] == 1
