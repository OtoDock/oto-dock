"""``POST /api/calls``: the process-wide limiter counts only bodies that
passed validation, so an invalid body cannot spend the window."""

import asyncio
from types import SimpleNamespace

from calls.http_api import OutboundCallAPI, _RateLimiter
from config_manager import ConfigManager


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _api(limit):
    api = OutboundCallAPI(SimpleNamespace(), ConfigManager())
    api._calls_limiter = _RateLimiter(limit, 60.0)
    return api


def test_invalid_bodies_do_not_consume_the_limiter():
    api = _api(1)
    for _ in range(3):
        resp = asyncio.run(api._create_call(_Req({"phone_number": "not-a-number"})))
        assert resp.status == 400
    # The first valid body is admitted (and stops at the route lookup: the
    # stub config has no routes); the second is what the window refuses.
    valid = {"phone_number": "+302101234567", "task_description": "t", "route_id": "r-1"}
    resp = asyncio.run(api._create_call(_Req(valid)))
    assert resp.status == 400 and b"route" in resp.body
    resp = asyncio.run(api._create_call(_Req(valid)))
    assert resp.status == 429
