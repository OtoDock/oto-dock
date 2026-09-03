"""Minimal async Twilio REST client — outbound call origination only.

The daemon needs exactly one vendor call: `POST Calls.json` with inline
TwiML pointing the answered call at our media stream URL. The proxy's
control-plane adapter has its own httpx-based client; the ~40 duplicated
lines are deliberate — proxy and phone share no code today, and a shared
package for one endpoint would invert that boundary.
"""

from __future__ import annotations

import aiohttp


class TwilioRestError(Exception):
    """A Twilio REST call failed. Carries the vendor status + body excerpt;
    never the auth token."""

    def __init__(self, message: str, *, status: int | None = None,
                 body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body[:500]


class TwilioRestClient:
    """Account-scoped client. ``base_url`` is overridable for offline tests."""

    BASE_URL = "https://api.twilio.com"

    def __init__(self, account_sid: str, auth_token: str,
                 base_url: str | None = None):
        self._account_sid = account_sid
        # Pre-encoded basic-auth header (the `auth=` kwarg is deprecated for
        # aiohttp v4).
        self._auth_header = aiohttp.BasicAuth(account_sid, auth_token).encode()
        self._base_url = (base_url or self.BASE_URL).rstrip("/")

    def __repr__(self) -> str:  # never leak the token
        return f"<TwilioRestClient account={self._account_sid[:8]}…>"

    async def create_call(
        self,
        *,
        to: str,
        from_: str,
        twiml: str,
        timeout_s: int,
        status_callback: str,
        status_events: tuple[str, ...] = ("initiated", "answered", "completed"),
    ) -> dict:
        """`POST /2010-04-01/Accounts/{sid}/Calls.json` → the call record.

        ``Twiml`` rides inline (≤ 4000 chars — ours is ~300) so no answer
        webhook is needed; ``StatusCallbackEvent`` repeats per event, as the
        API requires.
        """
        url = (f"{self._base_url}/2010-04-01/Accounts/"
               f"{self._account_sid}/Calls.json")
        data = [
            ("To", to),
            ("From", from_),
            ("Twiml", twiml),
            ("Timeout", str(timeout_s)),
            ("StatusCallback", status_callback),
            ("StatusCallbackMethod", "POST"),
        ] + [("StatusCallbackEvent", e) for e in status_events]
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url, data=data,
                    headers={"Authorization": self._auth_header},
                ) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        raise TwilioRestError(
                            f"Twilio call create rejected ({resp.status})",
                            status=resp.status, body=body,
                        )
                    return await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise TwilioRestError(f"Twilio REST unreachable: {e}")
