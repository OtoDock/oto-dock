"""The door every call to a Docker tool passes: one step of the credential
gateway's forward (``mcp_gateway.prepare_forward``) for a credential of the
``container`` kind, which every Docker tool's session entry carries
(``mcp_server_resolve.gateway_entry``), with a sign-in or not. So a call from a
compute machine, a paired computer or Direct LLM reaches it with its session
verified and its tool attached.

What it does with a request:

- the handshake and the lists are answered from the tool's record, awake or
  asleep: the platform's own read of the shared container for a tool with
  no sign-in (``tool_listing`` under ``PLATFORM_PERSON``), the session's own
  account's for one with a sign-in (its scope and the account in the
  fingerprint, as the stand-in keeps a program's); with none, the door reads
  the tool once itself, with that credential, and keeps it;
- any other request is forwarded, the container woken first when it sleeps
  (one wake per tool, however many calls wait), as an event stream that
  carries a keepalive every ``KEEPALIVE_S`` from the first byte to the
  answer, so neither an engine nor a hop ends a long call;
- the engine always sees a stateless server: for a server that keeps
  sessions (the browser) the door is that server's client, holds its stream
  and answers its pings, one server session per platform session, ended at
  the platform session's close (``end_session``).

The door's use counts are the tool's use (``state``): the idle sweep
(``idle.py``) never stops a tool while one is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode

import config
from core import file_commit
from core.background import spawn
from core.credentials import mcp_gateway
from services.containers import state

logger = logging.getLogger("otodock.containers")

KEEPALIVE_S = 20.0
#: How long a call waits for its tool's start, inside the 90 s a call has.
WAKE_BUDGET_S = 60.0
#: How long a session's handshake waits for the tool's first read, under
#: the 30 s Codex gives a Docker entry to start (``mcp_session_config``).
CAPTURE_BUDGET_S = 25.0
_PROBE_EVERY_S = 0.25
_FP_MEMO_S = 30.0
#: The newest protocol version the door asks for; a server answers its own.
DOOR_PROTOCOL = "2025-11-25"
_KEEPALIVE = b": keepalive\n\n"
_SSE = "text/event-stream"
_JSON = "application/json"
_ACCEPT = f"{_JSON}, {_SSE}"
_RECORD_METHODS = frozenset({
    "initialize", "ping", "logging/setLevel", "tools/list", "prompts/list", "resources/list",
    "resources/templates/list", "server/discover",
})
_LIST_KEYS = {"tools/list": "tools", "prompts/list": "prompts", "resources/list": "resources",
              "resources/templates/list": "resourceTemplates"}
_LIST_CAPABILITY = {"tools/list": "tools", "prompts/list": "prompts", "resources/list": "resources",
                    "resources/templates/list": "resources"}
_ENGINE_INITS_MAX = 4096
#: Records held in memory, one per tool, scope and fingerprint: a record read
#: with a sign-in is one person's and can be megabytes (m365's is about 1 MB).
_RECORDS_MAX = 256
_LOST_KEEP_S = 24 * 3600

SENTENCE_STARTING = "The {tool} tool is still starting. Try again in a minute."
SENTENCE_FAILED = "The {tool} tool could not start. An admin can see why on the MCP page."
SENTENCE_IMAGE = "The {tool} tool needs its image again. An admin can press Start on the MCP page."
SENTENCE_OFF = "The {tool} tool is turned off."
SENTENCE_SILENT = "The {tool} tool did not answer. Check what it did before trying again."
SENTENCE_LOST = ("The {tool} tool was restarted, so the pages it had open are closed. "
                 "Open them again if you need them.")


@dataclass(frozen=True)
class _Access:
    """What one request is to the door, the only thing the two kinds of
    Docker tool differ by: the credential added to every request to the tool
    (none for a tool with no sign-in), and the record's scope (``None`` for
    the platform's, shared by everyone; a session's own agent and person for
    a tool with a sign-in, its account in the fingerprint)."""

    scope: tuple[str, str] | None = None
    account: str = ""
    credential: tuple[tuple[str, str], ...] = ()


_PLATFORM = _Access()


@dataclass
class _Binding:
    """The door's own session with a tool's server for one platform
    session: the server's session id ("" for a stateless server) and the
    task that holds its stream."""

    mcp_sid: str = ""
    listener: asyncio.Task | None = None
    unbinding: bool = False


@dataclass
class _Door:
    bindings: dict[tuple[str, str], _Binding] = field(default_factory=dict)
    bind_locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)
    engine_inits: dict[tuple[str, str], dict] = field(default_factory=dict)
    # Sessions whose server session ended under them (a restart of the
    # container or of the platform), with when: their next call says so. A
    # mark outlives the session's close bookkeeping, which also runs when a
    # session is taken back after a restart, and goes after ``_LOST_KEEP_S``.
    lost: dict[str, dict[str, float]] = field(default_factory=dict)
    wakes: dict[str, asyncio.Task] = field(default_factory=dict)
    captures: dict[tuple, asyncio.Task] = field(default_factory=dict)
    records: dict[tuple, dict] = field(default_factory=dict)
    fps: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)


_door = _Door()


class _SignInRefused(Exception):
    """The tool's server refused the sign-in a request carried (401 or 403):
    a refusal for the person, never a tool that sleeps."""


def _refused(access: _Access, manifest) -> str:
    """What a request reads when the tool's server refuses it (401 or 403):
    the gateway's own sentence for a sign-in it refused, the plain "did not
    answer" for a tool that has none. The one place a refusal tells the two
    kinds apart."""
    if access.credential:
        return mcp_gateway._REFUSAL_VENDOR_401
    return _sentence(SENTENCE_SILENT, manifest)


def _label(manifest) -> str:
    return str(getattr(manifest, "label", "") or getattr(manifest, "name", "") or "this")


def _sentence(template: str, manifest) -> str:
    return template.format(tool=_label(manifest))


# --- the answer a carrier sends -------------------------------------------


def _json_answer(obj) -> mcp_gateway.Answered:
    return mcp_gateway.Answered(200, {"Content-Type": _JSON}, json.dumps(obj).encode())


def _error(rid, message: str, code: int = -32001) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_result(rid, sentence: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": sentence}], "isError": True}}


def _refusal_for(method: str, rid, sentence: str) -> dict:
    """A request the door cannot carry: a call's result a person reads, any
    other request's error."""
    return _tool_result(rid, sentence) if method == "tools/call" else _error(rid, sentence)


def _event(obj) -> bytes:
    return b"event: message\ndata: " + json.dumps(obj).encode() + b"\n\n"


# --- the step --------------------------------------------------------------


async def step(session_id: str, mcp: str, cred, method: str, rest: str, query: bytes,
               inbound_headers, body: bytes, *, credential: tuple = ()) -> mcp_gateway.Answered:
    """One request of ``session_id`` to the Docker tool ``cred`` names, the
    gateway's ``mcp`` key; ``credential`` the headers its sign-in adds, none
    for a tool with no sign-in."""
    from services.mcp import mcp_registry
    manifest = mcp_registry.get_manifest(cred.manifest)
    if manifest is None:
        return _json_answer(_error(None, "This tool is not installed any more."))
    access = _access_of(session_id, mcp, credential)
    if access is None:
        return _json_answer(_error(None, "This tool's session is not running."))
    inbound_headers = list(inbound_headers)
    if method == "GET":
        # No stream on offer: the engine sees a stateless server.
        return mcp_gateway.Answered(405, {"Allow": "POST, DELETE"}, b"")
    if method == "DELETE":
        return mcp_gateway.Answered(200, {}, b"")
    try:
        msg = json.loads(body) if body else None
    except ValueError:
        msg = None
    if isinstance(msg, dict) and "method" in msg and "id" not in msg:
        return mcp_gateway.Answered(202, {}, b"")
    if isinstance(msg, dict) and "method" not in msg:
        return mcp_gateway.Answered(202, {}, b"")
    one: dict = msg if isinstance(msg, dict) else {}
    rid = one.get("id")
    rpc = str(one.get("method") or "")
    if rpc in _RECORD_METHODS:
        return await _from_record(session_id, cred, manifest, one, access)
    if rpc == "tools/call":
        try:
            listing = await _record(cred, manifest, access)
        except _SignInRefused:
            return _json_answer(_refusal_for(rpc, rid, _refused(access, manifest)))
        names = {t.get("name") for t in ((listing or {}).get("lists") or {}).get("tools/list") or []
                 if isinstance(t, dict)}
        params = one.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if listing is not None and name not in names:
            return _json_answer(_error(rid, f"Unknown tool: {name}", -32602))
    stream = _forward(session_id, cred, manifest, access, rpc, rid, query, inbound_headers, body)
    return mcp_gateway.Answered(200, {"Content-Type": _SSE, "Cache-Control": "no-cache"}, stream=stream)


def _access_of(session_id: str, mcp: str, credential: tuple) -> _Access | None:
    """The access of one request: the platform's for a tool with no sign-in,
    else the session's own scope and account (``tool_listing.scope_of``, the
    account its bundle was built on), None when the session is gone."""
    if not credential:
        return _PLATFORM
    from core.credentials import mcp_broker
    from core.session.session_state import get_session_security
    from services.mcp import tool_listing
    ctx = get_session_security(session_id)
    if ctx is None:
        return None
    bundle = mcp_broker.get(session_id, mcp)
    return _Access(scope=tool_listing.scope_of(ctx), account=str(getattr(bundle, "account", "") or ""),
                   credential=tuple(credential))


async def _from_record(session_id: str, cred, manifest, msg: dict, access: _Access) -> mcp_gateway.Answered:
    from core.stdio_path_interceptor import saved_answer
    rpc = str(msg.get("method") or "")
    if rpc == "initialize":
        _remember_init(session_id, manifest.name, msg.get("params"))
        if cred.wakes_early and state.of(manifest.name).asleep:
            # A call's tool: woken beside the engine, so a spoken turn's
            # first call finds it running.
            _start_wake(manifest)
    try:
        listing = await _record(cred, manifest, access)
    except _SignInRefused:
        return _json_answer(_error(msg.get("id"), _refused(access, manifest)))
    if listing is None:
        # No record yet: a start that failed says why, a read still under way
        # (a slow start) says the tool is starting.
        st = state.of(manifest.name)
        sentence = _sentence(_failed_sentence(st) if st.failed else SENTENCE_STARTING, manifest)
        return _json_answer(_error(msg.get("id"), sentence))
    answer = saved_answer(listing, msg, _label(manifest))
    if answer is None:
        return _json_answer(_error(msg.get("id"), "Method not found", -32601))
    return _json_answer(answer)


def _remember_init(session_id: str, name: str, params) -> None:
    inits = _door.engine_inits
    inits[(session_id, name)] = params if isinstance(params, dict) else {}
    while len(inits) > _ENGINE_INITS_MAX:
        inits.pop(next(iter(inits)))


# --- the record -----------------------------------------------------------


async def _fingerprint(manifest, access: _Access) -> str:
    """The record's fingerprint: the container as it is and, for a tool with
    a sign-in, the account (``tool_listing.container_fingerprint``)."""
    from services.mcp import tool_listing
    now = time.monotonic()
    memo = (manifest.name, access.account)
    hit = _door.fps.get(memo)
    if hit is not None and now - hit[0] < _FP_MEMO_S:
        return hit[1]
    fp = await asyncio.to_thread(tool_listing.container_fingerprint, manifest, access.account)
    _door.fps[memo] = (now, fp)
    return fp


def _saved_record(access: _Access, key: str, fp: str) -> dict | None:
    """The saved record of this access: the one place its scope decides."""
    from services.mcp import tool_listing
    if access.scope is None:
        return tool_listing.saved_platform(key, fp)
    return tool_listing.saved(access.scope, key, fp)


def _keep_record(access: _Access, manifest, key: str, fp: str, listing: dict) -> bool:
    from services.mcp import tool_listing
    from storage.mcp import mcp_store
    if access.scope is None:
        return tool_listing.keep_platform(key, fp, listing, mcp_store.get_mcp_agents(manifest.name))
    return tool_listing.keep(access.scope, key, fp, listing)


async def _record(cred, manifest, access: _Access) -> dict | None:
    """The tool's record for this access and its container as it is now:
    kept in memory, else the saved one, else read once from the container
    with this access's credential."""
    key = _key(cred, manifest)
    fp = await _fingerprint(manifest, access)
    memo = (manifest.name, access.scope, fp)
    hit = _door.records.get(memo)
    if hit is not None:
        return hit
    listing = await asyncio.to_thread(_saved_record, access, key, fp)
    if listing is None:
        task = _door.captures.get(memo)
        if task is None or task.done():
            task = spawn(_capture(cred, manifest, key, fp, access), name=f"record:{manifest.name}",
                         owner="containers")
            _door.captures[memo] = task
            task.add_done_callback(lambda done: _forget_capture(memo, done))
        try:
            got = await asyncio.wait_for(asyncio.shield(task), timeout=CAPTURE_BUDGET_S)
        except asyncio.TimeoutError:
            return None
        if isinstance(got, _SignInRefused):
            raise got
        listing = got
    if listing is not None:
        # One record per tool and scope: an older fingerprint's goes.
        for other in [m for m in _door.records if m[:2] == memo[:2] and m != memo]:
            _door.records.pop(other, None)
        _door.records[memo] = listing
        while len(_door.records) > _RECORDS_MAX:
            _door.records.pop(next(iter(_door.records)))
    return listing


def _forget_capture(memo: tuple, task: asyncio.Task) -> None:
    if _door.captures.get(memo) is task:
        _door.captures.pop(memo, None)


def _key(cred, manifest) -> str:
    return str(getattr(manifest, "server_name", "") or manifest.name)


async def _capture(cred, manifest, key: str, fp: str, access: _Access) -> dict | _SignInRefused | None:
    """The door's own handshake with the container, with the access's
    credential, its lists kept as the record of that access and its labels
    recorded (the platform's own read). A refused sign-in is returned, not
    raised: every waiter reads it."""
    with hold(manifest):
        try:
            return await _read_record(cred, manifest, key, fp, access)
        except _SignInRefused as refused:
            return refused


async def _read_record(cred, manifest, key: str, fp: str, access: _Access) -> dict | None:
    from services.mcp import mcp_permissions, tool_listing
    deadline = time.monotonic() + CAPTURE_BUDGET_S
    sentence = await _awake(manifest, deadline)
    if sentence:
        logger.info("Docker MCP %s: no record: %s", manifest.name, _sentence(sentence, manifest))
        return None
    client = mcp_gateway.client(True)
    url = _url(cred, "")
    if not await _probe(manifest, deadline):
        logger.info("Docker MCP %s: no record, its server did not answer yet", manifest.name)
        return None
    headers = {"Accept": _ACCEPT, "Content-Type": _JSON, **dict(access.credential)}
    try:
        status, rheaders, init = await _rpc(client, url, headers, {
            "jsonrpc": "2.0", "id": "otodock-record", "method": "initialize",
            "params": {"protocolVersion": DOOR_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "otodock", "version": config.PINNED_OTODOCK_VERSION or ""}}})
        result = (init or {}).get("result")
        if status in (401, 403):
            logger.info("Docker MCP %s: its server refused the sign-in (%s)", manifest.name, status)
            raise _SignInRefused(status)
        if status != 200 or not isinstance(result, dict):
            logger.warning("Docker MCP %s: its handshake answered %s", manifest.name, status)
            return None
        mcp_sid = rheaders.get("mcp-session-id", "")
        if mcp_sid:
            headers["Mcp-Session-Id"] = mcp_sid
        await _notify(client, url, headers, "notifications/initialized")
        lists: dict[str, list] = {}
        raw_caps = result.get("capabilities")
        caps: dict = raw_caps if isinstance(raw_caps, dict) else {}
        for rpc, item_key in _LIST_KEYS.items():
            if _LIST_CAPABILITY[rpc] not in caps:
                continue
            items: list = []
            cursor = None
            for page in range(50):
                params: dict = {"cursor": cursor} if cursor else {}
                _status, _h, answer = await _rpc(client, url, headers, {
                    "jsonrpc": "2.0", "id": f"otodock-list-{page}", "method": rpc, "params": params})
                got = (answer or {}).get("result") or {}
                items.extend(i for i in got.get(item_key) or [] if isinstance(i, dict))
                cursor = got.get("nextCursor")
                if not cursor:
                    break
            lists[rpc] = items
        if mcp_sid:
            with contextlib.suppress(Exception):
                await client.request("DELETE", url, headers={**dict(access.credential), "Mcp-Session-Id": mcp_sid},
                                     timeout=5.0)
    except _SignInRefused:
        raise
    except Exception as e:  # noqa: BLE001  # the record is best effort: the session goes without the tool
        logger.warning("Docker MCP %s: its record could not be read: %s: %s", manifest.name, type(e).__name__, e)
        return None
    tools = lists.get("tools/list") or []
    await asyncio.to_thread(mcp_permissions.record_tool_hints, key,
                            [(str(t.get("name") or ""), t.get("annotations")) for t in tools if t.get("name")])
    listing = {"initialize": result, "lists": lists}
    if await asyncio.to_thread(_keep_record, access, manifest, key, fp, listing):
        logger.info("Docker MCP %s: its record kept (%d tools%s)", manifest.name, len(tools),
                    "" if access.scope is None else ", one account's")
    else:
        logger.warning("Docker MCP %s: its record is held in memory only (%d tools), so it is read again "
                       "after each start of the platform", manifest.name, len(tools))
    return await asyncio.to_thread(_saved_record, access, key, fp) or tool_listing._clean(listing)


# --- the wake ---------------------------------------------------------------


def _start_wake(manifest) -> asyncio.Task:
    task = _door.wakes.get(manifest.name)
    if task is None or task.done():
        task = spawn(_run_wake(manifest), name=f"wake:{manifest.name}", owner="containers")
        _door.wakes[manifest.name] = task
    return task


def wake_in_background(manifest) -> None:
    """Start the tool now (an admin kept it running), nobody waiting."""
    _start_wake(manifest)


async def _run_wake(manifest) -> str:
    """Start the tool's container and wait for its port. "" when it
    answers, else the sentence a call reads (the admin reads the cause)."""
    from services.containers import lifecycle
    from storage.mcp import mcp_store
    st = state.of(manifest.name)
    st.waking = True
    try:
        enabled = bool((await asyncio.to_thread(mcp_store.get_mcp_state, manifest.name) or {}).get("enabled"))
        if not enabled:
            return SENTENCE_OFF
        t0 = time.monotonic()
        ok = await asyncio.to_thread(lifecycle.wake_container, manifest)
        if not ok:
            logger.warning("Docker MCP %s: its wake failed: %s", manifest.name, st.last_error[:300])
            return _failed_sentence(st)
        # Counted from compose's return: a slow compose run that worked is
        # never marked failed.
        if not await _probe(manifest, time.monotonic() + WAKE_BUDGET_S):
            state.failed_start(manifest.name, "Its server did not answer after its start.")
            return _failed_sentence(st)
        state.started(manifest.name)
        logger.info("Docker MCP %s: woke in %.1f s", manifest.name, time.monotonic() - t0)
        return ""
    finally:
        st.waking = False


def _failed_sentence(st) -> str:
    """The sentence a call reads after a failed start."""
    return SENTENCE_IMAGE if st.failed == state.NO_IMAGE else SENTENCE_FAILED


async def _probe(manifest, deadline: float) -> bool:
    """The tool answers an MCP handshake (its own session, if it opens one,
    ended at once). A port alone is not enough: a front such as the
    browser's answers before the server behind it. Docker's own health word
    is never waited for."""
    from services.mcp import mcp_registry
    entry = mcp_registry.resolve_server_config(manifest, "", mcp_config_format="json")
    url = str(entry.get("url") or "")
    client = mcp_gateway.client(True)
    while time.monotonic() < deadline:
        if await _handshake(client, url) is not None:
            return True
        await asyncio.sleep(_PROBE_EVERY_S)
    return False


async def _handshake(client, url: str) -> dict | None:
    """One ``initialize`` the door sends itself: the server's result, or None
    while it does not answer one. A session it opens is ended."""
    import httpx
    try:
        status, rheaders, answer = await _rpc(client, url, {"Accept": _ACCEPT, "Content-Type": _JSON}, {
            "jsonrpc": "2.0", "id": "otodock-probe", "method": "initialize",
            "params": {"protocolVersion": DOOR_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "otodock", "version": ""}}}, timeout=5.0)
    except (httpx.HTTPError, OSError):
        return None
    if status in (401, 403):
        # The server is up and asks for a sign-in the probe has none of.
        return {}
    result = (answer or {}).get("result")
    if status != 200 or not isinstance(result, dict):
        return None
    if rheaders.get("mcp-session-id"):
        with contextlib.suppress(Exception):
            await client.request("DELETE", url, headers={"Mcp-Session-Id": rheaders["mcp-session-id"]},
                                 timeout=5.0)
    return result


async def _awake(manifest, deadline: float) -> str:
    """"" once the tool runs (woken when the platform put it to sleep),
    else the sentence: still starting past ``deadline``, or why not."""
    from services.containers import lifecycle
    st = state.of(manifest.name)
    if not st.asleep or not lifecycle.managed(manifest):
        return ""
    task = _start_wake(manifest)
    try:
        sentence = await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        return SENTENCE_STARTING
    return sentence


async def ready(manifest, *, budget_s: float = WAKE_BUDGET_S) -> str:
    """For the platform's own dials of a tool (a render, a download, a
    builder): "" once it runs, else the sentence, filled."""
    sentence = await _awake(manifest, time.monotonic() + budget_s)
    return _sentence(sentence, manifest) if sentence else ""


@contextlib.contextmanager
def hold(manifest):
    """Use of the tool for the life of the block (a platform dial)."""
    st = state.of(manifest.name)
    st.in_flight += 1
    try:
        yield
    finally:
        st.in_flight -= 1
        st.last_use = time.monotonic()


# --- the forward -------------------------------------------------------------


def _url(cred, query: str | bytes, session_id: str = "") -> str:
    q = query.decode("latin-1") if isinstance(query, bytes) else query
    params = [(k, v) for k, v in parse_qsl(q, keep_blank_values=True) if k != "session_id"]
    if session_id:
        params.append(("session_id", session_id))
    suffix = ("?" + urlencode(params)) if params else ""
    return f"{cred.upstream}{cred.path}{suffix}"


async def _rpc(client, url: str, headers: dict, msg: dict, timeout: float = 30.0):
    """One request the door sends itself: its status, its headers (lower
    case) and the answer to ``msg``'s id, from a JSON body or an event
    stream."""
    resp = await client.post(url, headers=headers, content=json.dumps(msg).encode(), timeout=timeout)
    rheaders = {k.lower(): v for k, v in resp.headers.items()}
    answer = None
    ctype = rheaders.get("content-type", "")
    if _SSE in ctype:
        for event in _events(resp.content):
            obj = _event_json(event)
            if isinstance(obj, dict) and obj.get("id") == msg.get("id"):
                answer = obj
                break
    elif resp.content:
        with contextlib.suppress(ValueError):
            answer = json.loads(resp.content)
    return resp.status_code, rheaders, answer if isinstance(answer, dict) else None


async def _notify(client, url: str, headers: dict, method: str) -> None:
    await client.post(url, headers=headers, timeout=10.0,
                      content=json.dumps({"jsonrpc": "2.0", "method": method}).encode())


def _events(data: bytes) -> list[bytes]:
    """The complete events of an event stream's bytes."""
    data = data.replace(b"\r\n", b"\n")
    return [e for e in data.split(b"\n\n") if e.strip()]


def _event_json(event: bytes):
    lines = [ln[5:].lstrip() for ln in event.split(b"\n") if ln.startswith(b"data:")]
    if not lines:
        return None
    try:
        return json.loads(b"\n".join(lines))
    except ValueError:
        return None


def _callback_headers(cred, session_id: str, inbound_headers) -> dict[str, str]:
    """The callback token a tool that calls the proxy back carries, minted
    per request from the request's own verified session token (its person
    and its floor with it, also after a restart): never in a session's
    config, nothing when the token is not this session's."""
    if not cred.callbacks or not session_id:
        return {}
    from auth.session_token import callback_token_for, validate_callback_token
    bearers = [v for k, v in inbound_headers if k.lower() == "authorization"]
    if len(bearers) != 1:
        return {}
    scheme, _, token = bearers[0].strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return {}
    minted = callback_token_for(token.strip())
    claims = validate_callback_token(minted) if minted else None
    if not claims or claims.get("sid") != session_id:
        return {}
    return {"Authorization": f"Bearer {minted}"}


async def _bind(session_id: str, cred, manifest, client, access: _Access, inbound_headers) -> _Binding:
    """The door's own session with the tool's server for ``session_id``:
    its ``initialize`` with the engine's protocol version and no client
    capabilities (the server then asks the door for nothing but pings), and
    for a server that keeps sessions, its stream held and its pings
    answered."""
    key = (session_id, manifest.name)
    found = _door.bindings.get(key)
    if found is not None:
        return found
    lock = _door.bind_locks.setdefault(key, asyncio.Lock())
    async with lock:
        found = _door.bindings.get(key)
        if found is not None:
            return found
        params = _door.engine_inits.get(key) or {}
        url = _url(cred, "", session_id)
        headers = {"Accept": _ACCEPT, "Content-Type": _JSON,
                   **_callback_headers(cred, session_id, inbound_headers), **dict(access.credential)}
        status, rheaders, answer = await _rpc(client, url, headers, {
            "jsonrpc": "2.0", "id": "otodock-bind", "method": "initialize",
            "params": {"protocolVersion": str(params.get("protocolVersion") or DOOR_PROTOCOL),
                       "capabilities": {},
                       "clientInfo": params.get("clientInfo") or {"name": "otodock", "version": ""}}})
        if status in (401, 403):
            raise _SignInRefused(status)
        if status != 200 or answer is None or "result" not in answer:
            raise ConnectionError(f"its handshake answered {status}")
        binding = _Binding(mcp_sid=rheaders.get("mcp-session-id", ""))
        if binding.mcp_sid:
            headers["Mcp-Session-Id"] = binding.mcp_sid
        await _notify(client, url, headers, "notifications/initialized")
        _door.bindings[key] = binding
        if binding.mcp_sid:
            state.of(manifest.name).bound.add(session_id)
            binding.listener = spawn(_listen(session_id, cred, manifest, binding, client, access),
                                     name=f"stream:{manifest.name}", owner="containers")
            _save_held()
        return binding


async def _listen(session_id: str, cred, manifest, binding: _Binding, client, access: _Access) -> None:
    """The server's own stream for the door's session: a ping answered,
    any other request refused. Its end without an unbind is a lost
    session: the platform session's next call says so."""
    url = _url(cred, "", session_id)
    headers = {"Accept": _SSE, "Mcp-Session-Id": binding.mcp_sid, **dict(access.credential)}
    offered = True
    try:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 405:
                # The server offers no stream (the protocol allows it): its
                # session stays the door's, nothing to hold.
                offered = False
                return
            if resp.status_code != 200:
                return
            pending = b""
            async for chunk in resp.aiter_raw():
                pending += chunk.replace(b"\r\n", b"\n")
                *done, pending = pending.split(b"\n\n")
                for event in done:
                    obj = _event_json(event)
                    if not (isinstance(obj, dict) and "method" in obj and "id" in obj):
                        continue
                    reply = ({"jsonrpc": "2.0", "id": obj["id"], "result": {}} if obj["method"] == "ping"
                             else _error(obj["id"], "Method not found", -32601))
                    post_headers = {"Accept": _ACCEPT, "Content-Type": _JSON, **headers}
                    post_headers["Accept"] = _ACCEPT
                    await client.post(url, headers=post_headers, content=json.dumps(reply).encode(), timeout=5.0)
    except asyncio.CancelledError:
        # The platform stops (or the session ended, ``unbinding``): the file
        # of held sessions keeps it for the next start.
        binding.unbinding = True
        raise
    except Exception as e:  # noqa: BLE001  # the stream's end is the binding's end, whatever ended it
        logger.info("Docker MCP %s: the stream of session %s ended: %s", manifest.name, session_id[:8],
                    type(e).__name__)
    finally:
        if offered and not binding.unbinding:
            _forget_binding(session_id, manifest.name, lost=True)


def _forget_binding(session_id: str, name: str, *, lost: bool, save: bool = True) -> _Binding | None:
    binding = _door.bindings.pop((session_id, name), None)
    _door.bind_locks.pop((session_id, name), None)
    state.of(name).bound.discard(session_id)
    if binding is not None and binding.mcp_sid:
        if lost:
            _mark_lost(name, session_id)
        if save:
            _save_held()
    return binding


def _mark_lost(name: str, session_id: str) -> None:
    now = time.monotonic()
    marks = _door.lost.setdefault(name, {})
    for sid in [sid for sid, at in marks.items() if now - at > _LOST_KEEP_S]:
        marks.pop(sid, None)
    marks[session_id] = now


def end_session(session_id: str) -> None:
    """The platform session ended: its server sessions end with it (the
    browser's pages close). Never wakes a tool. The file of held sessions
    loses it, except while the platform stops: a stop closes every session,
    and the next start must still read who held what."""
    from core import drain
    held = False
    for key in [k for k in _door.bindings if k[0] == session_id]:
        binding = _forget_binding(session_id, key[1], lost=False, save=False)
        if binding is None:
            continue
        binding.unbinding = True
        if binding.listener is not None:
            binding.listener.cancel()
        if binding.mcp_sid:
            held = True
            spawn(_delete(session_id, key[1], binding.mcp_sid), name=f"end:{key[1]}", owner="containers")
    for key in [k for k in _door.engine_inits if k[0] == session_id]:
        _door.engine_inits.pop(key, None)
    if held and not drain.stopping():
        _save_held()


def end_orphans() -> list[str]:
    """The sessions bound to a tool's server that the platform no longer
    knows (gone without their close): let go, so they hold no tool awake."""
    from core.session.session_state import get_session_security
    gone = sorted({sid for sid, _name in _door.bindings if get_session_security(sid) is None})
    for sid in gone:
        end_session(sid)
    return gone


def schedule_end(session_id: str) -> None:
    """``end_session`` from any caller, on the running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_soon(end_session, session_id)


async def _delete(session_id: str, name: str, mcp_sid: str) -> None:
    from services.mcp import mcp_registry
    manifest = mcp_registry.get_manifest(name)
    if manifest is None:
        return
    entry = mcp_registry.resolve_server_config(manifest, "", mcp_config_format="json")
    url = str(entry.get("url") or "")
    with contextlib.suppress(Exception):
        await mcp_gateway.client(True).request(
            "DELETE", f"{url}?{urlencode({'session_id': session_id})}",
            headers={"Mcp-Session-Id": mcp_sid}, timeout=5.0)


def forget_tool(name: str) -> None:
    """The tool was put to sleep: its stateless sessions go (a stateful one
    keeps it awake, so none is left)."""
    for key in [k for k in _door.bindings if k[1] == name]:
        _forget_binding(key[0], name, lost=True)


async def _forward(session_id: str, cred, manifest, access: _Access, rpc: str, rid, query: bytes,
                   inbound_headers, body: bytes) -> AsyncIterator[bytes]:
    """The forwarded request's answer as an event stream: keepalives while
    the tool wakes and while it works, then its answer. It is the tool's use
    from its first step to its end, however it ends (a stream nobody reads
    counts for nothing)."""
    st = state.of(manifest.name)
    st.in_flight += 1
    try:
        async for chunk in _forwarded(session_id, cred, manifest, access, rpc, rid, query, inbound_headers, body):
            yield chunk
    finally:
        st.in_flight -= 1
        st.last_use = time.monotonic()


async def _forwarded(session_id: str, cred, manifest, access: _Access, rpc: str, rid, query: bytes,
                     inbound_headers, body: bytes) -> AsyncIterator[bytes]:
    import httpx
    st = state.of(manifest.name)
    deadline = time.monotonic() + WAKE_BUDGET_S
    client = mcp_gateway.client(True)
    headers = {k: v for k, v in mcp_gateway.forward_request_headers(inbound_headers).items()
               if k.lower() not in ("mcp-session-id", "accept")}
    headers["Accept"] = _ACCEPT
    headers.update(_callback_headers(cred, session_id, inbound_headers))
    headers.update(dict(access.credential))
    retried = False
    while True:
        if st.asleep:
            wake = _start_wake(manifest)
            while not wake.done():
                done, _ = await asyncio.wait({wake}, timeout=KEEPALIVE_S)
                if done:
                    break
                if time.monotonic() >= deadline:
                    yield _event(_refusal_for(rpc, rid, _sentence(SENTENCE_STARTING, manifest)))
                    return
                yield _KEEPALIVE
            sentence = wake.result()
            if sentence:
                yield _event(_refusal_for(rpc, rid, _sentence(sentence, manifest)))
                return
        try:
            binding = await _bind(session_id, cred, manifest, client, access, inbound_headers)
            send_headers = dict(headers)
            if binding.mcp_sid:
                send_headers["Mcp-Session-Id"] = binding.mcp_sid
            req = client.build_request("POST", _url(cred, query, session_id), headers=send_headers, content=body)
            send = asyncio.create_task(client.send(req, stream=True))
            try:
                while True:
                    done, _ = await asyncio.wait({send}, timeout=KEEPALIVE_S)
                    if done:
                        break
                    yield _KEEPALIVE
            except BaseException:
                # The carrier dropped the call while it waited: its request
                # ends here, never left running unread.
                _drop(send)
                raise
            upstream = send.result()
        except _SignInRefused as refused:
            logger.info("Docker MCP %s: its server refused the session's sign-in (%s)", manifest.name, refused)
            yield _event(_refusal_for(rpc, rid, _refused(access, manifest)))
            return
        except (httpx.TransportError, ConnectionError, OSError) as e:
            from services.containers import lifecycle
            # Only a request that never reached the server is tried again (a
            # refused connection, a handshake not answered yet): a call cut
            # on its way back may have run.
            delivered = isinstance(e, httpx.TransportError) and not isinstance(
                e, (httpx.ConnectError, httpx.ConnectTimeout))
            if delivered or retried or not lifecycle.managed(manifest):
                logger.warning("Docker MCP %s: no answer: %s", manifest.name, e)
                yield _event(_refusal_for(rpc, rid, _sentence(SENTENCE_SILENT, manifest)))
                return
            # Its container is not running (stopped by hand, or before this
            # start of the platform): woken once.
            retried = True
            st.asleep = True
            _forget_binding(session_id, manifest.name, lost=True)
            continue
        if upstream.status_code == 404 and not retried and _door.bindings.get((session_id, manifest.name)):
            # The server forgot the door's session (its container restarted).
            await upstream.aclose()
            retried = True
            _forget_binding(session_id, manifest.name, lost=True)
            continue
        break
    if upstream.status_code in (401, 403):
        await upstream.aclose()
        logger.info("Docker MCP %s: its server refused the session's sign-in (%s)", manifest.name,
                    upstream.status_code)
        yield _event(_refusal_for(rpc, rid, _refused(access, manifest)))
        return
    lost = session_id in _door.lost.get(manifest.name, {})
    try:
        async for chunk in _answer_stream(upstream, rpc, rid, manifest, lost):
            yield chunk
        if lost and rpc == "tools/call":
            _door.lost[manifest.name].pop(session_id, None)
            logger.info("Docker MCP %s: session %s told its pages closed", manifest.name, session_id[:8])
    finally:
        await upstream.aclose()


def _drop(send: asyncio.Future) -> None:
    """A request to the tool nobody reads any more: cancelled, or its
    answer closed when it came at that moment."""
    if not send.done():
        send.cancel()
    elif not send.cancelled() and send.exception() is None:
        spawn(send.result().aclose(), name="drop", owner="containers")


#: The keys one of which a JSON-RPC answer carries (a request carries neither).
_ANSWER_KEYS = frozenset(("result", "error"))


def _answers(obj, rid) -> bool:
    """A JSON-RPC message, or a batch, that answers the request ``rid``."""
    return any(isinstance(one, dict) and one.get("id") == rid and bool(one.keys() & _ANSWER_KEYS)
               for one in (obj if isinstance(obj, list) else [obj]))


async def _answer_stream(upstream, rpc: str, rid, manifest, lost: bool) -> AsyncIterator[bytes]:
    """The server's answer as whole events, a keepalive when it is quiet; a
    JSON answer as one event when it is a JSON-RPC message. A request that
    got no answer for its id (a stream cut midway, a body that is no JSON-RPC
    message, an empty one) reads the "did not answer" sentence: the engine
    never waits for its own limit."""
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for chunk in upstream.aiter_raw():
                await queue.put(chunk)
        except Exception as e:  # noqa: BLE001  # an ended stream is the answer's end
            logger.info("Docker MCP %s: its answer ended early: %s", manifest.name, type(e).__name__)
        finally:
            await queue.put(None)

    reader = spawn(pump(), name=f"answer:{manifest.name}", owner="containers")
    ctype = upstream.headers.get("content-type", "")
    sse = _SSE in ctype
    pending = b""
    whole = b""
    answered = False
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield _KEEPALIVE
                continue
            if chunk is None:
                break
            if not sse:
                whole += chunk
                continue
            pending += chunk.replace(b"\r\n", b"\n")
            *done, pending = pending.split(b"\n\n")
            for event in done:
                if event.strip():
                    answered = answered or _answers(_event_json(event), rid)
                    yield _with_note(event, rid, manifest, lost) + b"\n\n"
        if sse:
            if pending.strip():
                answered = answered or _answers(_event_json(pending), rid)
                yield _with_note(pending, rid, manifest, lost) + b"\n\n"
        else:
            try:
                obj = json.loads(whole) if whole else None
            except ValueError:
                obj = None
            if isinstance(obj, list) or (isinstance(obj, dict) and "jsonrpc" in obj):
                if lost and isinstance(obj, dict):
                    obj = _noted(obj, rid, manifest)
                answered = _answers(obj, rid)
                yield _event(obj)
        if rid is not None and not answered:
            logger.warning("Docker MCP %s: no answer to its %s (status %s)", manifest.name, rpc or "request",
                           upstream.status_code)
            yield _event(_refusal_for(rpc, rid, _sentence(SENTENCE_SILENT, manifest)))
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader


def _with_note(event: bytes, rid, manifest, lost: bool) -> bytes:
    if not lost:
        return event
    obj = _event_json(event)
    if not isinstance(obj, dict) or obj.get("id") != rid or "result" not in obj:
        return event
    return _event(_noted(obj, rid, manifest)).rstrip(b"\n")


def _noted(obj: dict, rid, manifest) -> dict:
    """A call's answer with the sentence that the tool's pages closed in
    front of what it says."""
    result = obj.get("result")
    if obj.get("id") != rid or not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return obj
    note = {"type": "text", "text": _sentence(SENTENCE_LOST, manifest)}
    return {**obj, "result": {**result, "content": [note, *result["content"]]}}


def _held_path():
    return config.SESSIONS_DIR / "container-sessions.json"


def _encode_held() -> str:
    return json.dumps([[sid, name] for (sid, name), b in _door.bindings.items() if b.mcp_sid])


# Encoded at each change, written off the event loop at once.
_held_writer = file_commit.StateFile("door-held", _held_path, _encode_held, fsync=False)


def _save_held() -> None:
    """The server sessions the door holds now, on disk, so that after the
    next start of the platform their sessions' next calls say the pages
    closed. Written at each change (a crash keeps it), off the event loop."""
    _held_writer.mark_dirty()


def load_held() -> int:
    """At a start: the server sessions the door held before it are lost."""
    try:
        pairs = json.loads(_held_path().read_text())
    except (OSError, ValueError):
        return 0
    count = 0
    for pair in pairs if isinstance(pairs, list) else ():
        if isinstance(pair, list) and len(pair) == 2:
            _mark_lost(str(pair[1]), str(pair[0]))
            count += 1
    if count:
        logger.info("Docker tools: %d session(s) held before this start lost their server session", count)
    return count


def reset_for_tests() -> None:
    global _door
    _door = _Door()
    state.reset_for_tests()
