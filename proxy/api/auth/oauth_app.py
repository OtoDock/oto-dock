"""The GitHub App routes of a person's MCP account — the install page, the
app's setup URL and the re-listing of an account's installations. The
connect itself goes through the generic routes of ``api/auth/oauth.py`` (a
GitHub App's connect is an OAuth connect through the app's own kind); the
listing, the token files and the lifecycle are ``services/oauth/github_apps``.

Routes:
  POST   /v1/oauth/{provider}/app/install-url            — body: {mcp_name, account_label, mobile?}
  GET    /v1/oauth/{provider}/app/setup                  — GitHub's return after an install; returns HTML
  POST   /v1/oauth/{provider}/app/installations/refresh  — body: {mcp_name, account_label}
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.auth.oauth import (
    _bind,
    _bound_response,
    _connect_bound,
    _connect_ring_cookie,
    _error_html,
    _popup_origin,
    _redirect_uri,
    _refuse_another_address,
    _success_html,
    _validate_provider_for_mcp,
)
from auth.oauth_providers import canonical_provider_id
from auth.oauth_providers.base import INVALID_GRANT, OAuthTokenError
from auth.providers import UserContext, get_current_user, require_human
from services.billing import relay_client
from services.oauth import github_apps, oauth_engine
from storage.identity import credential_store, oauth_app_kinds

logger = logging.getLogger("otodock.oauth-api")
router = APIRouter()


class AppAccountRequest(BaseModel):
    """A GitHub-App account of the caller: the install page and the
    re-listing of its installations."""
    mcp_name: str
    account_label: str
    mobile: bool = False


def _notice_html(title: str, message: str) -> str:
    """A page that reports and closes nothing: the app's setup URL reached
    without a connect to finish (the person installed from GitHub's own
    page, or the state expired)."""
    return f"""\
<!DOCTYPE html>
<html><head><title>{html.escape(title)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; align-items: center;
         justify-content: center; height: 100vh; margin: 0; background: #faf9f9; }}
  .card {{ text-align: center; padding: 2rem; border-radius: 12px;
           background: white; border: 1px solid #e5e7eb; max-width: 360px; }}
  h2 {{ color: #333; margin: 0 0 0.5rem; font-size: 1.1rem; }}
  p {{ color: #666; font-size: 0.9rem; margin: 0; }}
</style></head>
<body><div class="card">
  <h2>{html.escape(title)}</h2>
  <p>{html.escape(message)}</p>
  <p style="margin-top:1rem;color:#999;font-size:0.8rem;">You can close this window.</p>
</div></body></html>"""


def _own_github_app_account(user: UserContext, body: AppAccountRequest) -> dict:
    """The caller's GitHub-App account named by ``body``, or a refusal:
    404 for a label they do not hold, 400 for an account of another kind."""
    accounts = credential_store.list_user_accounts(user.sub, body.mcp_name)
    row = next((a for a in accounts if a["account_label"] == body.account_label), None)
    if row is None:
        raise HTTPException(404, f"No account {body.account_label!r} is connected for {body.mcp_name}")
    if not oauth_app_kinds.is_github_app_meta(row.get("meta")):
        raise HTTPException(400, "This account did not sign in through a GitHub App")
    return row


# The relay's per-install limit is shared by its install page and its
# re-list: a refusal reads as this, not as the relay's generic sentence (the
# same code on any other relay call is not about the install page).
_INSTALL_PAGE_BUSY = "GitHub's install page was asked for too often. Wait a minute and try again."

# Refresh re-lists at GitHub: one run per account per this many seconds.
_REFRESH_INTERVAL_S = 10.0
_refresh_last: dict[tuple[str, str, str], float] = {}


@router.post("/v1/oauth/{provider}/app/install-url")
async def app_install_url(
    provider: str,
    body: AppAccountRequest,
    request: Request,
    user: UserContext = Depends(get_current_user),
    connect_ring: str | None = Depends(_connect_ring_cookie),
):
    """GitHub's page to install the app on the repositories the person
    chooses, for one of their GitHub-App accounts: the page returns to the
    setup URL with a state minted here (bound to this browser like a
    connect), which re-lists the account's installations. OtoDock's hosted
    app's page is asked of the relay, which records this install's setup
    URL and sends GitHub's return there through its own."""
    require_human(user)
    _validate_provider_for_mcp(body.mcp_name, provider)
    await asyncio.to_thread(_own_github_app_account, user, body)
    _refuse_another_address(request, body.mobile)
    url, state = await _install_page(provider, body.mcp_name, body.account_label, user.sub, mobile=body.mobile)
    return _bound_response({"url": url}, connect_ring, state)


# Where every install page lives: an admin's app's by its slug, OtoDock's
# from the relay.
_GITHUB_PAGE = "https://github.com/"


async def _install_page(
    provider: str, mcp_name: str, account_label: str, user_sub: str, *, mobile: bool,
) -> tuple[str, str]:
    """GitHub's install page for one GitHub-App account and the state its
    return carries (the caller binds it to the browser). ``HTTPException``
    with the sentence when the app or the relay cannot give one now."""
    try:
        in_use = await asyncio.to_thread(github_apps.app_in_use, mcp_name)
    except relay_client.RelayNotConfigured as e:
        raise HTTPException(503, str(e)) from e
    except relay_client.RelayError as e:
        raise HTTPException(503, relay_client.relay_error_message(e.code)) from e
    except (httpx.HTTPError, OSError) as e:
        raise HTTPException(503, "The hosted relay could not be reached; try again later") from e
    if in_use.kind != oauth_app_kinds.KIND_GITHUB_APP or not in_use.slug:
        if in_use.hosted:
            raise HTTPException(409, "OtoDock's GitHub App is not available right now. Try again later.")
        raise HTTPException(
            409, "This install's GitHub credentials are not a GitHub App now; "
            "an admin sets one in MCP Servers.",
        )
    state = oauth_engine.create_state(
        user_sub=user_sub, mcp_name=mcp_name, provider_id=provider, services=[],
        account_label_hint=account_label, mobile=mobile,
        redirect_uri=_redirect_uri(provider), app_kind=in_use.kind, install=True,
    )
    if in_use.hosted:
        setup_uri = _redirect_uri(provider).rsplit("/", 1)[0] + "/app/setup"
        try:
            url = await asyncio.to_thread(
                relay_client.oauth_app_install_url, provider_id=provider, state=state,
                install_callback=setup_uri, user_sub=user_sub,
            )
        except relay_client.RelayError as e:
            oauth_engine.discard_state(state)
            if e.code == relay_client.RATE_LIMITED:
                raise HTTPException(429, _INSTALL_PAGE_BUSY) from e
            raise HTTPException(503, relay_client.relay_error_message(e.code)) from e
        except (relay_client.RelayNotConfigured, httpx.HTTPError, OSError) as e:
            oauth_engine.discard_state(state)
            raise HTTPException(503, "OtoDock could not be reached. Try again later.") from e
    else:
        url = github_apps.install_url(in_use.slug, state)
    if not url.startswith(_GITHUB_PAGE):
        # The onward page opens it by itself on the dashboard's own origin.
        oauth_engine.discard_state(state)
        logger.warning("GitHub app: an install page that is not on GitHub was refused")
        raise HTTPException(503, "OtoDock could not get GitHub's install page. Try again later.")
    return url, state


def _onward_html(provider: str, email: str, url: str) -> str:
    """The connect's success page that goes on to GitHub's install page in
    the same window: the dashboard learns of the connect (the same message
    as the plain success page), then the window opens ``url``. The values
    reach the script only through escaped data attributes."""
    return f"""\
<!DOCTYPE html>
<html><head><title>Connected</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; align-items: center;
         justify-content: center; height: 100vh; margin: 0; background: #faf9f9; }}
  .card {{ text-align: center; padding: 2rem; border-radius: 12px;
           background: white; border: 1px solid #e5e7eb; max-width: 360px; }}
  .check {{ font-size: 3rem; margin-bottom: 0.5rem; }}
  h2 {{ color: #333; margin: 0 0 0.5rem; font-size: 1.1rem; }}
  p {{ color: #666; font-size: 0.9rem; margin: 0; }}
</style></head>
<body><div class="card" id="result" data-provider="{html.escape(provider)}"
  data-email="{html.escape(email)}" data-origin="{html.escape(_popup_origin())}"
  data-next="{html.escape(url)}">
  <div class="check">&#10003;</div>
  <h2>{html.escape(provider.capitalize())} Account Connected</h2>
  <p>{html.escape(email)}</p>
  <p style="margin-top:1rem;color:#999;font-size:0.8rem;">Next, choose where to install the app on GitHub.</p>
</div>
<script>
  var el = document.getElementById("result");
  if (window.opener) {{
    window.opener.postMessage({{type: "oauth-complete", provider: el.dataset.provider, email: el.dataset.email}}, el.dataset.origin);
  }}
  setTimeout(() => window.location.replace(el.dataset.next), 1200);
</script>
</body></html>"""


async def connect_page(provider: str, result: oauth_engine.ExchangeResult, request: Request) -> HTMLResponse:
    """The web connect's last page. A GitHub-App account whose listing
    with the fresh token found no installation goes on, in the same window,
    to GitHub's install page: the person signs in and chooses where the app
    goes in one visit, and the setup page lists the installation and closes.
    Anything else, or an install page that cannot be had now, closes as
    before (the card keeps Install on GitHub)."""
    if result.installations_listed != 0:
        return HTMLResponse(_success_html(provider, result.email))
    try:
        url, state = await _install_page(
            provider, result.state.mcp_name, result.account_label, result.state.user_sub, mobile=False,
        )
    except HTTPException as e:
        logger.info("GitHub app connect: no install page after the connect (%s)", e.detail)
        return HTMLResponse(_success_html(provider, result.email))
    return _bind(HTMLResponse(_onward_html(provider, result.email, url)), _connect_ring_cookie(request), state)


_INSTALLED_NOTICE = (
    "Return to OtoDock and click Refresh under your GitHub account to see the "
    "installation."
)


def no_connect_page(setup_action: str | None) -> HTMLResponse:
    """GitHub's return with no connect to finish (the app installed or
    changed from GitHub's own page)."""
    requested = (setup_action or "") == "request"
    title = "Installation requested" if requested else "Installed"
    return HTMLResponse(_notice_html(title, (
        "The organization's owner must approve the request on GitHub first. " if requested else ""
    ) + _INSTALLED_NOTICE))


@router.get("/v1/oauth/{provider}/app/setup")
async def app_setup(
    provider: str,
    request: Request,
    state: str = Query(None),
    setup_action: str = Query(None),
    installation_id: str = Query(None),
):
    """The app's setup URL: GitHub sends the browser here after an install
    (and after a repository change). With a live state this browser
    started, the state is consumed and the account's installations are
    re-listed with its own token (the ``installation_id`` GitHub appends is
    never trusted: the listing decides); otherwise a plain page says to
    Refresh in OtoDock, and nothing is spent."""
    try:
        provider = canonical_provider_id(provider)
    except KeyError:
        return HTMLResponse(_error_html("", "Unknown provider"), status_code=400)
    requested = (setup_action or "") == "request"
    title = "Installation requested" if requested else "Installed"
    notice = (
        "The organization's owner must approve the request on GitHub first. "
        if requested else ""
    ) + _INSTALLED_NOTICE
    identity = oauth_engine.peek_state_identity(state or "")
    if identity is None:
        return HTMLResponse(_notice_html(title, notice))
    if not identity[1] and not _connect_bound(_connect_ring_cookie(request), state or ""):
        oauth_engine.discard_state(state or "")
        return HTMLResponse(_notice_html(title, notice))
    ctx = oauth_engine.validate_state(state or "")
    if ctx is None or not ctx.install or ctx.provider_id != provider:
        return HTMLResponse(_notice_html(title, notice))
    label = ctx.account_label_hint
    try:
        meta = await asyncio.to_thread(
            github_apps.refresh_installations, ctx.user_sub, ctx.mcp_name, label,
        )
        await asyncio.to_thread(github_apps.relist_at_relay, ctx.user_sub, ctx.mcp_name, label)
    except Exception:  # the page reports; the card's Refresh retries
        logger.exception("GitHub app setup: the installations of %s were not listed", label)
        return HTMLResponse(_notice_html(title, notice))
    logger.info("GitHub app setup: %s/%s lists %d installation(s) (action=%s, id=%s)",
                ctx.mcp_name, label, len(meta.get("installations") or []),
                setup_action or "", installation_id or "")
    if requested:
        return HTMLResponse(_notice_html(title, notice))
    return HTMLResponse(_success_html(provider, str(meta.get("login") or label), title="Installed on GitHub"))


@router.post("/v1/oauth/{provider}/app/installations/refresh")
async def app_installations_refresh(
    provider: str,
    body: AppAccountRequest,
    user: UserContext = Depends(get_current_user),
):
    """Re-list the installations of one of the caller's GitHub-App accounts
    with its own token (the card's Refresh)."""
    require_human(user)
    _validate_provider_for_mcp(body.mcp_name, provider)
    await asyncio.to_thread(_own_github_app_account, user, body)
    key = (user.sub, body.mcp_name, body.account_label)
    now = time.monotonic()
    if now - _refresh_last.get(key, -_REFRESH_INTERVAL_S) < _REFRESH_INTERVAL_S:
        raise HTTPException(429, "Refreshed a moment ago; try again in a few seconds")
    _refresh_last[key] = now
    try:
        meta = await asyncio.to_thread(
            github_apps.refresh_installations, user.sub, body.mcp_name, body.account_label,
        )
    except OAuthTokenError as e:
        if e.code == INVALID_GRANT:
            raise HTTPException(409, "The account's token no longer works; reconnect it") from e
        raise HTTPException(503, "GitHub could not be reached; try again later") from e
    except relay_client.RelayNotConfigured as e:
        raise HTTPException(503, str(e)) from e
    except relay_client.RelayError as e:
        raise HTTPException(503, relay_client.relay_error_message(e.code)) from e
    except (httpx.HTTPError, OSError) as e:
        raise HTTPException(503, "The hosted relay could not be reached; try again later") from e
    await asyncio.to_thread(github_apps.relist_at_relay, user.sub, body.mcp_name, body.account_label)
    return {"status": "ok", "installations": meta.get("installations") or []}
