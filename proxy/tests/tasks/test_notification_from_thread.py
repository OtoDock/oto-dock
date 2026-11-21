"""``fire_from_thread``: a notice found on a worker thread (a store write
that changed what a pick gives) is delivered through ``fire_notification``
on the loop the manager started on."""

from __future__ import annotations

import asyncio
import threading

import pytest

from services.notifications import notification_manager as nm


@pytest.fixture
def fired(monkeypatch):
    seen: list[dict] = []

    async def fake_fire(**kw):
        seen.append(kw)
        return []
    monkeypatch.setattr(nm, "fire_notification", fake_fire)
    monkeypatch.setattr(nm, "_loop", None)
    return seen


@pytest.mark.asyncio
async def test_from_a_worker_thread_it_lands_on_the_managers_loop(fired):
    nm._loop = asyncio.get_running_loop()
    done = threading.Event()

    def worker():
        nm.fire_from_thread(title="T", body="B", scope="agent", target="a")
        done.set()
    await asyncio.to_thread(worker)
    assert done.is_set()
    for _ in range(50):
        if fired:
            break
        await asyncio.sleep(0.01)
    assert fired == [{"title": "T", "body": "B", "scope": "agent", "target": "a"}]


@pytest.mark.asyncio
async def test_on_the_loop_itself_it_runs_as_a_task(fired):
    nm.fire_from_thread(title="T", body="B")
    await asyncio.sleep(0)
    assert fired == [{"title": "T", "body": "B"}]


def test_with_no_loop_the_notice_is_dropped_with_a_warning(fired, caplog):
    with caplog.at_level("WARNING"):
        nm.fire_from_thread(title="T", body="B")
    assert fired == [] and "T" in caplog.text


@pytest.mark.asyncio
async def test_a_notice_that_fails_on_the_loop_is_logged(monkeypatch, caplog):
    async def failing(**kw):
        raise RuntimeError("the inbox is gone")
    monkeypatch.setattr(nm, "fire_notification", failing)
    nm._loop = asyncio.get_running_loop()
    with caplog.at_level("ERROR"):
        await asyncio.to_thread(nm.fire_from_thread, title="Lost", body="B")
        for _ in range(50):
            if "Lost" in caplog.text:
                break
            await asyncio.sleep(0.01)
    assert "Lost" in caplog.text and "the inbox is gone" in caplog.text
