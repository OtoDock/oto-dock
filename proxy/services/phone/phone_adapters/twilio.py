"""Twilio adapter — cloud control plane over the Twilio REST API.

Provisions inbound routes by pointing the matching owned phone number's
``VoiceUrl`` at the phone daemon's Twilio webhook
(``{public_base_url}/v1/twilio/inbound/{server_id}``); the daemon answers with
``<Connect><Stream>`` TwiML and carries the media itself. There is no
bootstrap: a Twilio server is usable the moment its credentials validate,
so ``verify_bootstrap`` doubles as the credential + public-URL gate the
route cascade trusts.

Server row contract: ``config.account_sid`` + ``config.public_base_url``
(https, host[:port] only — the daemon reconstructs signed URLs as
``base + path``, so a path prefix would break X-Twilio-Signature
validation); the auth token lives in ``infra_credentials``
(``phone-server-{id}-twilio-auth-token`` → inner key ``AUTH_TOKEN``).
Outbound routes need no vendor provisioning — the daemon originates via
REST at call time; the route's ``ami_caller_id`` is the From number and
must be a Twilio number on this account.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import httpx

from .base import (
    BootstrapResult,
    HealthStatus,
    PhoneAdapterError,
    PhoneServerAdapter,
    RouteHandle,
)

logger = logging.getLogger("claude-proxy")

_TIMEOUT_S = 15.0
_PAGE_SIZE = 100
_MAX_PAGES = 20

_DID_DIGITS_RE = re.compile(r"\D")


def normalize_did(did: str) -> str:
    """Digits-only comparison key — mirrors the phone daemon's
    ``config_manager.normalize_did`` so provisioning, routing and drift all
    agree on what counts as the same number."""
    return _DID_DIGITS_RE.sub("", did or "")


def normalize_public_base_url(raw: str) -> str:
    """Canonical ``https://host[:port]`` or ``""`` when unusable — the same
    rule the daemon applies (``calls/twilio_http.py``), duplicated because
    proxy and phone share no code: the VoiceUrl written here and the URL the
    daemon validates signatures against must be byte-identical."""
    raw = (raw or "").strip().rstrip("/")
    if not raw.lower().startswith("https://"):
        return ""
    parts = urlsplit(raw)
    if not parts.netloc or parts.path or parts.query or parts.fragment:
        return ""
    return f"https://{parts.netloc}"


def resolve_twilio_public_base() -> str:
    """The public HTTPS base Twilio calls in on: the platform's
    ``DASHBOARD_PUBLIC_URL``, always — the proxy publicly relays
    ``/v1/twilio/*`` to the phone daemon, so the dashboard's existing public
    front IS the phone entrance, exactly like every other machine-callable
    surface (satellites, trigger webhooks). Forward-auth installs exempt the
    path (see the reverse-proxy guide); there is deliberately NO per-server
    override — one entrance keeps the SSO posture intact and a future
    dedicated-hostname option would be a purely additive change. An http://
    (LAN-only) dashboard URL yields ``""``: Twilio requires TLS, so
    verify/provision fail with a clear message instead of writing webhooks
    that can never validate."""
    import config
    # The RAW origin, scheme intact (get_platform_public_url strips schemes
    # for its satellite-WSS callers — that would erase the https-only rule).
    return normalize_public_base_url(config.DASHBOARD_PUBLIC_URL or "")


class TwilioAdapter(PhoneServerAdapter):
    """Twilio REST control plane: number lookup + VoiceUrl provisioning,
    credential health probe, and real drift enumeration."""

    adapter_type = "twilio"
    requires_bootstrap = False

    #: REST base — class attr so offline tests can point it at a fake server.
    api_base = "https://api.twilio.com"

    # -- REST plumbing ------------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        sid = str(self.config.get("account_sid") or "").strip()
        token = self._resolve_credentials("twilio-auth-token").get(
            "AUTH_TOKEN", "",
        )
        if not sid or not token:
            raise PhoneAdapterError(
                "Configure the Twilio Account SID and auth token for this "
                "phone server first.",
                status_code=400,
            )
        return sid, token

    def _public_base(self) -> str:
        base = resolve_twilio_public_base()
        if not base:
            raise PhoneAdapterError(
                "Twilio needs a public HTTPS entrance: set the platform's "
                "dashboard public URL (DASHBOARD_PUBLIC_URL, https) — calls "
                "enter through it on /v1/twilio/. Forward-auth gateways must "
                "exempt that path.",
                status_code=400,
            )
        return base

    def _webhook_url(self) -> str:
        return f"{self._public_base()}/v1/twilio/inbound/{self.server_id}"

    async def _request(self, method: str, path: str, *,
                       params: dict | None = None,
                       data: dict | None = None) -> dict:
        """One authenticated call under ``/2010-04-01/Accounts/{sid}``.
        Vendor failures map onto the standard adapter error envelope."""
        sid, token = self._credentials()
        url = f"{self.api_base}/2010-04-01/Accounts/{sid}{path}"
        try:
            async with httpx.AsyncClient(
                auth=(sid, token), timeout=_TIMEOUT_S,
            ) as client:
                resp = await client.request(method, url, params=params, data=data)
        except httpx.TimeoutException:
            raise PhoneAdapterError(
                "Twilio API timed out.", status_code=504,
            )
        except httpx.HTTPError as e:
            raise PhoneAdapterError(f"Twilio API unreachable: {e}")
        if resp.status_code in (401, 403):
            raise PhoneAdapterError(
                "Twilio rejected the credentials — check the Account SID "
                "and auth token.",
                vendor_status=resp.status_code,
            )
        if resp.status_code >= 400:
            raise PhoneAdapterError(
                "Twilio API request failed.",
                vendor_status=resp.status_code,
                vendor_body=resp.text[:300],
            )
        return resp.json()

    async def _find_number(self, did: str) -> dict:
        """The owned IncomingPhoneNumber matching ``did``, or a 400 that
        names the problem. Twilio's ``PhoneNumber=`` filter is
        format-tolerant; the digits-only comparison pins the exact match."""
        listing = await self._request(
            "GET", "/IncomingPhoneNumbers.json",
            params={"PhoneNumber": did, "PageSize": _PAGE_SIZE},
        )
        wanted = normalize_did(did)
        for number in listing.get("incoming_phone_numbers") or []:
            if normalize_did(number.get("phone_number", "")) == wanted:
                return number
        raise PhoneAdapterError(
            f"This Twilio account owns no phone number matching {did!r} — "
            "buy the number (or fix the DID) first.",
            status_code=400,
        )

    # -- Health -------------------------------------------------------------

    async def health_check(self) -> HealthStatus:
        try:
            base = self._public_base()
        except PhoneAdapterError as e:
            return HealthStatus(healthy=False, detail=e.message)
        try:
            account = await self._request("GET", ".json")
        except PhoneAdapterError as e:
            return HealthStatus(healthy=False, detail=e.message)
        status = str(account.get("status") or "")
        detail = f"{account.get('friendly_name', '')} ({status}) via {base}"
        if status and status != "active":
            return HealthStatus(
                healthy=False, detail=f"Account is {status}. {detail}",
            )
        return HealthStatus(healthy=True, detail=detail.strip())

    # -- Bootstrap (credential gate — no one-time install) -------------------

    async def get_bootstrap_snippet(self) -> str | None:
        return None

    async def verify_bootstrap(self) -> BootstrapResult:
        """Twilio has no dialplan to install; ``verified`` is the route
        cascade's gate, so it asserts what provisioning depends on — valid
        credentials AND a usable public_base_url."""
        health = await self.health_check()
        if not health.healthy:
            return BootstrapResult(status="failed", detail=health.detail)
        return BootstrapResult(status="verified", detail=health.detail)

    # -- Route provisioning -------------------------------------------------

    async def provision_route(self, route: dict) -> RouteHandle:
        if route.get("direction") == "outbound":
            caller_id = str(route.get("ami_caller_id") or "").strip()
            if not caller_id:
                raise PhoneAdapterError(
                    "Outbound Twilio routes need a caller ID — the From "
                    "number, a Twilio number on this account (E.164).",
                    status_code=400,
                )
            return RouteHandle(
                adapter_data={},
                instructions=(
                    f"Outbound calls will originate from {caller_id} via the "
                    "Twilio REST API — no number rewiring needed. The number "
                    "must belong to this Twilio account."
                ),
            )

        did = str(route.get("did") or "").strip()
        if not did:
            raise PhoneAdapterError(
                "Inbound Twilio routes need a DID (the Twilio number, E.164).",
                status_code=400,
            )
        webhook = self._webhook_url()
        number = await self._find_number(did)
        if number.get("voice_application_sid"):
            raise PhoneAdapterError(
                f"Number {number.get('phone_number')} is attached to a TwiML "
                "App (VoiceApplicationSid), which would override the webhook "
                "— detach it in the Twilio console first.",
                status_code=400,
            )
        previous = str(number.get("voice_url") or "")
        await self._request(
            "POST", f"/IncomingPhoneNumbers/{number['sid']}.json",
            data={"VoiceUrl": webhook, "VoiceMethod": "POST"},
        )
        canonical = str(number.get("phone_number") or did)
        logger.info(
            f"twilio server {self.server_id}: number {canonical} → {webhook}"
        )
        return RouteHandle(
            adapter_data={
                "number_sid": number["sid"],
                "phone_number": canonical,
                "previous_voice_url": previous,
            },
            did=did,
            instructions=(
                f"Calls to {canonical} now stream to this install. Twilio "
                "must reach the phone daemon at the server's public URL "
                "(TLS-fronted /v1/twilio/* paths)."
            ),
        )

    async def deprovision_route(self, route: dict) -> None:
        if route.get("direction") == "outbound":
            return
        number_sid = (route.get("adapter_data") or {}).get("number_sid")
        if not number_sid:
            return
        try:
            number = await self._request(
                "GET", f"/IncomingPhoneNumbers/{number_sid}.json")
        except PhoneAdapterError as e:
            if e.vendor_status == 404:
                return  # number released — nothing to undo
            raise
        # Only unwire numbers still pointing at THIS server's webhook — the
        # route-update flow deprovisions the OLD identity after provisioning
        # the new one, and a shared account/base URL between two server rows
        # must not let one clear the other's work.
        if str(number.get("voice_url") or "") != self._webhook_url():
            return
        await self._request(
            "POST", f"/IncomingPhoneNumbers/{number_sid}.json",
            data={"VoiceUrl": "", "VoiceMethod": "POST"},
        )

    # -- Drift --------------------------------------------------------------

    async def list_provisioned_routes(self) -> list[RouteHandle] | None:
        """Owned numbers whose VoiceUrl is this server's webhook — the first
        adapter with real drift enumeration. ``did`` carries Twilio's
        canonical E.164 (the health worker compares digits-only)."""
        webhook = self._webhook_url()
        handles: list[RouteHandle] = []
        path = "/IncomingPhoneNumbers.json"
        params: dict | None = {"PageSize": _PAGE_SIZE}
        for _ in range(_MAX_PAGES):
            listing = await self._request("GET", path, params=params)
            for number in listing.get("incoming_phone_numbers") or []:
                if str(number.get("voice_url") or "") == webhook:
                    handles.append(RouteHandle(
                        adapter_data={"number_sid": number.get("sid", "")},
                        did=str(number.get("phone_number") or ""),
                    ))
            next_uri = str(listing.get("next_page_uri") or "")
            if not next_uri:
                break
            # next_page_uri is absolute-path form incl. account + query.
            sid, _token = self._credentials()
            prefix = f"/2010-04-01/Accounts/{sid}"
            path = next_uri.removeprefix(prefix)
            params = None
        return handles
