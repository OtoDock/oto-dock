"""The relay push path is best-effort: every failure it can meet returns
False and is logged, never raised into the caller's gather."""

import psycopg
import pytest

import services.notifications.push_sender as ps
from services.billing import relay_client


@pytest.mark.asyncio
async def test_a_store_error_in_the_relay_identity_is_logged(monkeypatch, caplog):
    # The relay identity reads the install id and the license key from the
    # store synchronously inside push_send; a database blip surfaces there.
    async def failing_push_send(**kwargs):
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(relay_client, "push_send", failing_push_send)
    with caplog.at_level("WARNING"):
        ok = await ps._send_fcm_relay("android", "tok", {"title": "t"})
    assert ok is False
    assert "Relay push failed" in caplog.text


def _relay_configured(monkeypatch):
    import config
    monkeypatch.setattr(config, "OTODOCK_RELAY_BASE", "https://relay.test")
    posts: list[dict] = []

    async def fake_post(path, payload):
        posts.append(payload)
        return {}

    monkeypatch.setattr(relay_client, "_relay_post", fake_post)
    relay_client.reset_identity_cache()
    return posts


@pytest.mark.asyncio
async def test_a_fan_out_reads_the_relay_identity_once_and_off_the_loop(temp_db, loop_db_guard, monkeypatch):
    """Five pushes that miss the cache together share one ``run_db`` read of
    the licence key and the install id; nothing reads the store on the loop,
    and the push sender's verdict is the same cache."""
    import asyncio
    posts = _relay_configured(monkeypatch)
    reads = {"n": 0}
    real = relay_client._read_identity

    def counted():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(relay_client, "_read_identity", counted)
    with loop_db_guard.active():
        await asyncio.gather(*(
            relay_client.push_send(platform="android", device_token=f"t{i}", payload={})
            for i in range(5)
        ))
        assert await ps._relay_available() is True
    assert reads["n"] == 1 and len(posts) == 5
    assert all(p["install_id"] and "license" in p and p["device_token"] == f"t{i}"
               for i, p in enumerate(posts))


@pytest.mark.asyncio
async def test_a_licence_write_resets_the_identity_the_pushes_send(temp_db, monkeypatch):
    """``set_license_key`` (the admin's paste or clear, the worker's
    adoption) resets the cache, so the next push carries the new key at
    once rather than within the minute; an unavailable relay is refused
    from the cache too."""
    import auth.license as L
    posts = _relay_configured(monkeypatch)
    await relay_client.push_send(platform="android", device_token="t", payload={})
    L.set_license_key("key-two")
    await relay_client.push_send(platform="android", device_token="t", payload={})
    assert [p["license"] for p in posts] == ["", "key-two"]
    monkeypatch.setattr(relay_client, "is_available", lambda: False)
    ps.reset_relay_verdict()
    with pytest.raises(relay_client.RelayNotConfigured):
        await relay_client.push_send(platform="android", device_token="t", payload={})
    assert await ps._relay_available() is False and len(posts) == 2


@pytest.mark.asyncio
async def test_a_reset_during_a_refresh_starts_a_fresh_one(temp_db, monkeypatch):
    """A licence write while a refresh is in flight: the caller that arrives
    after it does not join the stale refresh, the stale one stores nothing,
    and the cache ends with the value read after the write."""
    import asyncio
    import threading
    gate = threading.Event()
    reads: list[int] = []

    def slow_read():
        n = len(reads) + 1
        reads.append(n)
        if n == 1:
            gate.wait(5)
        return True, {"install_id": "i", "license": f"k{n}"}

    monkeypatch.setattr(relay_client, "_read_identity", slow_read)
    relay_client.reset_identity_cache()
    first = asyncio.ensure_future(relay_client.relay_identity())
    await asyncio.sleep(0.05)
    relay_client.reset_identity_cache()
    second = asyncio.ensure_future(relay_client.relay_identity())
    await asyncio.sleep(0.05)
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert a[1]["license"] == "k1" and b[1]["license"] == "k2"
    assert relay_client._identity.value[1]["license"] == "k2" and reads == [1, 2]
    assert (await relay_client.relay_identity())[1]["license"] == "k2" and reads == [1, 2]
