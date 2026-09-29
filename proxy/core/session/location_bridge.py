"""Browser-geolocation request/response bridge.

Mirrors the permission bridge: an MCP/hook calls ``wait_for_location()`` and
blocks on a per-request ``asyncio.Event``; the dashboard WS handler calls
``resolve_location()`` with the geolocation payload, which stores the result and
sets the event. Split out of session_state.py; session_state re-exports both
functions so existing imports are unchanged.
"""

import asyncio


# Location bridge state (mirrors permission bridge for MCP→WS→dashboard→WS→MCP flow)
_location_events: dict[str, asyncio.Event] = {}  # request_id -> event
_location_results: dict[str, dict] = {}  # request_id -> {lat, lng, accuracy, ...} or {error}
_location_sessions: dict[str, str] = {}  # request_id -> the session the request went to


async def wait_for_location(request_id: str, timeout: float = 30.0, *,
                            session_id: str = "") -> dict:
    """Block until dashboard sends location. Returns result dict. With
    ``session_id`` only a connection driving that session may answer."""
    event = asyncio.Event()
    _location_events[request_id] = event
    if session_id:
        _location_sessions[request_id] = session_id
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return _location_results.pop(request_id, {"error": "No result"})
    except asyncio.TimeoutError:
        return {"error": "Location request timed out"}
    finally:
        _location_events.pop(request_id, None)
        _location_sessions.pop(request_id, None)


def resolve_location(request_id: str, result: dict, *, session_id: str | None = None) -> bool:
    """Resolve a pending location request. Called from WS handler; an answer
    from a session other than the one the request went to is dropped."""
    event = _location_events.get(request_id)
    if not event:
        return False
    bound = _location_sessions.get(request_id)
    if bound and session_id is not None and session_id != bound:
        return False
    _location_results[request_id] = result
    event.set()
    return True

