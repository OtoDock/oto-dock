"""A compute machine on another server: added, paired and locked by an admin
in person, owned by nobody.

- ``GET /v1/admin/remote-machines/compute``: whether this install sends
  sessions to compute machines, and the image a machine on another server
  runs (the Compute card renders from it, with or without a machine).
- ``POST /v1/admin/remote-machines/compute``: add one (``{name}``). The
  answer carries the one command an admin pastes on the server, with a
  one-time pairing token; every admin is told.
- ``POST /v1/admin/remote-machines/compute/{id}/pair``: a new command for a
  machine that lost its sign-in; every admin is told.
- ``PUT /v1/admin/remote-machines/compute/{id}/address-lock``: "Lock to this
  address" (``{locked, address}``).
- ``POST /v1/satellite/bootstrap``: the exchange the install script makes
  after its checks, with the pairing token and the hash of the secret it made
  (``services/remote/compute_pairing``). The script itself is served by the
  bootstrap door every machine pairs through (``remote_machines``).
- ``GET /v1/satellite/compute-host``: the machine's host service asks, with
  the machine's id and secret, which image tag to run and whether the machine
  was removed, before it acts on the machine's doorbell (exit 76 or 77).

A compute machine is the platform's own: no person owns it. It serves the
platform's place, and no door places a session on it by its id
(``core.placement``'s ``machine_is_compute``, the store's settings writers).
It is listed and removed with the other machines (``remote_machines``);
included by that router. The install's own compute machine is made by the
install (``compute_bootstrap``) and has no command, no pairing and no lock.
"""

from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import config
from auth import lan_check
from auth.providers import UserContext, get_current_user, require_admin, require_human
from core import host_os, placement
from services.remote import compute_bootstrap, compute_pairing
from storage import compute_store, remote_store
from storage.pg import run_db

logger = logging.getLogger("otodock")

router = APIRouter()

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


async def compute_machine_refused(request: Request, exc: remote_store.ComputeMachineRefused):
    """The answer to a write a compute machine does not take (its settings,
    an update, a default place): the store refuses it in one place, and the
    app registers this handler so every route that reached it answers 400
    with the store's sentence."""
    return JSONResponse({"detail": str(exc)}, status_code=400)


class AddComputeRequest(BaseModel):
    name: str


class AddressLockRequest(BaseModel):
    locked: bool
    address: str = ""


class ExchangeRequest(BaseModel):
    secret_hash: str


#: Why an address other clients share cannot be locked.
SHARED_ADDRESS = "Other clients reach OtoDock from this address too, so it cannot be locked."


def _public_host() -> str:
    host = config.get_platform_public_url()
    if not host:
        raise HTTPException(
            status_code=400,
            detail="Set DASHBOARD_PUBLIC_URL before you add a compute machine: its command needs the "
                   "address the server reaches OtoDock at.")
    return host


def _settings() -> dict:
    """What the Compute card and its command step say about this install.
    ``own``: the install runs a compute machine of its own (the compose
    service, the bare-metal unit). ``docker``: where its log is read, so the
    card names the command that shows why none is online."""
    import config
    from core.config import deployment
    image = compute_pairing.wanted_image()
    return {"image": image.ref if image else "",
            "image_public": bool(image and image.public), "plain": deployment.link_is_plain(),
            "add_refusal": compute_pairing.add_refusal(),
            "own": compute_bootstrap.runs_own(), "docker": bool(config.RUNNING_IN_DOCKER)}


def _command(machine: dict, token: str, public_host: str) -> dict:
    return {**_settings(), "id": machine["id"], "name": machine["name"],
            "install_command": compute_pairing.install_command(public_host, token),
            "expires_in_hours": remote_store.PAIRING_TOKEN_EXPIRY_HOURS}


async def _added_machine(machine_id: str) -> dict:
    """A compute machine an admin added (never the install's own), or 404."""
    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if not machine or not placement.machine_is_compute(machine) or await run_db(compute_bootstrap.is_own, machine_id):
        raise HTTPException(status_code=404, detail="Machine not found")
    return machine


@router.get("/v1/admin/remote-machines/compute")
async def compute_settings(user: UserContext | None = Depends(get_current_user)):
    require_admin(user)
    return _settings()


@router.post("/v1/admin/remote-machines/compute")
async def add_compute_machine(
    body: AddComputeRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Add a compute machine on another server (an admin, a person: a key
    or a session's token never adds one). Its command is shown; no secret
    exists until the server makes one."""
    u = require_admin(require_human(user))
    if compute_pairing.add_refusal():
        raise HTTPException(status_code=400, detail=compute_pairing.add_refusal())
    name = (body.name or "").strip()
    if not name or _CONTROL.search(name) or len(name) > 100:
        raise HTTPException(status_code=400, detail="Give the machine a name of one line.")
    public_host = _public_host()
    try:
        row = await run_db(
            remote_store.create_remote_machine,
            machine_id=f"compute-{secrets.token_hex(8)}", name=name,
            registered_by=None, pairing_scope=placement.PAIRING_COMPUTE, paired=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await compute_pairing.tell_admins(u, row, "added")
    return _command(row, row["pairing_token"], public_host)


@router.post("/v1/admin/remote-machines/compute/{machine_id}/pair")
async def pair_compute_machine_again(
    machine_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """A new command for a compute machine on another server: a person who
    is an admin. Its current sign-in works until the command runs."""
    u = require_admin(require_human(user))
    machine = await _added_machine(machine_id)
    if compute_pairing.add_refusal():
        raise HTTPException(status_code=400, detail=compute_pairing.add_refusal())
    public_host = _public_host()
    token = await run_db(remote_store.mint_pairing_token, machine_id)
    await compute_pairing.tell_admins(u, machine, "pair_again")
    return _command(machine, token, public_host)


@router.put("/v1/admin/remote-machines/compute/{machine_id}/address-lock")
async def set_compute_address_lock(
    machine_id: str,
    body: AddressLockRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """"Lock to this address": on, a sign-in from another address than the
    one shown (``address``) is refused and every admin is told; off, the
    machine signs in from any address. A person who is an admin."""
    require_admin(require_human(user))
    await _added_machine(machine_id)
    if body.locked and lan_check.address_is_shared(body.address.strip()):
        raise HTTPException(status_code=409, detail=SHARED_ADDRESS)
    problem = await run_db(compute_store.set_address_lock, machine_id, body.locked, body.address.strip())
    if problem:
        raise HTTPException(status_code=409 if body.locked else 404, detail=problem)
    if not body.locked:
        compute_pairing.forget_refused(machine_id)
    return {"ok": True, "locked": body.locked}


async def bootstrap_response(machine_id: str, token: str, family: str, expired: bool) -> Response:
    """The bootstrap door's answer for a compute row's token: the compute
    install script, nothing exchanged (its checks come first)."""
    if family != host_os.LINUX:
        raise HTTPException(status_code=400, detail="A compute machine runs on Linux.")
    if expired:
        raise HTTPException(status_code=401, detail="Pairing token expired")
    if compute_pairing.add_refusal():
        raise HTTPException(status_code=400, detail=compute_pairing.add_refusal())
    from ws.satellite import SATELLITE_VERSION_LATEST
    try:
        body = compute_pairing.bootstrap_script(machine_id, token, _public_host(), SATELLITE_VERSION_LATEST or "")
    except ValueError as e:
        logger.error("Compute: the install script for %s was not made: %s", machine_id, e)
        raise HTTPException(status_code=500, detail="The install script could not be made.") from e
    return Response(content=body, media_type="text/x-shellscript", headers={"Cache-Control": "no-store"})


@router.post("/v1/satellite/bootstrap")
async def exchange_compute_pairing(
    body: ExchangeRequest,
    request: Request,
    x_pairing_token: str = Header("", alias="X-Pairing-Token"),
):
    """The compute install script's exchange, after its checks: the pairing
    token for the hash of the secret the server made. The address it came
    from is the machine's first (``compute_pairing.link_address``)."""
    token = x_pairing_token.strip()
    row = await run_db(remote_store.pairing_row, token) if token else None
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or already-exchanged pairing token")
    if row["pairing_scope"] != placement.PAIRING_COMPUTE:
        raise HTTPException(status_code=400, detail="This pairing token is for a remote machine: run its own command.")
    address, why, _shared = compute_pairing.link_address(request.scope)
    if why:
        raise HTTPException(status_code=403, detail=why)
    try:
        await run_db(remote_store.exchange_pairing_token, row["id"], token,
                     secret_hash=body.secret_hash.strip(), address=address)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    await run_db(compute_bootstrap.load_addresses)
    machine = await run_db(remote_store.get_remote_machine, row["id"]) or {"id": row["id"]}
    await compute_pairing.tell_admins(None, machine, "paired", address=address)
    return {"machine_id": row["id"]}


@router.get("/v1/satellite/compute-host")
async def compute_host_answer(
    x_machine_id: str = Header("", alias="X-Machine-Id"),
    x_machine_secret: str = Header("", alias="X-Machine-Secret"),
):
    """What a compute machine's host service asks before it acts on the
    machine's doorbell, signed with the machine's id and secret (read by root
    from the sign-in file the container only reads): the image it runs now,
    or 410 when an admin removed the machine (``compute_store.removed_verifies``).
    Anything else is 401: the host does nothing."""
    from ws.satellite import SATELLITE_VERSION_LATEST
    machine_id, secret = x_machine_id.strip(), x_machine_secret.strip()
    if not machine_id or not secret:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if machine is None:
        if await run_db(compute_store.removed_verifies, machine_id, secret):
            return JSONResponse({"removed": True}, status_code=410)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not placement.compute_own_copy(machine) or not await run_db(
            remote_store.verify_machine_secret, machine_id, secret):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    image = compute_pairing.wanted_image()
    return {"image": image.ref if image else "", "satellite_version": SATELLITE_VERSION_LATEST or ""}
