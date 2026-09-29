"""App bindings (APPS.md "Bindings"): one app calling another over the org
chart. An app names the apps it uses in its signed ``bindings`` block; the
other side names what it offers in its signed ``exports`` block; an editor
of each side approved; and at call time the proxy checks the delegation
edge from the caller's agent to the target's agent (or the owner's own
membership for a personal caller), both approvals, the export's floor
against the viewer's role ON THE TARGET agent, and brokers the call — apps
never see each other's ports.

Three routes, all on the caller's launch token: ``ANY /v1/apps/{id}/
bindings/{name}/{path}`` forwards to the target's ``/api/{path}`` with a
fresh ``X-OtoDock-Caller`` claim; ``GET …/bindings/{name}/snapshot/{snap}``
serves a file the target wrote under its data directory without waking
it; ``POST /v1/apps/{id}/events`` wakes every approved subscriber whose
bindings name this app and whose handlers listen for the event.

Constraints: nothing is cached across calls but a snapshot's bytes; a
revoked edge is an absent row and bites at once; a hop count and a
deadline travel in the claim and the proxy-side bounds (in-flight calls
per caller, the per-pair bucket, the upstream timeout) hold even when an
app forwards nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import stat
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from api.apps import manifest as _mf
from api.apps.app_proxy import (
    _CORS,
    _DROP_RESPONSE,
    _read_body,
    _redirect_ok,
    _refuse,
    _row_or_404,
    _take,
    _unavailable,
    check_app_path,
    check_rate,
    forward,
    resolve_caller,
)
from services.apps import app_supervisor, app_tokens, releases
from storage import database as task_store
from storage import db_app_deliveries as deliveries
from storage.pg import run_db
from auth.providers import effective_role_of
from auth import roles

logger = logging.getLogger("claude-proxy.apps")
router = APIRouter()

BIND_RATE = 10.0
BIND_BURST = 20.0
BIND_INFLIGHT_MAX = 8
HOP_MAX = 3
CALL_DEADLINE_S = 30.0
SNAPSHOT_MAX_BYTES = 1024 * 1024
EVENT_MAX_BYTES = 32 * 1024
EVENTS_PER_MINUTE = 60.0
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FORWARD_REQUEST = ("content-type", "accept", "accept-language", "content-length")

_inflight: dict[str, int] = {}
_event_seq = 0


# ── the gate ────────────────────────────────────────────────────────────────


def _binding_of(row: dict, name: str) -> dict | None:
    for b in _mf.parse_bindings(row):
        if b.get("name") == name:
            return b
    return None


def _target_of(binding: dict) -> dict | None:
    """The shared folder row a binding names (personal apps are never
    targets)."""
    row = task_store.get_app_by_slug(binding.get("agent") or "", "", binding.get("app") or "")
    if not row or row.get("hidden") or not task_store.app_kind_of(row).may_serve:
        return None
    if row.get("scope_chat_id") or row.get("scope_project_id"):
        return None
    return row


def edge_ok(caller: dict, target: dict) -> bool:
    """The org-chart rule, read live: a shared caller reaches its own agent
    or an agent its agent delegates to; a personal caller reaches an agent
    its owner is a member of. Revocation is row deletion, so absent means
    revoked."""
    from storage.agents import agent_store
    if caller.get("username"):
        # The owner's row alone (an admin owner with no row holds no edge).
        return bool(roles.row_role(task_store.get_user_agent_roles(caller.get("owner_sub") or ""),
                                   target.get("agent") or ""))
    if caller.get("agent") == target.get("agent"):
        return True
    return (target.get("agent") or "") in agent_store.get_delegation_targets(caller.get("agent") or "")


def gate(caller: dict, target: dict | None) -> dict:
    """Both approvals and the edge, or 404 (never an oracle)."""
    if target is None or not task_store.app_actions_approved(caller) \
            or not task_store.app_actions_approved(target) or not edge_ok(caller, target):
        raise _refuse(404, "not available")
    return target


# The brokered caller claim's ``role`` for a principal that holds no role on
# the target agent (APPS.md "The broker") — a wire word, written from the
# resolver's ``NO_ACCESS``, never compared.
CLAIM_ROLE_NONE = "none"


def role_on(sub: str, agent: str) -> str:
    """The viewer's role on ``agent``: ``admin`` for a platform admin,
    ``none`` for a stranger."""
    return effective_role_of(sub, agent) or CLAIM_ROLE_NONE


def floor_ok(entry: dict, role: str) -> bool:
    """The default floor is reachable on the edge alone; ``editor`` and
    ``manager`` need membership on the target."""
    return _mf.meets_floor({"min_role": entry.get("min_role")}, role)


# ── the caller context ─────────────────────────────────────────────────────


async def _caller_context(request: Request, row: dict) -> tuple[dict, dict | None]:
    """The launch token's app (403 for any other basis), the forwarded
    viewer claim if one is behind the call, and the forwarded parent caller
    claim if the app itself is answering a brokered call."""
    caller = await resolve_caller(request, row)
    if caller.basis != "app":
        raise _refuse(403, "the broker takes the app's launch token")
    if caller.instance != "live":
        raise _refuse(403, "the preview copy cannot call other apps")
    viewer: dict | None = None
    forwarded = request.headers.get("x-otodock-viewer", "")
    if forwarded:
        viewer = app_tokens.verify(forwarded, row["id"], app_tokens.PURPOSE_VIEWER)
        if viewer is None:
            platform = app_tokens.verify(forwarded, row["id"], app_tokens.PURPOSE_CALLER)
            relayed = (platform or {}).get("principal")
            if relayed not in (app_tokens.PRINCIPAL_PLATFORM, app_tokens.PRINCIPAL_AGENT):
                raise _refuse(400, "the forwarded viewer claim is not this app's")
            viewer = platform if relayed == app_tokens.PRINCIPAL_AGENT else None
        elif viewer.get("external"):
            raise _refuse(403, "not available on shared links")
        elif viewer.get("render"):
            # The render's claim names a personal app's owner so the page
            # renders as they see it; it calls nothing as them (the platform
            # route refuses it the same way).
            raise _refuse(403, "not available in the rendered check")
    parent: dict | None = None
    chain = request.headers.get("x-otodock-caller", "")
    if chain:
        parent = app_tokens.verify(chain, row["id"], app_tokens.PURPOSE_CALLER)
        if not parent or parent.get("principal") not in ("membership", "delegation", "platform"):
            raise _refuse(400, "the forwarded caller claim is not this app's")
    return viewer or {}, parent


def _claim_for_target(caller: dict, target: dict, viewer: dict, parent: dict | None) -> tuple[str, str]:
    """``(claim, role on the target)``; 429 past the hop cap or the
    deadline."""
    now = time.time()
    hop = int((parent or {}).get("hop") or 0) + 1 if parent else 0
    deadline = now + CALL_DEADLINE_S
    if parent and parent.get("deadline"):
        deadline = min(deadline, float(parent["deadline"]) - 1.0)
    if hop >= HOP_MAX or deadline <= now:
        raise _refuse(429, "the call chain is too deep or out of time")
    personal = bool(caller.get("username"))
    sub = str(viewer.get("sub") or "") or ((caller.get("owner_sub") or "") if personal else "")
    username = str(viewer.get("username") or "")
    if sub and not username:
        username = (task_store.get_user(sub) or {}).get("username") or ""
    if viewer.get("principal") == app_tokens.PRINCIPAL_AGENT or (personal and not viewer):
        # A session's forwarded claim and an unattended personal call carry
        # the per-agent row alone, never a platform admin's standing (the
        # rule ``_agent_caller`` and ``edge_ok`` apply on their side).
        rows = task_store.get_user_agent_roles(sub) if sub else {}
        role = roles.row_role(rows, target.get("agent") or "") or CLAIM_ROLE_NONE
    else:
        role = role_on(sub, target.get("agent") or "")
    claims = {
        "principal": "membership" if personal else "delegation",
        "caller_app": caller["id"], "caller_agent": caller.get("agent") or "",
        "caller_slug": caller.get("slug") or "",
        "sub": sub, "username": username, "role": role,
        "caller_role": str(viewer.get("role") or ("owner" if personal and not viewer else CLAIM_ROLE_NONE)),
        "hop": hop, "deadline": int(deadline), "external": False,
    }
    return app_tokens.mint(target["id"], app_tokens.PURPOSE_CALLER, claims,
                           int(max(1, deadline - now))), role


# ── the snapshot ────────────────────────────────────────────────────────────


def _read_regular(base: Path, name: str) -> tuple[bytes, os.stat_result]:
    """The bytes of ``exports/<name>.json`` under ``base``: the app writes
    that directory from its sandbox and the proxy reads it with its own
    rights, so no step below ``base`` follows a link, only a regular file is
    read (a FIFO would block the thread for good) and never past the cap."""
    nofollow = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    dfd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        efd = os.open("exports", nofollow | os.O_DIRECTORY, dir_fd=dfd)
        try:
            fd = os.open(f"{name}.json", nofollow | os.O_NONBLOCK, dir_fd=efd)
        finally:
            os.close(efd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _refuse(502, "the snapshot is not a regular file")
        raise
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _refuse(502, "the snapshot is not a regular file")
        chunks: list[bytes] = []
        left = SNAPSHOT_MAX_BYTES + 1
        while left > 0:
            chunk = os.read(fd, min(64 * 1024, left))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
        if len(data) > SNAPSHOT_MAX_BYTES:
            raise _refuse(502, "the snapshot is larger than 1 MB")
        return data, st
    finally:
        os.close(fd)


def _read_snapshot(target: dict, name: str) -> tuple[bytes, str] | None:
    """The bytes and an ETag of ``data/exports/<name>.json``, parsed once to
    prove it is JSON; None when the target has not written it yet. A
    rename in progress is retried once."""
    base = releases.app_data_dir(target)
    for attempt in (0, 1):
        try:
            data, st = _read_regular(base, name)
            json.loads(data.decode("utf-8"))
            return data, f'"{st.st_mtime_ns}-{st.st_size}"'
        except FileNotFoundError:
            if attempt:
                return None
        except (ValueError, UnicodeDecodeError):
            if attempt:
                raise _refuse(502, "the snapshot is not valid JSON")
        time.sleep(0.05)
    return None


@router.get("/v1/apps/{app_id}/bindings/{name}/snapshot/{snap}")
async def read_snapshot(app_id: str, name: str, snap: str, request: Request):
    """A snapshot the target published, served without waking it; the
    export's floor is judged here from the target's signed manifest (the
    target is asleep and cannot)."""
    row = await run_db(_row_or_404, app_id)
    viewer, parent = await _caller_context(request, row)
    binding = _binding_of(row, name)
    if binding is None or not _NAME_RE.match(snap):
        raise _refuse(404, "not available")
    target = await run_db(lambda: gate(row, _target_of(binding)))
    entry = (_mf.parse_exports(target).get("snapshots") or {}).get(snap)
    if entry is None:
        raise _refuse(404, "not available")
    if not _take(f"bind|{row['id']}|{target['id']}", BIND_RATE, BIND_BURST):
        raise _refuse(429, "too many calls to this app")
    _claim, role = await asyncio.to_thread(_claim_for_target, row, target, viewer, parent)
    if not floor_ok(entry, role):
        raise _refuse(403, f"this snapshot needs the {entry.get('min_role')} role on {target.get('agent')}")
    got = await asyncio.to_thread(_read_snapshot, target, snap)
    if got is None:
        raise _refuse(404, "no snapshot yet")
    data, etag = got
    headers = {**_CORS, "ETag": etag, "Cache-Control": "no-cache",
               "X-OtoDock-Snapshot-Of": target["id"]}
    if request.headers.get("if-none-match", "") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="application/json", headers=headers)


# ── the broker ──────────────────────────────────────────────────────────────


@router.api_route("/v1/apps/{app_id}/bindings/{name}/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def broker(app_id: str, name: str, path: str, request: Request):
    """One live call from the calling app to the target's ``/api/{path}``:
    the gate, the export's floor by the first path segment, the caller
    claim for the target, the bounds, then the forward."""
    row = await run_db(_row_or_404, app_id)
    viewer, parent = await _caller_context(request, row)
    binding = _binding_of(row, name)
    if binding is None:
        raise _refuse(404, "not available")
    target = await run_db(lambda: gate(row, _target_of(binding)))
    raw = request.scope.get("raw_path") or b""
    raw_s = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
    check_app_path(path, raw_s)
    methods = _mf.parse_exports(target).get("methods") or {}
    first = path.split("/", 1)[0]
    entry = methods.get(first)
    if entry is None:
        raise _refuse(404, "not exported by that app")
    if not _take(f"bind|{row['id']}|{target['id']}", BIND_RATE, BIND_BURST):
        raise _refuse(429, "too many calls to this app")
    check_rate(target["id"], f"bind:{row['id']}")
    claim, role = await asyncio.to_thread(_claim_for_target, row, target, viewer, parent)
    if not floor_ok(entry, role):
        raise _refuse(403, f"this call needs the {entry.get('min_role')} role on {target.get('agent')}")
    if _inflight.get(row["id"], 0) >= BIND_INFLIGHT_MAX:
        raise _refuse(429, "too many calls in flight")
    try:
        inst = await app_supervisor.ensure_up(target)
    except app_supervisor.AppUnavailable as e:
        return _unavailable(e)
    if inst.state == app_supervisor.STATIC:
        raise _refuse(404, "that app has no server")
    body = await _read_body(request)
    headers: list[tuple[str, str]] = [
        (k, v) for k, v in request.headers.items() if k.lower() in _FORWARD_REQUEST
    ]
    headers += [("X-OtoDock-Caller", claim), ("X-OtoDock-Basis", "binding")]
    query = request.scope.get("query_string") or b""
    upstream_path = f"/api/{path}"
    if query:
        upstream_path += "?" + (query.decode("latin-1") if isinstance(query, bytes) else str(query))
    _inflight[row["id"]] = _inflight.get(row["id"], 0) + 1
    released = False

    def _release() -> None:
        nonlocal released
        if released:
            return
        released = True
        _inflight[row["id"]] = max(0, _inflight.get(row["id"], 1) - 1)

    try:
        upstream = await forward(inst, request.method, upstream_path, headers, body)
    except httpx.HTTPError as e:
        _release()
        logger.warning("Binding %s→%s: upstream error on %s: %s", row.get("slug"),
                       target.get("slug"), path, e)
        raise _refuse(502, "that app did not answer")
    except BaseException:
        _release()
        raise
    app_supervisor.touch(target["id"])
    if 300 <= upstream.status_code < 400 and not _redirect_ok(
            upstream.headers.get("location", ""), target["id"]):
        await upstream.aclose()
        _release()
        raise _refuse(502, "that app answered with a redirect outside its own routes")
    out_headers = [(k, v) for k, v in upstream.headers.multi_items()
                   if k.lower() not in _DROP_RESPONSE and not k.lower().startswith("access-control-")]
    out_headers.extend(_CORS.items())

    async def _close() -> None:
        with contextlib.suppress(Exception):
            await upstream.aclose()
        _release()

    if request.method == "HEAD":
        await _close()
        return Response(status_code=upstream.status_code, headers=dict(out_headers))

    async def _body():
        # The background task never runs when the upstream fails mid-body,
        # so the slot is released here too (``_release`` counts once).
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await _close()

    return StreamingResponse(_body(), status_code=upstream.status_code,
                             headers=dict(out_headers), background=BackgroundTask(_close))


# ── events ──────────────────────────────────────────────────────────────────


@router.post("/v1/apps/{app_id}/events")
async def emit_event(app_id: str, request: Request):
    """The app emits one of its declared events; every approved subscriber
    whose bindings name it and whose handlers listen gets a delivery — the
    subscriber's edge to the emitter re-checked now."""
    global _event_seq
    from services.apps import app_handlers
    row = await run_db(_row_or_404, app_id)
    caller = await resolve_caller(request, row)
    if caller.basis != "app":
        raise _refuse(403, "events are emitted with the app's launch token")
    if caller.instance != "live":
        raise _refuse(403, "the preview copy cannot emit events")
    if not task_store.app_actions_approved(row):
        raise _refuse(409, "the app is waiting for approval")
    # Subscribers find an emitter by (agent, slug) and a binding names only
    # the shared row there (``_target_of``): a personal or Dock row of the
    # same slug would speak in that app's name.
    named = await run_db(_target_of, {"agent": row.get("agent") or "", "app": row.get("slug") or ""})
    if named is None or named["id"] != row["id"]:
        raise _refuse(403, "only a shared app emits events")
    body = await _read_body(request)
    if len(body) > EVENT_MAX_BYTES + 4096:
        raise _refuse(413, "event larger than 32 KB")
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        raise _refuse(400, "a JSON body is required")
    name = str((doc or {}).get("name") or "") if isinstance(doc, dict) else ""
    payload = (doc or {}).get("payload") if isinstance(doc, dict) else None
    events = _mf.parse_exports(row).get("events") or {}
    if not _NAME_RE.match(name) or name not in events:
        raise _refuse(400, "the event is not in the app's exports")
    if payload is not None and not isinstance(payload, dict):
        raise _refuse(400, "payload must be an object")
    if len(json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")) > EVENT_MAX_BYTES:
        raise _refuse(413, "payload larger than 32 KB")
    if not _take(f"{row['id']}|events", EVENTS_PER_MINUTE / 60.0, EVENTS_PER_MINUTE):
        raise _refuse(429, "too many events this minute")
    _event_seq += 1
    event_id = f"{row['id']}:{int(time.time() * 1000)}:{_event_seq}"
    delivered = 0
    for sub_id, handler, bname in await app_handlers.cross_app_subscribers(
            row.get("agent") or "", row.get("slug") or "", name):
        sub_row = await run_db(task_store.get_app, sub_id)
        if not sub_row or sub_row.get("hidden"):
            continue
        try:
            await run_db(lambda: gate(sub_row, row))
        except Exception:
            continue
        d = await app_handlers.enqueue(
            sub_row, handler, f"app:{bname}:{name}",
            {"event": f"app:{bname}:{name}", "name": name,
             "from": {"agent": row.get("agent") or "", "app": row.get("slug") or ""},
             "payload": payload or {}},
            event_id=event_id)
        if d is not None and d["status"] == deliveries.PENDING:
            delivered += 1
    return JSONResponse({"ok": True, "delivered": delivered, "event_id": event_id},
                        headers=dict(_CORS))


# ── describe ────────────────────────────────────────────────────────────────


def describe(target: dict) -> dict:
    """An app's exports with their words, for ``describe_app``."""
    ex = _mf.parse_exports(target)
    return {
        "app_id": target["id"], "agent": target.get("agent") or "", "slug": target.get("slug") or "",
        "title": target.get("title") or target.get("slug") or "",
        "approved": task_store.app_actions_approved(target),
        "exports": {k: ex.get(k) or {} for k in ("methods", "snapshots", "events")},
        "binding": {"agent": target.get("agent") or "", "app": target.get("slug") or ""},
    }


def reachable_agents(session_agent: str, username: str) -> set[str]:
    """The agents whose apps a session may describe: its own, its
    delegation targets, and — for a user-backed session — the agents the
    user is a member of."""
    from storage.agents import agent_store
    out = {session_agent} | set(agent_store.get_delegation_targets(session_agent))
    if username:
        from storage.automation import notification_store
        sub = notification_store.resolve_username_to_sub(username) or ""
        out |= set(task_store.get_user_agent_roles(sub)) if sub else set()
    return out


__all__ = ["router", "describe", "reachable_agents", "edge_ok", "gate", "role_on"]
