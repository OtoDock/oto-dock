"""The booking template's two halves, its own server run under bun: a caller's
session (an ``external`` caller claim with a channel and an opaque person)
sees the free times, books one and reads only their own bookings, in full
only when verified; a link visitor never reaches the caller's methods; the
owner's view (one of the agent's own editors, never a person the app was
only shared with) lists every booking, a caller's too. Skipped where bun is
missing."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from services.apps import app_tokens
from tests._paths import REPO_ROOT

TEMPLATE = REPO_ROOT / "mcps" / "custom" / "display-mcp" / "skills" / "app-authoring" / "templates" / "booking"
APP_ID = "0f0f0f0f-1111-2222-3333-444444444444"

pytestmark = pytest.mark.skipif(not shutil.which("bun"), reason="bun is not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "PORT": str(port),
           "OTODOCK_DATA_DIR": str(tmp_path), "OTODOCK_APP_ID": APP_ID,
           "OTODOCK_APP_PUBLIC_KEY": app_tokens.public_key_b64(APP_ID)}
    proc = subprocess.Popen([shutil.which("bun") or "bun", "run", str(TEMPLATE / "server" / "index.ts")],
                            cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{base}/_health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _call(base: str, path: str, claim: dict, *, purpose: str, basis: str, body: dict | None = None,
          method: str = "POST") -> tuple[int, dict]:
    token = app_tokens.mint(APP_ID, purpose, claim, 60)
    req = urllib.request.Request(f"{base}{path}", method=method, data=json.dumps(body or {}).encode(),
                                 headers={"x-otodock-viewer": token, "x-otodock-basis": basis,
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _caller(person: str, *, verified: bool = False, ephemeral: bool = False) -> dict:
    return {"principal": "external", "sub": "", "username": "", "role": "external", "agent": "support",
            "external": True, "channel": "phone", "person": person, "ephemeral": ephemeral,
            "verified": verified}


def test_a_caller_books_and_reads_only_their_own_and_the_owner_sees_every_booking(server):
    call = lambda path, claim, **kw: _call(server, path, claim, purpose=app_tokens.PURPOSE_CALLER,
                                           basis="external", **kw)
    status, slots = call("/api/slots", _caller("p1"))
    assert status == 200 and slots["slots"], slots
    first, second = slots["slots"][0]["starts_at"], slots["slots"][1]["starts_at"]
    status, booked = call("/api/book", _caller("p1"), body={"starts_at": first})
    assert status == 200 and booked["booked"]["starts_at"] == first, booked
    # The same time again is taken; a hidden number books nothing.
    assert call("/api/book", _caller("p2"), body={"starts_at": first})[0] == 409
    status, out = call("/api/book", _caller("", ephemeral=True), body={"starts_at": second})
    assert status == 400 and "cannot tell" in out["error"]
    # Their own bookings: a count unverified, the list verified, never another's.
    assert call("/api/mine", _caller("p1"))[1] == {
        "count": 1, "note": "The caller was not verified, so only the count is given."}
    status, mine = call("/api/mine", _caller("p1", verified=True))
    assert status == 200 and len(mine["bookings"]) == 1 and "name" not in mine["bookings"][0]
    assert call("/api/mine", _caller("p2", verified=True))[1] == {"bookings": []}
    # A link visitor (a grant, the app's own customer) never reaches them.
    link = {"principal": "external", "role": "external", "external": True, "grant": "share-1", "sub": ""}
    status, _ = _call(server, "/api/mine", link, purpose=app_tokens.PURPOSE_VIEWER, basis="external")
    assert status == 403
    # The owner's view: the agent's own editor sees the caller's booking;
    # a person the app was shared with at editor does not.
    editor = {"principal": "viewer", "sub": "u-ed", "username": "ed", "role": "editor", "grant": "",
              "external": False}
    status, owner = _call(server, "/owner/bookings", editor, purpose=app_tokens.PURPOSE_VIEWER,
                          basis="viewer", method="GET", body=None)
    assert status == 200, owner
    assert [(b["source"], b["name"]) for b in owner["bookings"]] == [("call", "A caller")]
    grantee = {**editor, "grant": "share-2"}
    assert _call(server, "/owner/bookings", grantee, purpose=app_tokens.PURPOSE_VIEWER,
                 basis="viewer", method="GET")[0] == 403


def test_a_callers_past_bookings_never_count_against_a_new_one(server, data_dir):
    """The cap counts a caller's bookings still ahead: two bookings whose time
    is over leave the caller free to book again."""
    import sqlite3
    call = lambda path, claim, **kw: _call(server, path, claim, purpose=app_tokens.PURPOSE_CALLER,
                                           basis="external", **kw)
    free = call("/api/slots", _caller("p1"))[1]["slots"]
    for slot in free[:2]:
        assert call("/api/book", _caller("p1"), body={"starts_at": slot["starts_at"]})[0] == 200
    status, out = call("/api/book", _caller("p1"), body={"starts_at": free[2]["starts_at"]})
    assert status == 409 and out["error"] == "You already have two bookings ahead, so another one cannot be made by phone.", out
    with sqlite3.connect(data_dir / "app.db") as db:
        db.execute("UPDATE bookings SET starts_at = '2020-01-0' || id || 'T09:00:00.000Z' WHERE caller = 'phone:p1'")
    status, out = call("/api/book", _caller("p1"), body={"starts_at": free[2]["starts_at"]})
    assert status == 200, out
