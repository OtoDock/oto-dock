"""Minimal async Asterisk AMI client for outbound call origination.

Raw TCP protocol — no external dependencies. Supports only the
operations needed for outbound AI calls: connect, login, originate.

Caller-ID resolution for inbound calls is handled by ``call_registry``
+ the HTTP-push pattern. The persistent event listener (signalled DTMF)
lives in ``ami_events`` and shares this module's wire helpers
(``format_action`` / ``read_packet``).
"""

import asyncio
import contextlib
import logging
import uuid

logger = logging.getLogger("ami_client")


class AMIError(Exception):
    pass


def format_action(action: dict) -> bytes:
    """Serialize one AMI action, rejecting frame-delimiter injection.

    CR/LF/NUL are the AMI frame delimiters, never present in a legitimate
    value; an injected value could forge extra AMI fields or whole actions
    (e.g. a System/Command call). Shared by the origination client and the
    event listener so both get the same guard.
    """
    for k, v in action.items():
        if any(c in str(k) or c in str(v) for c in ("\r", "\n", "\x00")):
            raise AMIError(f"illegal control character in AMI field {k!r}")
    lines = [f"{k}: {v}" for k, v in action.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def read_packet(
    reader: asyncio.StreamReader, line_timeout: float,
) -> dict:
    """Read one AMI packet (``Key: Value`` lines until a blank line).

    ``line_timeout`` bounds each line read — the origination client keeps a
    tight request/response budget, the event listener a long idle window
    covered by its ping cadence.
    """
    packet = {}
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=line_timeout)
        if not line:
            raise AMIError("AMI connection closed")
        text = line.decode(errors="replace").strip()
        if not text:
            break
        if ": " in text:
            key, value = text.split(": ", 1)
            packet[key] = value
    return packet


class AMIClient:
    """Async AMI client for Asterisk call origination."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        secret: str,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Open TCP connection and read AMI banner."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=10.0,
        )
        # Read banner line (e.g., "Asterisk Call Manager/6.0.1")
        banner = await asyncio.wait_for(
            self._reader.readline(), timeout=5.0,
        )
        logger.info(f"AMI connected: {banner.decode().strip()}")

    async def login(self) -> None:
        """Authenticate with AMI."""
        response = await self._send_action({
            "Action": "Login",
            "ActionID": "login",
            "Username": self.username,
            "Secret": self.secret,
        })
        if "Success" not in response.get("Response", ""):
            raise AMIError(f"AMI login failed: {response.get('Message', 'unknown')}")
        logger.info("AMI login successful")

    async def originate(
        self,
        phone_number: str,
        audio_uuid: str,
        caller_id: str,
        context: str = "oto-audiosocket-outbound",
        dial_prefix: str = "",
        timeout_ms: int = 30000,
    ) -> dict:
        """Originate an outbound call.

        Dials the phone number via the configured SIP trunk, and upon
        answer routes to the oto-audiosocket-outbound dialplan context which connects
        to AudioSocket with the given UUID.
        """
        action = {
            "Action": "Originate",
            "ActionID": str(uuid.uuid4())[:8],
            "Channel": f"Local/{dial_prefix}{phone_number}@from-internal",
            "Context": context,
            "Exten": "s",
            "Priority": "1",
            "Timeout": str(timeout_ms),
            "CallerID": caller_id,
            "Variable": f"OUTBOUND_UUID={audio_uuid}",
            "Async": "true",
        }
        response = await self._send_action(action)
        logger.info(f"Originate response: {response}")
        return response

    async def _read_packet(self) -> dict:
        """Read one AMI packet (lines until blank line)."""
        return await read_packet(self._reader, line_timeout=10.0)

    async def _send_action(self, action: dict) -> dict:
        """Send an AMI action and read the matching response.

        Skips unrelated events (e.g., FullyBooted) by matching on
        ActionID. Actions without an ActionID match the first Response packet.
        """
        if not self._writer or not self._reader:
            raise AMIError("Not connected")

        self._writer.write(format_action(action))
        await self._writer.drain()

        action_id = action.get("ActionID")

        # Read packets until we find a Response matching our ActionID
        for _ in range(20):  # safety limit
            packet = await self._read_packet()
            if not packet:
                continue

            # Match by ActionID if we sent one
            if action_id and packet.get("ActionID") == action_id:
                return packet

            # Match any Response packet if no ActionID
            if not action_id and "Response" in packet:
                return packet

            logger.debug(f"Skipping AMI event: {packet.get('Event', packet)}")

        raise AMIError("No matching AMI response received")

    async def close(self) -> None:
        """Send Logoff and close connection."""
        if self._writer:
            with contextlib.suppress(Exception):
                await self._send_action({"Action": "Logoff"})
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None
            logger.info("AMI connection closed")

