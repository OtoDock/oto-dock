"""The holder of an agent session token, answered once a minute.

A session token names the person it was minted for (``user_sub``); that
person may be gone, or their credentials changed, since the mint
(``auth.providers.session_token_holder_ok``). The session routes, the hook
routes and the satellite tunnel judge every token on it, so the answer is
cached here per (person, issue time): a pass stands for ``HOLDER_TTL_S``, a
refusal for the token's life (the same token never becomes current again),
and a full table drops expired answers, then passes, before it is cleared.
A token with no person is not judged. ``forget`` drops a person's answers
when their epoch moves, so a bump refuses their running sessions' tokens at
once instead of within the minute.
"""

from __future__ import annotations

import time

HOLDER_TTL_S = 60.0
ANSWERS_MAX = 4096
_answers: dict[tuple[str, int], tuple[bool, float]] = {}


def _key(payload: dict) -> tuple[str, int] | None:
    sub = payload.get("user_sub") or ""
    if not sub:
        return None
    iat = payload.get("iat")
    return (sub, iat if isinstance(iat, int) else 0)


def _remember(key: tuple[str, int], ok: bool, payload: dict, now: float) -> None:
    if len(_answers) >= ANSWERS_MAX:
        for k in [k for k, (good, until) in _answers.items() if good or until < now]:
            del _answers[k]
        if len(_answers) >= ANSWERS_MAX:
            _answers.clear()
    if ok:
        _answers[key] = (True, now + HOLDER_TTL_S)
        return
    exp = payload.get("exp")
    left = (exp - time.time()) if isinstance(exp, (int, float)) else 0.0
    _answers[key] = (False, now + max(HOLDER_TTL_S, left))


async def holder_ok(payload: dict) -> bool:
    """True while the token's person exists and the token is current; the
    store is read on the fast lane once per person and issue time per TTL."""
    key = _key(payload)
    if key is None:
        return True
    now = time.monotonic()
    hit = _answers.get(key)
    if hit is not None and hit[1] >= now:
        return hit[0]
    from auth.providers import session_token_holder_ok
    from storage.pg import run_db_fast
    ok = bool(await run_db_fast(session_token_holder_ok, payload))
    _remember(key, ok, payload, time.monotonic())
    return ok


def forget(sub: str) -> None:
    """Drop every answer of ``sub``: their next token use reads the store."""
    for key in [k for k in _answers if k[0] == sub]:
        _answers.pop(key, None)


def reset_for_tests() -> None:
    _answers.clear()
