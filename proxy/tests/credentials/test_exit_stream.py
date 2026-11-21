"""A body of any size through the relay (``core/credentials/exit_relay.py``):
an entry that streams passes a large body as it arrives, searched as it
passes; every other request keeps the cap; what is buffered is counted; a
refused caller reads its answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tracemalloc

import h11
import httpx
import pytest
import pytest_asyncio

from core.credentials import exit_authority, exit_relay
from tests._timing import bound
from tests.credentials.exit_standin import (
    HOST,
    VALUE,
    Backend,
    Vendor,
    answer_of,
    caller,
    raw_caller,
)

MIB = 1024 * 1024
PIECE = bytes(range(256)) * 256


@pytest_asyncio.fixture
async def standin(tmp_path):
    authority = exit_authority.ExitAuthority(tmp_path / "authority")
    bundle = tmp_path / "bundle.pem"
    authority.write_bundle(bundle)
    vendor = Vendor(authority)
    await vendor.start()
    backend = Backend(authority, vendor)
    relay = exit_relay.Relay(backend)
    port = await relay.start()
    yield backend, vendor, relay, port, bundle
    await relay.stop()
    if backend.http is not None:
        await backend.http.aclose()
    await vendor.stop()


async def _pieces(total: int, digest=None):
    sent = 0
    while sent < total:
        piece = PIECE[:min(len(PIECE), total - sent)]
        if digest is not None:
            digest.update(piece)
        sent += len(piece)
        yield piece


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", ["length", "chunked"])
async def test_a_large_body_of_an_entry_that_streams_arrives_whole(standin, framing):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    total, digest = 40 * MIB, hashlib.sha256()
    headers = {"Authorization": f"Bearer {entry.placeholder}"}
    if framing == "length":
        headers["Content-Length"] = str(total)
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/upload", headers=headers,
                              content=_pieces(total, digest))
    assert r.status_code == 200
    seen = vendor.seen[-1]
    assert (seen.size, seen.digest, seen.complete) == (total, digest.hexdigest(), True)
    assert seen.headers["authorization"] == f"Bearer {VALUE}"
    # The caller's own framing goes on: a length as a length, chunks as chunks.
    if framing == "length":
        assert seen.headers.get("content-length") == str(total) and "transfer-encoding" not in seen.headers
    else:
        assert seen.headers.get("transfer-encoding") == "chunked" and "content-length" not in seen.headers


@pytest.mark.asyncio
async def test_the_same_body_of_an_entry_that_does_not_stream_is_refused_and_the_caller_reads_why(standin):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold()
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/upload",
                              headers={"Authorization": f"Bearer {entry.placeholder}"},
                              content=_pieces(40 * MIB))
    assert r.status_code == 413 and exit_relay.REFUSAL_LARGE in r.text
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_a_length_past_the_cap_is_refused_before_any_byte_is_read(standin):
    backend, vendor, relay, port, bundle = standin
    entry = backend.hold()
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"POST /upload HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                 f"Authorization: Bearer {entry.placeholder}\r\n"
                 f"Content-Length: {exit_relay.MAX_BODY_BYTES + 1}\r\nExpect: 100-continue\r\n\r\n".encode())
    await writer.drain()
    status, _headers, body = await asyncio.wait_for(answer_of(reader), bound(5))
    assert status == 413 and exit_relay.REFUSAL_LARGE in body.decode()
    assert relay.held_all == 0 and vendor.seen == []
    writer.close()


def _chunked(body: bytes, sizes) -> bytes:
    out, at = b"", 0
    for size in sizes:
        piece = body[at:at + size]
        if not piece:
            break
        out += f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
        at += len(piece)
    if at < len(body):
        out += f"{len(body) - at:x}\r\n".encode() + body[at:] + b"\r\n"
    return out + b"0\r\n\r\n"


def _ones():
    while True:
        yield 1


@pytest.mark.asyncio
@pytest.mark.parametrize("where, sizes", [
    ("the body's start", [70_000]),
    ("the body's last bytes", [70_000]),
    ("a piece's edge", [65_536 - 20, 70_000]),
    ("one byte a piece", _ones()),
    ("forty bytes a piece", [40] * 4_000),
])
async def test_a_placeholder_in_a_streamed_body_cuts_the_request_and_none_of_it_leaves(standin, where, sizes):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    mark = entry.placeholder.encode()
    filler = b"x" * (3_000 if where == "one byte a piece" else 100_000)
    if where == "the body's start":
        body = mark + filler
    elif where == "the body's last bytes":
        body = filler + mark
    else:
        body = filler[:65_536 - 20 if where == "a piece's edge" else 1_500] + mark + filler
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"POST /upload HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                 f"Authorization: Bearer {entry.placeholder}\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
                 + _chunked(body, sizes))
    await writer.drain()
    status, _headers, answer = await asyncio.wait_for(answer_of(reader), bound(10))
    assert status == 400 and exit_relay.REFUSAL_PLACE in answer.decode()
    writer.close()
    # The vendor never received a whole request, and of the body only the
    # filler: no byte of the placeholder left the relay.
    for _ in range(100):
        if vendor.seen and vendor.seen[-1].only in (b"", b"x"):
            break
        await asyncio.sleep(0.01)
    assert len(vendor.seen) == 1
    assert vendor.seen[0].complete is False and vendor.seen[0].only in (b"", b"x")


@pytest.mark.asyncio
async def test_a_small_body_of_an_entry_that_streams_is_searched_before_any_dial(standin):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/upload",
                              headers={"Authorization": f"Bearer {entry.placeholder}"},
                              content=b"key=" + entry.placeholder.encode())
        ok = await client.post(f"https://{HOST}:{vendor.port}/upload",
                               headers={"Authorization": f"Bearer {entry.placeholder}"}, content=b"small")
    assert r.status_code == 400 and exit_relay.REFUSAL_PLACE in r.text
    assert ok.status_code == 200
    assert [s.size for s in vendor.seen] == [5]
    assert vendor.seen[0].headers.get("content-length") == "5"


@pytest.mark.asyncio
async def test_a_request_with_no_body_of_an_entry_that_streams_is_sent_with_none(standin):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    async with caller(port, entry.sid, bundle) as client:
        r = await client.get(f"https://{HOST}:{vendor.port}/info/refs",
                             headers={"Authorization": f"Bearer {entry.placeholder}"})
    assert r.status_code == 200
    seen = vendor.seen[0]
    assert "transfer-encoding" not in seen.headers and seen.size == 0 and seen.complete


@pytest.mark.asyncio
async def test_a_body_framed_in_two_ways_is_refused(standin):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"POST /upload HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                 f"Authorization: Bearer {entry.placeholder}\r\nContent-Length: 5\r\n"
                 "Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n".encode())
    await writer.drain()
    status, _headers, body = await asyncio.wait_for(answer_of(reader), bound(5))
    assert status == 400 and exit_relay.REFUSAL_FRAMING in body.decode()
    assert vendor.seen == []
    writer.close()


@pytest.mark.asyncio
async def test_the_callers_expectation_is_answered_and_not_sent_on(standin):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    size = 2 * MIB
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"POST /upload HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                 f"Authorization: Bearer {entry.placeholder}\r\nContent-Length: {size}\r\n"
                 "Expect: 100-continue\r\n\r\n".encode())
    await writer.drain()
    assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), bound(5))).startswith(b"HTTP/1.1 100")
    writer.write(b"y" * size)
    await writer.drain()
    status, _headers, _body = await asyncio.wait_for(answer_of(reader), bound(10))
    assert status == 200
    assert vendor.seen[0].size == size and "expect" not in vendor.seen[0].headers
    writer.close()


@pytest.mark.asyncio
async def test_an_answer_before_the_bodys_end_reaches_the_caller_and_ends_the_connection(standin):
    """A host's answer that comes while the caller still sends: the forward
    left the body before its end, so the answer goes back with the
    connection's end announced and the rest of the caller's body is dropped.
    The host here is a client that answers without reading the body."""
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    heads: list[httpx.Request] = []

    async def said():
        yield b'{"message": "no"}'

    class _AnswersAtOnce(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            heads.append(request)
            return httpx.Response(403, headers={"content-type": "application/json"}, content=said())

    backend.http = httpx.AsyncClient(transport=_AnswersAtOnce())
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/upload",
                              headers={"Authorization": f"Bearer {entry.placeholder}"},
                              content=_pieces(8 * MIB))
        assert r.status_code == 403 and r.json() == {"message": "no"}
        assert r.headers.get("connection") == "close"
        # The connection ended with that answer: the next request opens another.
        again = await client.get(f"https://{HOST}:{vendor.port}/x")
    assert again.status_code == 403 and len(heads) == 2
    assert heads[0].headers["authorization"] == f"Bearer {VALUE}"


@pytest.mark.asyncio
async def test_a_host_that_drops_the_request_inside_a_streamed_body_is_an_answer_never_a_hang(standin):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    vendor.early = 403
    async with caller(port, entry.sid, bundle) as client:
        r = await asyncio.wait_for(client.post(
            f"https://{HOST}:{vendor.port}/upload", headers={"Authorization": f"Bearer {entry.placeholder}"},
            content=_pieces(64 * MIB)), bound(30))
    # The host answered and closed under a caller that still sent: its answer
    # is lost with the connection, and the caller reads the relay's own.
    assert r.status_code in (403, 502)
    assert vendor.seen[0].complete is False


@pytest.mark.asyncio
async def test_a_streamed_body_is_never_held_whole(standin):
    backend, vendor, relay, port, bundle = standin
    entry = backend.hold(streams=True)
    total = 96 * MIB
    async with caller(port, entry.sid, bundle) as client:
        tracemalloc.start()
        try:
            r = await client.post(f"https://{HOST}:{vendor.port}/upload",
                                  headers={"Authorization": f"Bearer {entry.placeholder}",
                                           "Content-Length": str(total)}, content=_pieces(total))
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert r.status_code == 200 and vendor.seen[-1].size == total
    # The caller, the relay and the vendor of this test all pass the body
    # piece by piece: the peak of them together stays far under the body.
    assert peak < 16 * MIB, f"the peak was {peak / MIB:.1f} MiB for a body of {total // MIB} MiB"
    assert relay.held_all == 0 and relay.held == {}


@pytest.mark.asyncio
async def test_buffered_bodies_count_against_the_sessions_share_and_come_back(standin, monkeypatch):
    backend, vendor, relay, port, bundle = standin
    monkeypatch.setattr(exit_relay, "SESSION_BUFFER", 300_000)
    monkeypatch.setattr(exit_relay, "BUFFER_BUDGET", 500_000)
    first, other, third = backend.hold(), backend.hold(), backend.hold()
    vendor.hold = asyncio.Event()

    async def post(entry, size):
        async with caller(port, entry.sid, bundle) as client:
            return await client.post(f"https://{HOST}:{vendor.port}/upload",
                                     headers={"Authorization": f"Bearer {entry.placeholder}"}, content=b"z" * size)

    held = asyncio.create_task(post(first, 250_000))
    for _ in range(500):
        if relay.held.get(first.sid) == 250_000:
            break
        await asyncio.sleep(0.01)
    assert relay.held == {first.sid: 250_000} and relay.held_all == 250_000
    # The same session's next body passes its share: refused, its bytes back.
    over = await post(first, 100_000)
    assert over.status_code == 503 and exit_relay.REFUSAL_BUSY in over.text
    assert relay.held == {first.sid: 250_000}
    # Another session still has its own share.
    small = asyncio.create_task(post(other, 200_000))
    for _ in range(500):
        if relay.held.get(other.sid) == 200_000:
            break
        await asyncio.sleep(0.01)
    assert relay.held_all == 450_000
    # A third session's body would pass the relay's budget.
    budget = await post(third, 100_000)
    assert budget.status_code == 503
    vendor.hold.set()
    assert (await held).status_code == 200 and (await small).status_code == 200
    assert relay.held == {} and relay.held_all == 0


@pytest.mark.asyncio
async def test_a_request_that_ends_early_gives_its_bytes_back(standin):
    backend, vendor, relay, port, bundle = standin
    entry = backend.hold()
    vendor.hold = asyncio.Event()
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"POST /upload HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                 f"Authorization: Bearer {entry.placeholder}\r\nContent-Length: 1000\r\n\r\n".encode()
                 + b"q" * 1000)
    await writer.drain()
    for _ in range(500):
        if relay.held_all == 1000:
            break
        await asyncio.sleep(0.01)
    assert relay.held_all == 1000
    for task in list(relay.tasks):
        task.cancel()
    for _ in range(500):
        if relay.held_all == 0:
            break
        await asyncio.sleep(0.01)
    assert relay.held == {} and relay.held_all == 0
    vendor.hold.set()
    writer.close()


# ---------------------------------------------------------------------------
# Where a value may go: the entry's own hosts, and an address on the host
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_placeholder_toward_a_host_its_own_entry_does_not_name_is_refused(standin):
    """A session holds two entries. The connection's host is one the first
    names, so it is ended and read. The second entry's placeholder may go
    to its own hosts alone: sent toward this host, in its own header, it
    refuses the request and no value is asked."""
    backend, vendor, _relay, port, bundle = standin
    here = backend.hold()
    elsewhere = backend.hold(sid=here.sid, hosts=("elsewhere.test",))
    async with caller(port, here.sid, bundle) as client:
        r = await client.get(f"https://{HOST}:{vendor.port}/x",
                             headers={"Authorization": f"Bearer {elsewhere.placeholder}"})
        ok = await client.get(f"https://{HOST}:{vendor.port}/x",
                              headers={"Authorization": f"Bearer {here.placeholder}"})
    assert r.status_code == 400 and exit_relay.REFUSAL_PLACE in r.text
    assert ok.status_code == 200 and len(vendor.seen) == 1 and backend.order == ["value"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["*x", "x/y", "https://elsewhere.test/x", "@elsewhere.test/x", "/a/b#/c"])
async def test_a_target_that_is_no_path_is_refused(standin, target):
    """The forward's address is built from the connection's host and the
    request's target, so a target must be a path: anything else could name
    another place. A number sign ends an address for the forward's parser,
    so what was checked and judged would not be what is sent."""
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(judged=True)
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                 f"Authorization: Bearer {entry.placeholder}\r\n\r\n".encode())
    await writer.drain()
    status, _headers, body = await asyncio.wait_for(answer_of(reader), bound(5))
    assert status == 400 and "The request names no path" in body.decode()
    assert vendor.seen == [] and backend.order == []
    writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["{placeholder}@{name}:{port}", "{name}:{port}@{placeholder}"])
async def test_a_placeholder_inside_the_host_header_is_refused(standin, host):
    """The name is read out of the ``Host`` header and the header is sent on
    as the caller wrote it, so it is searched as a body is."""
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold()
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    written = host.format(placeholder=entry.placeholder, name=HOST, port=vendor.port)
    writer.write(f"GET /x HTTP/1.1\r\nHost: {written}\r\n\r\n".encode())
    await writer.drain()
    status, _headers, body = await asyncio.wait_for(answer_of(reader), bound(5))
    assert status == 400 and vendor.seen == [] and backend.order == []
    writer.close()
    # Another session's placeholder there is text, as it is in a body.
    other = backend.hold()
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"GET /x HTTP/1.1\r\nHost: {other.placeholder}@{HOST}:{vendor.port}\r\n\r\n".encode())
    await writer.drain()
    status, _headers, _body = await asyncio.wait_for(answer_of(reader), bound(5))
    assert status == 200 and len(vendor.seen) == 1
    writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", [
    "Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
    f"Content-Length: {2 * MIB}\r\nExpect: 100-continue\r\n\r\n",
])
async def test_a_placeholder_inside_the_host_header_of_a_request_that_would_stream_is_refused(standin, framing):
    """The Host header is searched before the body's path is chosen."""
    backend, vendor, relay, port, bundle = standin
    entry = backend.hold(streams=True)
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"POST /upload HTTP/1.1\r\nHost: {entry.placeholder}@{HOST}:{vendor.port}\r\n"
                 f"Authorization: Bearer {entry.placeholder}\r\n{framing}".encode())
    await writer.drain()
    status, _headers, body = await asyncio.wait_for(answer_of(reader), bound(5))
    assert status == 400 and exit_relay.REFUSAL_PLACE in body.decode()
    assert vendor.seen == [] and relay.held_all == 0
    writer.close()


@pytest.mark.asyncio
async def test_a_large_body_with_no_placeholder_in_its_head_keeps_the_cap(standin):
    """Only a request whose head carries the placeholder of an entry that
    streams takes the larger path."""
    backend, vendor, relay, port, bundle = standin
    entry = backend.hold(streams=True)
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"POST /upload HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                 f"Content-Length: {exit_relay.MAX_BODY_BYTES + 1}\r\nExpect: 100-continue\r\n\r\n".encode())
    await writer.drain()
    status, _headers, body = await asyncio.wait_for(answer_of(reader), bound(5))
    assert status == 413 and exit_relay.REFUSAL_LARGE in body.decode()
    assert vendor.seen == [] and relay.held_all == 0
    writer.close()


# ---------------------------------------------------------------------------
# What a caller reads, and a caller that goes away
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("accept, as_json", [
    ("application/vnd.github+json", True),
    ("application/json, text/plain;q=0.5", True),
    ("Application/JSON", True),
    ("*/*", False),
    ("", False),
])
async def test_the_relays_own_refusal_is_a_json_message_for_a_caller_that_accepts_json(standin, accept, as_json):
    """A client of an API shows a JSON message and nothing of a plain text.
    The relay knows no host: it reads what the request accepts."""
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold()
    headers = {"Authorization": f"Bearer {entry.placeholder}"}
    if accept:
        headers["Accept"] = accept
    async with caller(port, entry.sid, bundle) as client:
        placed = await client.post(f"https://{HOST}:{vendor.port}/upload", headers=headers,
                                   content=b"key=" + entry.placeholder.encode())
        large = await client.post(f"https://{HOST}:{vendor.port}/upload", headers=headers,
                                  content=_pieces(exit_relay.MAX_BODY_BYTES + MIB))
    assert (placed.status_code, large.status_code) == (400, 413) and vendor.seen == []
    for answer, sentence in ((placed, exit_relay.REFUSAL_PLACE), (large, exit_relay.REFUSAL_LARGE)):
        if as_json:
            assert answer.headers["content-type"] == "application/json; charset=utf-8"
            assert answer.json() == {"message": sentence}
        else:
            assert answer.headers["content-type"].startswith("text/plain") and answer.text == sentence + "\n"


@pytest.mark.asyncio
async def test_a_host_that_is_not_there_and_one_that_ends_a_streamed_body_are_told_apart(standin):
    backend, vendor, _relay, port, bundle = standin
    entry = backend.hold(streams=True)
    vendor.early = 403
    async with caller(port, entry.sid, bundle) as client:
        cut = await asyncio.wait_for(client.post(
            f"https://{HOST}:{vendor.port}/upload", content=_pieces(64 * MIB),
            headers={"Authorization": f"Bearer {entry.placeholder}", "Accept": "application/json"}), bound(30))
        await vendor.stop()
        gone = await asyncio.wait_for(client.post(
            f"https://{HOST}:{vendor.port}/upload", content=_pieces(4 * MIB),
            headers={"Authorization": f"Bearer {entry.placeholder}"}), bound(30))
    # The host took the head and ended the connection inside the body: the
    # caller is told that, in the form it accepts.
    assert cut.status_code == 502 and cut.json() == {"message": exit_relay.REFUSAL_CUT}
    # No host at all: nothing of the body was taken.
    assert gone.status_code == 502 and gone.text == "The host did not answer\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("streams", [True, False])
async def test_a_caller_that_ends_inside_a_body_ends_quietly(standin, caplog, streams):
    """An interrupted push is an ordinary event: the request never completes
    at the host, the bytes it held are given back, and the log holds no
    failure and no trace."""
    backend, vendor, relay, port, bundle = standin
    entry = backend.hold(streams=streams)
    with caplog.at_level(logging.DEBUG):
        reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
        writer.write(f"POST /upload HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                     f"Authorization: Bearer {entry.placeholder}\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
                     + b"20000\r\n" + b"z" * 0x20000 + b"\r\n")
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()
        for _ in range(500):
            if not relay.tasks:
                break
            await asyncio.sleep(0.01)
    assert not relay.tasks and relay.held == {} and relay.held_all == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text
    assert "a connection failed" not in caplog.text
    assert all(not seen.complete for seen in vendor.seen)
    assert (len(vendor.seen) == 1) is streams


def test_the_held_back_bytes_are_one_less_than_a_placeholder():
    assert exit_relay._HELD_BACK == len(exit_relay.new_placeholder()) - 1 == 40
    assert isinstance(h11.SEND_BODY, object)
