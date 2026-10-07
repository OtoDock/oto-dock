"""The binding writer (services/engines/subscription_pool): every write of
the persisted mirror and the seat counters rides one ordered worker, so a
bind's row lands before its release's delete (no orphan row from a quick
bind and release), a release leaves the loop, a reconcile sees the queued
decrements, and the shutdown flush turns later writes inline.
"""

import asyncio
import threading
import time

import pytest

from services.engines import subscription_pool as pool
from storage.billing import subscription_store


@pytest.fixture(autouse=True)
def _fresh_writer():
    pool._reopen_binding_writes()
    yield
    pool.flush_binding_writes()
    pool._reopen_binding_writes()


def _sub(layer: str = "claude-code-cli") -> str:
    row = subscription_store.add_subscription(
        layer=layer, provider="anthropic", auth_type="api_key", owner_sub="",
        use_personal=False, contribute_platform=True, label="t",
        credential_data={"api_key": "k"},
    )
    return row["id"]


def test_a_quick_bind_and_release_leaves_no_row_and_the_seat_count_whole(temp_db):
    sub = _sub()
    subscription_store.increment_active_sessions(sub)
    fut = pool.bind_session("sess-w1", sub, layer="claude-code-cli", user_sub="")
    pool.release_subscription("sess-w1")       # queued behind the bind's upsert
    fut.result(5)
    pool.flush_binding_writes()
    assert subscription_store.get_session_binding("sess-w1") is None
    assert subscription_store.get_subscription(sub)["active_sessions"] == 0
    assert not pool.session_bound("sess-w1")


@pytest.mark.asyncio
async def test_a_release_on_the_loop_writes_nothing_on_the_loop(temp_db, monkeypatch):
    sub = _sub()
    subscription_store.increment_active_sessions(sub)
    await asyncio.wrap_future(pool.bind_session("sess-w2", sub, layer="claude-code-cli", user_sub=""))
    real = subscription_store.decrement_active_sessions

    def guarded(sub_id):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return real(sub_id)
        raise AssertionError("decrement_active_sessions ran on the event loop")

    monkeypatch.setattr(subscription_store, "decrement_active_sessions", guarded)
    pool.release_subscription("sess-w2")
    await asyncio.to_thread(pool.flush_binding_writes)
    assert subscription_store.get_subscription(sub)["active_sessions"] == 0


def test_a_reconcile_sees_the_queued_releases_first(temp_db):
    sub = _sub()
    for i in range(3):
        subscription_store.increment_active_sessions(sub)
        pool.bind_session(f"sess-w3-{i}", sub, layer="claude-code-cli", user_sub="")
    pool.release_subscription("sess-w3-0")
    pool.release_subscription("sess-w3-1")
    # Without the flush the stored count reads 3 and the reconcile would
    # lower it to the live count the queued decrements then take below.
    stored, live = pool.reconcile_active_sessions(sub)
    assert stored == 1 and live == 1
    assert subscription_store.get_subscription(sub)["active_sessions"] == 1


def test_after_the_shutdown_flush_a_write_runs_inline(temp_db):
    sub = _sub()
    subscription_store.increment_active_sessions(sub)
    pool.bind_session("sess-w4", sub, layer="claude-code-cli", user_sub="")
    pool.close_binding_writes()
    assert subscription_store.get_session_binding("sess-w4") is not None
    pool.release_subscription("sess-w4")        # inline now: visible at once
    assert subscription_store.get_session_binding("sess-w4") is None
    assert subscription_store.get_subscription(sub)["active_sessions"] == 0


@pytest.mark.asyncio
async def test_the_shutdown_drain_is_bounded_and_off_the_loop_while_a_write_is_wedged(
        temp_db, monkeypatch):
    """The lifespan's shutdown drains the writer from the loop
    (``asyncio.to_thread(close_binding_writes)``): a write wedged on a
    stalled database must not hold the shutdown past the bound, the loop
    keeps running meanwhile, and what is still queued behind the wedge is
    dropped (the next boot's counter reset repairs it)."""
    sub = _sub()
    for _ in range(3):
        subscription_store.increment_active_sessions(sub)
    wedge = threading.Event()
    real = subscription_store.decrement_active_sessions

    def stalled(sub_id):
        wedge.wait(10)                     # a database stall
        return real(sub_id)

    monkeypatch.setattr(subscription_store, "decrement_active_sessions", stalled)
    monkeypatch.setattr(pool, "_BINDING_DRAIN_TIMEOUT_S", 0.3, raising=False)
    pool.release_unbound_seat(sub)          # the wedged write
    pool.release_unbound_seat(sub)          # queued behind it
    writer = pool._binding_writer
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    try:
        drained = await asyncio.wait_for(asyncio.to_thread(pool.close_binding_writes), 3)
        elapsed = time.monotonic() - started
    finally:
        task.cancel()
        wedge.set()
        await asyncio.to_thread(writer.shutdown, True)    # let the wedged write finish
    assert drained is False
    assert elapsed < 2
    assert ticks >= 5                                      # the loop was never held
    assert pool._binding_writes_closed
    # The wedged decrement landed once released; the one queued behind it was dropped.
    assert subscription_store.get_subscription(sub)["active_sessions"] == 2


def test_a_failed_write_is_logged_and_the_in_memory_binding_stands(temp_db, monkeypatch, caplog):
    sub = _sub()
    monkeypatch.setattr(subscription_store, "upsert_session_binding",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    with caplog.at_level("ERROR"):
        fut = pool.bind_session("sess-w5", sub, layer="claude-code-cli", user_sub="")
        assert fut.result(5) is None
    assert any("persisting a session binding failed" in r.getMessage() for r in caplog.records)
    assert pool.session_bound("sess-w5")


def test_a_failed_upsert_still_releases_the_replaced_seat(temp_db, monkeypatch, caplog):
    sub = _sub()
    subscription_store.increment_active_sessions(sub)
    pool.bind_session("sess-w6", sub, layer="claude-code-cli", user_sub="").result(5)
    subscription_store.increment_active_sessions(sub)        # the re-bind's fresh seat
    monkeypatch.setattr(subscription_store, "upsert_session_binding",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    with caplog.at_level("ERROR"):
        pool.bind_session("sess-w6", sub, layer="claude-code-cli", user_sub="").result(5)
    pool.flush_binding_writes()
    # The replaced seat's decrement ran although the row write failed.
    assert subscription_store.get_subscription(sub)["active_sessions"] == 1
    assert any("persisting a session binding failed" in r.getMessage() for r in caplog.records)
