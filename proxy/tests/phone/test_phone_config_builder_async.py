"""The phone config built off the loop: the store reads on the DB lane, the
classifier's relay mint in its own thread, bounded, shared by every caller
in flight and its failure remembered."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from services.phone import phone_config


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    phone_config.forget_mint_failure()
    monkeypatch.setattr(phone_config, "assemble_phone_config",
                        lambda *, classifier=True: {"version": 1, "credentials": {
                            "groq_api_key": "", "groq_base_url": ""}})
    yield
    phone_config.forget_mint_failure()


def _relay(monkeypatch, mint):
    monkeypatch.setattr(phone_config, "_groq_source", lambda: ("relay", ""))
    monkeypatch.setattr("services.engines.subscription_pool.relay_llm_credentials", mint)


def test_ten_reconnects_in_a_second_mint_once(monkeypatch):
    calls = []

    def mint(layer, provider, user_sub):
        calls.append(threading.current_thread().name)
        time.sleep(0.2)
        return ("TKN", "https://relay/v1/relay/groq/v1")
    _relay(monkeypatch, mint)

    async def run():
        return await asyncio.gather(*(phone_config.build_phone_config() for _ in range(10)))
    configs = asyncio.run(run())

    assert len(calls) == 1 and not calls[0].startswith("MainThread")
    assert all(c["credentials"] == {"groq_api_key": "TKN", "groq_base_url": "https://relay/v1/relay/groq/v1"}
               for c in configs)


def test_a_failing_relay_leaves_the_classifier_unset_and_is_remembered(monkeypatch, caplog):
    calls = []

    def mint(layer, provider, user_sub):
        calls.append(1)
        return None                       # the relay refused (its own warning)
    _relay(monkeypatch, mint)

    async def run():
        first = await phone_config.build_phone_config()
        second = await phone_config.build_phone_config()
        return first, second
    first, second = asyncio.run(run())

    assert first["credentials"] == {"groq_api_key": "", "groq_base_url": ""}
    assert second["credentials"] == first["credentials"]
    assert len(calls) == 1                 # the failure is remembered, no second attempt
    assert "version" in first              # the daemon gets its config


def test_a_relay_that_does_not_answer_is_bounded(monkeypatch, caplog):
    monkeypatch.setattr(phone_config, "MINT_TIMEOUT_S", 0.1)
    released = threading.Event()

    def mint(layer, provider, user_sub):
        released.wait(2.0)
        return ("LATE", "u")
    _relay(monkeypatch, mint)

    async def run():
        start = time.monotonic()
        cfg = await phone_config.build_phone_config()
        return cfg, time.monotonic() - start
    cfg, elapsed = asyncio.run(run())
    released.set()

    assert elapsed < 1.0 and cfg["credentials"]["groq_api_key"] == ""
    assert any("did not answer" in r.getMessage() for r in caplog.records)


def test_a_stored_key_never_touches_the_relay(monkeypatch):
    monkeypatch.setattr(phone_config, "_groq_source", lambda: ("key", "byo-secret"))
    touched = []
    monkeypatch.setattr("services.engines.subscription_pool.relay_llm_credentials",
                        lambda *a: touched.append(1))
    cfg = asyncio.run(phone_config.build_phone_config())
    assert cfg["credentials"] == {"groq_api_key": "byo-secret", "groq_base_url": ""}
    assert touched == []


def test_a_slow_store_does_not_hold_the_loop(monkeypatch):
    def slow(*, classifier=True):
        time.sleep(0.3)
        return {"version": 2, "credentials": {"groq_api_key": "", "groq_base_url": ""}}
    monkeypatch.setattr(phone_config, "assemble_phone_config", slow)
    monkeypatch.setattr(phone_config, "_groq_source", lambda: ("", ""))

    async def run():
        ticks = []

        async def ticker():
            for _ in range(5):
                await asyncio.sleep(0.03)
                ticks.append(time.monotonic())
        cfg, _ = await asyncio.gather(phone_config.build_phone_config(), ticker())
        return cfg, ticks
    cfg, ticks = asyncio.run(run())

    assert cfg["version"] == 2
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps) < 0.15                # the ticker ran while the store blocked a thread


class _Daemon:
    def __init__(self):
        self.frames = []

    async def send_text(self, raw):
        import json
        self.frames.append(json.loads(raw))


def test_a_failed_mint_is_tried_again_for_a_connected_daemon(monkeypatch):
    """After a failure the daemon is not left without the classifier until
    the next save: one broadcast after the failure window mints again,
    a failure schedules the next try, a success ends the chain."""
    monkeypatch.setattr(phone_config, "MINT_FAILURE_TTL_S", 0.05)
    answers = [None, None, ("TKN", "https://relay/v1/relay/groq/v1")]
    calls = []

    def mint(layer, provider, user_sub):
        calls.append(1)
        return answers[min(len(calls), len(answers)) - 1]
    _relay(monkeypatch, mint)
    daemon = _Daemon()
    phone_config._management_clients.add(daemon)

    async def run():
        first = await phone_config.build_phone_config()
        await asyncio.sleep(0.4)                       # three windows: two retries, then success
        return first
    try:
        first = asyncio.run(run())
    finally:
        phone_config._management_clients.discard(daemon)

    assert first["credentials"]["groq_api_key"] == ""
    assert len(calls) == 3                             # the failure, two retries, no more
    tokens = [f["data"]["credentials"]["groq_api_key"] for f in daemon.frames]
    assert tokens == ["", "TKN"]                       # the retries' pushes, the last with the token


def test_no_retry_is_scheduled_without_a_daemon(monkeypatch):
    monkeypatch.setattr(phone_config, "MINT_FAILURE_TTL_S", 0.05)
    calls = []
    _relay(monkeypatch, lambda *a: calls.append(1))
    phone_config._management_clients.clear()

    async def run():
        await phone_config.build_phone_config()
        await asyncio.sleep(0.2)
    asyncio.run(run())
    assert len(calls) == 1 and phone_config._mint_retry is None


def test_pushes_are_serialized_so_an_older_assembly_never_lands_last(monkeypatch):
    """A daemon's connect-time push whose assembly waits on the mint and an
    admin's save broadcast in that window: the save's frame reaches the
    daemon after the connect push, never before it."""
    from ws import phone_management

    versions = iter([1, 2])
    gate = asyncio.Event()

    async def slow_then_fast():
        v = next(versions)
        if v == 1:
            await gate.wait()
        return {"version": v, "credentials": {"groq_api_key": "", "groq_base_url": ""}}
    monkeypatch.setattr(phone_management, "build_phone_config", slow_then_fast)
    monkeypatch.setattr(phone_config, "build_phone_config", slow_then_fast)
    monkeypatch.setattr(phone_management.config, "is_master_key", lambda bearer: bearer == "key")

    class Socket(_Daemon):
        headers = {"authorization": "Bearer key"}

        async def accept(self):
            pass

        async def send_json(self, data):
            self.frames.append(data)

        async def receive_text(self):
            await asyncio.sleep(0.2)
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect(1005)

        async def close(self, code=1000, reason=""):
            pass

    socket = Socket()

    async def run():
        handler = asyncio.create_task(phone_management.ws_phone_management_handler(socket, key=""))
        await asyncio.sleep(0.02)                      # the connect push is assembling
        save = asyncio.create_task(phone_config.notify_phone_config_changed())
        await asyncio.sleep(0.02)
        gate.set()                                     # the slow assembly finishes
        await save
        await handler
    asyncio.run(run())

    assert [f["type"] for f in socket.frames] == ["config_full", "config_update"]
    assert [f["data"]["version"] for f in socket.frames] == [1, 2]
