"""``platform_stopping``: the platform says for how long at most it stops,
and the local link tells a held call how long to hold (``hold_s``)."""

import json
import time
from types import SimpleNamespace

import pytest

from satellite.sessions import alive_report, codex_session
from satellite.transport.http_tunnel import LocalTunnelServer
from satellite.transport.ws_client import SatelliteWSClient


def _client() -> SatelliteWSClient:
    cfg = SimpleNamespace(machine_id="m-1", machine_secret="s", platform_url="ws://127.0.0.1:1")
    return SatelliteWSClient(cfg, SimpleNamespace(pty_sessions={}, sessions={}))


@pytest.mark.asyncio
async def test_the_frame_sets_the_hold_and_zero_takes_it_back():
    client = _client()
    assert client.platform_hold_s() == 0
    await client._dispatch_frame("platform_stopping", {"type": "platform_stopping", "back_within_s": 900})
    assert 898 <= client.platform_hold_s() <= 900
    await client._dispatch_frame("platform_stopping", {"type": "platform_stopping", "back_within_s": 0})
    assert client.platform_hold_s() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-1, 3601, "900", 9.5, True, None])
async def test_a_value_that_is_no_whole_number_of_seconds_up_to_an_hour_is_ignored(bad):
    client = _client()
    client.platform_back_by = time.monotonic() + 60
    await client._dispatch_frame("platform_stopping", {"type": "platform_stopping", "back_within_s": bad})
    assert 58 <= client.platform_hold_s() <= 60


def test_a_hold_that_is_over_reads_as_none():
    client = _client()
    client.platform_back_by = time.monotonic() - 1
    assert client.platform_hold_s() == 0


def test_the_local_link_names_the_hold_only_while_one_stands():
    client = _client()
    tunnel = LocalTunnelServer.__new__(LocalTunnelServer)
    tunnel.ws_client = client
    assert tunnel._hold() == {}
    client.platform_back_by = time.monotonic() + 120
    assert 119 <= tunnel._hold()["hold_s"] <= 121
    # An older client object (a test's stand-in) names none.
    tunnel.ws_client = SimpleNamespace()
    assert tunnel._hold() == {}


def test_the_bridge_reads_the_hold_from_the_links_answer():
    assert codex_session._hold_seconds(json.dumps({"error": "tunnel-not-connected", "hold_s": 600})) == 600
    # A fraction is read as the permission gate reads it.
    assert codex_session._hold_seconds(json.dumps({"error": "x", "hold_s": 90.5})) == 90.5
    assert codex_session._hold_seconds(json.dumps({"error": "x", "hold_s": 99999})) == 3600
    for body in ("", "not json", json.dumps({"error": "x"}), json.dumps({"hold_s": "600"}),
                 json.dumps({"hold_s": True}), json.dumps({"hold_s": -5}), json.dumps([1])):
        assert codex_session._hold_seconds(body) == 0


def test_a_list_that_failed_on_the_machine_is_said_in_the_report(monkeypatch):
    sm = SimpleNamespace(headless_sessions_alive=lambda: [], sessions={})
    assert "partial" not in alive_report.sessions_alive_frame(sm)

    def boom(_sm):
        raise RuntimeError("list failed")

    monkeypatch.setattr(alive_report, "other_sessions_held", boom)
    frame = alive_report.sessions_alive_frame(sm)
    assert frame["partial"] is True and "other_sessions" not in frame
