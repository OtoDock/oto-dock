"""A judged entry at the relay (``core/credentials/exit_relay.py``): the
backend is asked about each request that carries the entry's placeholder,
before the value, and its word refuses the request, reads its body, replaces
it or lets it go. What the word rests on is the backend's own.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from core.credentials import exit_authority, exit_relay
from tests.credentials.exit_standin import HOST, VALUE, Backend, Vendor, caller

MIB = 1024 * 1024


def Judgement(**word):  # read at the call, so the module loads on a tree without the type
    return exit_relay.Judgement(**word)


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
    yield backend, vendor, port, bundle
    await relay.stop()
    if backend.http is not None:
        await backend.http.aclose()
    await vendor.stop()


def _auth(entry) -> dict[str, str]:
    return {"Authorization": f"Bearer {entry.placeholder}"}


@pytest.mark.asyncio
async def test_a_judged_entry_is_asked_before_the_value_and_then_goes(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True)
    async with caller(port, entry.sid, bundle) as client:
        r = await client.get(f"https://{HOST}:{vendor.port}/repos/a/b?x=1", headers=_auth(entry))
    assert r.status_code == 200
    assert backend.order == ["judge", "value"]
    assert backend.asked == [(entry.placeholder, "GET", HOST, "/repos/a/b?x=1",
                              {"content_type": "", "content_encoding": ""}, None)]
    assert vendor.seen[0].headers["authorization"] == f"Bearer {VALUE}"


@pytest.mark.asyncio
async def test_an_entry_that_is_not_judged_is_never_asked(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold()
    async with caller(port, entry.sid, bundle) as client:
        r = await client.get(f"https://{HOST}:{vendor.port}/x", headers=_auth(entry))
    assert r.status_code == 200 and backend.asked == [] and backend.order == ["value"]


@pytest.mark.asyncio
async def test_a_request_without_the_placeholder_is_not_judged(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True)
    async with caller(port, entry.sid, bundle) as client:
        r = await client.get(f"https://{HOST}:{vendor.port}/public")
    assert r.status_code == 200 and backend.asked == [] and backend.order == []
    assert "authorization" not in vendor.seen[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type, shown", [
    ("", "plain"),
    ("application/json; charset=utf-8", "json"),
    ("application/vnd.git-lfs+json", "json"),
])
async def test_a_refusal_reaches_the_caller_and_nothing_reaches_the_host(standin, content_type, shown):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True)
    sentence = "This agent may reach only these GitHub repositories: a/b. c/d is not one of them."
    backend.words = [Judgement(status=403, detail=sentence, content_type=content_type)]
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/repos/c/d/issues", headers=_auth(entry),
                              content=b'{"title": "x"}')
    assert r.status_code == 403
    if shown == "plain":
        assert r.headers["content-type"].startswith("text/plain") and r.text == sentence + "\n"
    else:
        assert r.headers["content-type"] == content_type and r.json() == {"message": sentence}
    # Refused at the head: no value was asked and nothing was dialled.
    assert backend.order == ["judge"] and vendor.seen == []


@pytest.mark.asyncio
async def test_a_judge_that_wants_the_body_is_asked_again_with_it_and_its_body_goes_on(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True)
    rebuilt = json.dumps({"query": "{viewer{login}}"}).encode()
    backend.words = [Judgement(wants_body=4096), Judgement(body=rebuilt)]
    sent = b'{"query": "{viewer{login}}", "extra": 1}'
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/graphql", content=sent,
                              headers={**_auth(entry), "Content-Type": "application/json"})
    assert r.status_code == 200
    assert [a[5] for a in backend.asked] == [None, sent]
    assert backend.asked[1][4] == {"content_type": "application/json", "content_encoding": ""}
    assert backend.order == ["judge", "judge", "value"]
    import hashlib
    assert (vendor.seen[0].size, vendor.seen[0].digest) == (len(rebuilt), hashlib.sha256(rebuilt).hexdigest())
    assert vendor.seen[0].headers["content-length"] == str(len(rebuilt))


@pytest.mark.asyncio
@pytest.mark.parametrize("second, status, received", [
    ({"status": 403, "detail": "No."}, 403, 0),
    ({}, 200, 1),
])
async def test_a_chunked_body_the_judge_asked_for_is_read_and_judged_though_the_entry_streams(
        standin, second, status, received):
    """A body the judge asked for is read whole and judged before anything
    else: an entry that streams passes a chunked body as it arrives only
    when no judge asked for it."""
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True, streams=True)
    backend.words = [Judgement(wants_body=4096), Judgement(**second)]

    async def pieces():
        yield b'{"query": '
        yield b'"mutation{x}"}'

    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/graphql", content=pieces(),
                              headers={**_auth(entry), "Content-Type": "application/json"})
    assert r.status_code == status
    assert [a[5] for a in backend.asked] == [None, b'{"query": "mutation{x}"}']
    assert len(vendor.seen) == received
    assert backend.order == (["judge", "judge", "value"] if received else ["judge", "judge"])


@pytest.mark.asyncio
async def test_a_body_past_the_judges_bound_is_refused_and_never_judged(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True, streams=True)
    backend.words = [Judgement(wants_body=1024)]
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/graphql", headers=_auth(entry),
                              content=b"q" * 4096)
    assert r.status_code == 413 and exit_relay.REFUSAL_LARGE in r.text
    assert len(backend.asked) == 1 and vendor.seen == []


@pytest.mark.asyncio
async def test_a_second_refusal_after_the_body_is_read_refuses_the_request(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True)
    backend.words = [Judgement(wants_body=4096), Judgement(status=403, detail="No.", content_type="application/json")]
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/graphql", headers=_auth(entry), content=b"{}")
    assert r.status_code == 403 and r.json() == {"message": "No."}
    assert backend.order == ["judge", "judge"] and vendor.seen == []


@pytest.mark.asyncio
async def test_a_judge_that_asks_for_the_body_twice_refuses_the_request(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True)
    backend.words = [Judgement(wants_body=4096), Judgement(wants_body=4096)]
    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/graphql", headers=_auth(entry), content=b"{}")
    assert r.status_code == 403 and exit_relay.REFUSAL_UNCHECKED in r.text
    assert vendor.seen == []


@pytest.mark.asyncio
async def test_the_answers_link_header_is_rewritten_as_the_judge_asked(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True)
    vendor.extra = [(b"link", b'<https://api.github.com/repositories/42/issues?page=2>; rel="next", '
                              b'<https://api.github.com/repositories/42/issues?page=9>; rel="last"'),
                    (b"x-other", b"https://api.github.com/repositories/42/x")]
    backend.words = [Judgement(link=(r"https://api\.github\.com/repositories/\d+/",
                                     "https://api.github.com/repos/a/b/"))]
    async with caller(port, entry.sid, bundle) as client:
        r = await client.get(f"https://{HOST}:{vendor.port}/repos/a/b/issues", headers=_auth(entry))
    assert r.headers["link"] == ('<https://api.github.com/repos/a/b/issues?page=2>; rel="next", '
                                 '<https://api.github.com/repos/a/b/issues?page=9>; rel="last"')
    # Only the Link header is rewritten.
    assert r.headers["x-other"] == "https://api.github.com/repositories/42/x"


@pytest.mark.asyncio
async def test_a_judged_entry_that_streams_passes_a_large_body_once_it_may_go(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True, streams=True)

    async def pieces():
        for _ in range(64):
            yield b"p" * (64 * 1024)

    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/a/b.git/git-receive-pack", headers=_auth(entry),
                              content=pieces())
    assert r.status_code == 200 and vendor.seen[0].size == 4 * MIB
    assert backend.order == ["judge", "value"] and backend.asked[0][5] is None


@pytest.mark.asyncio
async def test_a_refused_large_body_is_answered_before_any_of_it_is_read(standin):
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True, streams=True)
    backend.words = [Judgement(status=403, detail="This agent may only read on GitHub. git push was refused.")]

    async def pieces():
        for _ in range(512):
            yield b"p" * (64 * 1024)

    async with caller(port, entry.sid, bundle) as client:
        r = await client.post(f"https://{HOST}:{vendor.port}/a/b.git/git-receive-pack", headers=_auth(entry),
                              content=pieces())
    assert r.status_code == 403 and "git push was refused" in r.text
    assert vendor.seen == [] and backend.order == ["judge"]


@pytest.mark.asyncio
async def test_a_head_with_two_types_or_two_encodings_reaches_the_judge_as_one_joined_value(standin):
    """A judge reads a body by its type. The relay hands it every value of
    the two headers, so that a head with two is never read by its first."""
    from tests.credentials.exit_standin import answer_of, raw_caller
    backend, vendor, port, bundle = standin
    entry = backend.hold(judged=True)
    reader, writer = await raw_caller(port, entry.sid, bundle, vendor.port)
    writer.write(f"POST /graphql HTTP/1.1\r\nHost: {HOST}:{vendor.port}\r\n"
                 f"Authorization: Bearer {entry.placeholder}\r\nContent-Type: application/json\r\n"
                 "Content-Type: text/plain\r\nContent-Encoding: identity\r\nContent-Encoding: gzip\r\n"
                 "Content-Length: 2\r\n\r\n{}".encode())
    await writer.drain()
    status, _headers, _body = await answer_of(reader)
    writer.close()
    assert status == 200
    assert backend.asked[0][4] == {"content_type": "application/json, text/plain",
                                   "content_encoding": "identity, gzip"}
