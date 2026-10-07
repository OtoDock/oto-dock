"""Checks 3, 4 and 8: what an unauthenticated client can make the loop do
at the door. Oversized bodies and an over-long password are refused at once,
without reading the body or hashing; 2,000 sockets that never send a byte
leave ``/health`` answering and are closed by the header deadline."""

import asyncio
import contextlib
import json
import os
import resource
import time

import pytest

pytestmark = [pytest.mark.loadtest, pytest.mark.timeout(300, method="thread")]


def _spec(tmp_path, name: str, **spec) -> str:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(spec))
    return str(path)


def test_an_oversized_body_is_refused_at_once(tmp_path):
    from tests.loadtest import _harness as h

    async def main():
        async with h.production_loop(tmp_path):
            async with h.serve_app(witness={"/v1/triggers"}) as (host, port, router):
                spec = _spec(tmp_path, "bodies", host=host, port=port, path="/v1/triggers", tries=20)
                client = await h.spawn(h.CLIENT, "bodies", spec)
                try:
                    await h.read_line(client, 60)
                    async with h.Window() as window:
                        await h.tell(client, "go")
                        declared = await h.read_line(client, 120)
                        chunked = await h.read_line(client, 120)
                finally:
                    await h.end(client)
                return declared, chunked, list(router.requests), window

    declared, chunked, requests, window = h.run_loop(main, 240)
    served = [r for r in requests if r["path"] == "/v1/triggers"]
    h.record("bodies", declared=declared, chunked=chunked,
             server={"statuses": sorted({str(r["status"]) for r in served}),
                     "chunked_body_bytes": served[-1]["body_bytes"] if served else None},
             **window.summary())

    window.ticker.assert_covered()
    assert len(served) == 22, served
    assert {r["status"] for r in served} == {413}, served
    assert declared["statuses"] == ["413"], declared
    assert declared["bodies"] == ['{"detail":"Request body too large"}'], declared
    assert declared["connection"] == ["close"], declared
    assert declared["timing"]["n"] == 20, declared
    assert declared["timing"]["p50_ms"] < h.REFUSAL_MEDIAN_S * 1000, declared
    assert served[-1]["body_bytes"] < 1024 * 1024, served[-1]
    assert chunked["closed"], chunked


def test_an_over_long_password_is_refused_before_any_hash(tmp_path, monkeypatch):
    import jwt

    import config
    from auth import password, rate_limiter
    from tests.loadtest import _harness as h

    def _never(*_a, **_k):
        raise AssertionError("zxcvbn ran on a password over the length limit")

    monkeypatch.setattr(password, "zxcvbn", _never)
    for key in [k for k in rate_limiter._attempts if k[0] == "reset"]:
        rate_limiter._attempts.pop(key, None)
    now = int(time.time())
    token = jwt.encode({"sub": "user-admin", "purpose": "password_reset", "iat": now, "exp": now + 900},
                       config.JWT_SECRET, algorithm="HS256")

    async def main():
        async with h.production_loop(tmp_path):
            async with h.serve_app(witness={"/auth/reset-password"}) as (host, port, router):
                spec = _spec(tmp_path, "password", host=host, port=port, token=token, tries=8)
                client = await h.spawn(h.CLIENT, "password", spec)
                try:
                    await h.read_line(client, 60)
                    async with h.Window() as window:
                        await h.tell(client, "go")
                        result = await h.read_line(client, 120)
                finally:
                    await h.end(client)
                return result, list(router.requests), window

    result, requests, window = h.run_loop(main, 200)
    h.record("password", result=result,
             server={"statuses": sorted({str(r["status"]) for r in requests})}, **window.summary())

    assert len(requests) == 9 and {r["status"] for r in requests} == {400}, requests
    assert result["details"] == [password._TOO_LONG], result
    assert result["timing"]["p50_ms"] < h.REFUSAL_MEDIAN_S * 1000, result


def test_two_thousand_idle_sockets_leave_the_proxy_answering(tmp_path):
    import startup
    from tests.loadtest import _harness as h

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    # The shell this runs from may grant a million descriptors already; the
    # check is that the boot raise gets the proxy from the common 1024.
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
    raised = startup.raise_nofile_limit()
    assert raised is not None and raised >= 8192, (raised, hard)

    def fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    def held(port: int) -> int:
        """Established connections on the server's side of ``port``: the
        sockets it accepted and still holds."""
        n = 0
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            with contextlib.suppress(OSError), open(table) as fh:
                for line in fh.readlines()[1:]:
                    local, _remote, state = line.split()[1:4]
                    if state == "01" and int(local.rsplit(":", 1)[1], 16) == port:
                        n += 1
        return n

    async def main():
        async with h.production_loop(tmp_path):
            async with h.serve_app() as (host, port, _router):
                spec = _spec(tmp_path, "idle", host=host, port=port, count=2000, wait_s=25)
                client = await h.spawn(h.CLIENT, "idle", spec)
                try:
                    await h.read_line(client, 60)
                    async with h.Window() as window:
                        base = await asyncio.to_thread(fds)
                        await h.tell(client, "go")
                        opened = await h.read_line(client, 60)
                        await asyncio.sleep(1.0)
                        before = await asyncio.to_thread(lambda: (fds(), held(port)))
                        await h.tell(client, "probe")
                        health = await h.read_line(client, 60)
                        after = await asyncio.to_thread(lambda: (fds(), held(port)))
                        t_wait = time.monotonic()
                        await h.tell(client, "wait")
                        closed = await h.read_line(client, 60)
                        final = await asyncio.to_thread(fds)
                finally:
                    await h.end(client)
                return base, opened, before, health, after, t_wait, closed, final, window

    base, opened, before, health, after, t_wait, closed, final, window = h.run_loop(main, 240)
    expiries = window.ticker.stats(t_wait, None)
    h.record("idle-sockets", raised_nofile=raised,
             fds={"base": base, "before_health": before[0], "after_health": after[0], "final": final},
             server_connections={"before_health": before[1], "after_health": after[1]},
             opened=opened, health=health, closed=closed, deadline_expiries_loop=expiries,
             **window.summary())

    window.ticker.assert_covered()
    assert opened["opened"] == 2000, opened
    assert before[1] >= 2000 and after[1] >= 2000, (before, after)
    assert before[0] - base >= 1990 and after[0] - base >= 1990, (base, before, after)
    assert [p["status"] for p in health["health"]] == [200, 200, 200], health
    assert closed["closed_by_server"] == 2000 and closed["still_open"] == 0, closed
    assert 14.5 <= closed["closed_after_min_s"] and closed["closed_after_max_s"] <= 20, closed
    assert [p["status"] for p in closed["health"]] == [200, 200, 200], closed
    assert final - base < 100, (base, final)
    assert expiries["max_ms"] < h.DEADLINE_BURST_MAX_S * 1000, expiries
