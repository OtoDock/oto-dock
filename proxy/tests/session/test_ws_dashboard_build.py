"""The dashboard build id over the socket: ``server_info`` last of the
connect-time frames, and inside every ``pong`` — the two signals a running
page compares against its own stamp to reload once after a deploy."""

from __future__ import annotations

import config
from tests.fixtures.ws_dashboard_harness import (
    ANY,
    dashboard_connection,
    run_ws_scenario,
    session_cookie,
)

STAMPED = (
    '<!doctype html><html><head><meta name="otodock-build" content="{id}">'
    '<script type="module" src="/assets/index-AAAAbbbb.js"></script></head></html>'
)


def test_server_info_is_last_and_pong_carries_the_build(temp_db, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(STAMPED.format(id="cafe0123beef4567"), encoding="utf-8")
    monkeypatch.setattr(config, "DASHBOARD_DIST", tmp_path)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await ws.expect({"type": "notification_count", "count": 0})
            await ws.expect({"type": "satellite_update_sync", "inflight": []})
            await ws.expect({"type": "chat_status_snapshot", "chat_ids": []})
            # Last on purpose: a stale page closes the socket on receipt.
            await ws.expect({"type": "server_info", "build_id": "cafe0123beef4567", "version": ANY})
            ws.client_send({"type": "ping"})
            await ws.expect({"type": "pong", "build_id": "cafe0123beef4567"})
            ws.client_send({"type": "close"})
        ws.no_more_frames()
    run_ws_scenario(scenario)


def test_no_dist_means_an_empty_build_id(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_DIST", tmp_path / "missing")

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await ws.expect({"type": "notification_count", "count": 0})
            await ws.expect({"type": "satellite_update_sync", "inflight": []})
            await ws.expect({"type": "chat_status_snapshot", "chat_ids": []})
            await ws.expect({"type": "server_info", "build_id": "", "version": ANY})
            ws.client_send({"type": "close"})
    run_ws_scenario(scenario)
