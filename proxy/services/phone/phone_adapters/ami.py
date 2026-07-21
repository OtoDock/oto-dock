"""Minimal async Asterisk AMI client — AstDB control-plane writes only.

Deliberately separate from ``phone/telephony/ami_client.py`` (the media-plane *Originate*
client that lives in the phone daemon). This one runs proxy-side and does ONLY
what the FreePBX control-plane adapter needs: log in and ``DBPut``/``DBGet``/
``DBDel`` against the ``otodock/route_uuid/*`` AstDB tree, using a least-privilege
``system``-class AMI user. It never originates calls and never runs CLI commands.

Each operation is a short-lived connection (connect → login → op → logoff); use
it as an async context manager. Raw asyncio TCP, no dependencies. Failures raise
``PhoneAdapterError`` (504 on timeout, 502 on connection/auth/protocol error) so
the adapter and admin API surface a clean status instead of a 500.

AMI protocol notes that shape this client:
  * Actions and their responses are correlated by ``ActionID``. We tag every
    action with a unique id and filter strictly on it, so unrelated events on the
    socket (FullyBooted, etc.) are simply skipped — no ``Events: off`` needed,
    which also sidesteps version-dependent masking of the DBGetResponse event.
  * ``DBGet`` acks with ``Response: Success`` and then delivers the value in a
    *separate* ``DBGetResponse`` event (same ActionID); a missing key acks with
    ``Response: Error``. ``DBPut``/``DBDel`` are answered on the Response line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from .base import PhoneAdapterError

logger = logging.getLogger("claude-proxy")

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 10.0
# Safety bound on the per-action read loop — comfortably absorbs a few stray
# events between login and the single op on a short-lived connection.
_MAX_PACKETS = 50


class AMIClient:
    """Async AMI client scoped to AstDB get/put/del.

    ``async with AMIClient(host=..., port=..., username=..., secret=...) as ami:``
    connects + logs in on enter and logs off + closes on exit (even on error).
    The secret is never logged — ``__repr__`` redacts it.
    """

    def __init__(self, *, host: str, port: int, username: str, secret: str) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._secret = secret
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    def __repr__(self) -> str:  # never leak the secret
        return f"<AMIClient {self.username}@{self.host}:{self.port}>"

    async def __aenter__(self) -> "AMIClient":
        await self.connect()
        try:
            await self.login()
        except Exception:
            await self.close()  # don't leak the socket on a failed login
            raise
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    # -- connection lifecycle ----------------------------------------------
    async def connect(self) -> None:
        """Open the TCP connection and consume the AMI banner line."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=_CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError as e:
            raise PhoneAdapterError(
                f"AMI connect timed out ({self.host}:{self.port})", status_code=504,
            ) from e
        except OSError as e:
            raise PhoneAdapterError(
                f"AMI connect failed ({self.host}:{self.port}): {e}", status_code=502,
            ) from e
        try:
            banner = await asyncio.wait_for(
                self._reader.readline(), timeout=_READ_TIMEOUT,
            )
        except asyncio.TimeoutError as e:
            await self.close()  # tear down the half-open socket
            raise PhoneAdapterError(
                f"AMI banner read timed out ({self.host}:{self.port})", status_code=504,
            ) from e
        logger.debug("AMI connected: %s", banner.decode(errors="replace").strip())

    async def login(self) -> None:
        resp = await self._request({
            "Action": "Login",
            "Username": self.username,
            "Secret": self._secret,
        })
        if "Success" not in resp.get("Response", ""):
            raise PhoneAdapterError(
                f"AMI login failed: {resp.get('Message', 'authentication rejected')}",
                status_code=502,
            )

    async def close(self) -> None:
        """Best-effort Logoff + socket teardown. Safe to call more than once."""
        if self._writer is None:
            return
        with contextlib.suppress(Exception):
            await self._write_action({"Action": "Logoff"})
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass
        self._reader = None
        self._writer = None

    # -- AstDB operations ---------------------------------------------------
    async def db_put(self, family: str, key: str, val: str) -> None:
        resp = await self._request({
            "Action": "DBPut", "Family": family, "Key": key, "Val": val,
        })
        if "Success" not in resp.get("Response", ""):
            raise PhoneAdapterError(
                f"AMI DBPut {family}/{key} failed: {resp.get('Message', 'error')}",
                status_code=502,
            )

    async def db_del(self, family: str, key: str) -> None:
        """Delete a key. An already-absent key is treated as success (idempotent),
        so a best-effort deprovision never fails on a missing entry."""
        resp = await self._request({
            "Action": "DBDel", "Family": family, "Key": key,
        })
        if "Success" in resp.get("Response", ""):
            return
        message = resp.get("Message", "").lower()
        if "does not exist" in message or "not found" in message:
            return  # nothing to delete — idempotent
        raise PhoneAdapterError(
            f"AMI DBDel {family}/{key} failed: {resp.get('Message', 'error')}",
            status_code=502,
        )

    async def db_get(self, family: str, key: str) -> str | None:
        """Return the stored value, or ``None`` if the key is unset.

        The value is NOT on the Response line — it arrives in a follow-up
        ``DBGetResponse`` event correlated by ActionID. A missing key answers
        with ``Response: Error``.
        """
        action_id = str(uuid.uuid4())
        await self._write_action({
            "Action": "DBGet", "ActionID": action_id, "Family": family, "Key": key,
        })
        for _ in range(_MAX_PACKETS):
            packet = await self._read_packet()
            if not packet or packet.get("ActionID") != action_id:
                continue  # unrelated event / response — keep reading
            if packet.get("Event") == "DBGetResponse":
                return packet.get("Val", "")
            if packet.get("Response") == "Error":
                return None  # key not found
            # ``Response: Success`` ack — the value follows in the event above.
        raise PhoneAdapterError(
            f"AMI DBGet {family}/{key}: no DBGetResponse received", status_code=502,
        )

    # -- protocol plumbing --------------------------------------------------
    async def _request(self, action: dict) -> dict:
        """Send an action (auto-tagged with a unique ActionID) and return its
        first ActionID-matched Response packet."""
        action_id = action.get("ActionID") or str(uuid.uuid4())
        action["ActionID"] = action_id
        await self._write_action(action)
        for _ in range(_MAX_PACKETS):
            packet = await self._read_packet()
            if not packet:
                continue
            if packet.get("ActionID") == action_id and "Response" in packet:
                return packet
        raise PhoneAdapterError("AMI: no response received", status_code=502)

    async def _write_action(self, action: dict) -> None:
        if self._writer is None:
            raise PhoneAdapterError("AMI not connected", status_code=502)
        # "Key: Value\r\n" per field, terminated by a blank "\r\n".
        lines = [f"{k}: {v}" for k, v in action.items()]
        message = "\r\n".join(lines) + "\r\n\r\n"
        self._writer.write(message.encode())
        await self._writer.drain()

    async def _read_packet(self) -> dict:
        """Read one AMI packet (header lines until a blank line) into a dict."""
        if self._reader is None:
            raise PhoneAdapterError("AMI not connected", status_code=502)
        packet: dict[str, str] = {}
        while True:
            try:
                line = await asyncio.wait_for(
                    self._reader.readline(), timeout=_READ_TIMEOUT,
                )
            except asyncio.TimeoutError as e:
                raise PhoneAdapterError("AMI read timed out", status_code=504) from e
            if not line:  # EOF — peer closed the connection
                raise PhoneAdapterError("AMI connection closed by peer", status_code=502)
            text = line.decode(errors="replace").strip()
            if not text:
                break
            if ": " in text:
                k, v = text.split(": ", 1)
                packet[k] = v
        return packet
