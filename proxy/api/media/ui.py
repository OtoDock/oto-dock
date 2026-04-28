"""display_ui artifact serving — ``GET /v1/ui/{token}``.

Serves an agent-authored HTML artifact into the dashboard's sandboxed iframe
(`UiArtifact`). Auth mirrors ``/v1/media/{token}``: the session cookie plus
the per-token provenance check in `api.media.access` — the sandboxed iframe's
document request carries same-site cookies (site-for-cookies computes over
the ancestor chain, not the frame's opaque origin; verified empirically), so
no frontend change is needed. A leaked token URL is useless without a
platform login.

Security model (full checklist in proxy/docs and the feature plan):

* The document executes at an **opaque origin**: the iframe embeds it with
  ``sandbox="allow-scripts"`` and the response itself carries the CSP
  ``sandbox allow-scripts`` directive, so even a direct top-level navigation
  to the token URL stays sandboxed — no dashboard cookies/storage/DOM ever.
* Because the origin is opaque, ``'self'`` matches nothing — the CSP must
  name the request's own **concrete origin** for the ``/ui-kit/*``
  subresources to load. Inline scripts are the feature (the agent writes
  ``<script>``), so ``script-src`` includes ``'unsafe-inline'`` by design;
  the CSP's job is to allow same-host kit + inline and raise the bar on
  egress (``connect-src 'none'`` etc.), not to police the agent's own code.
* EVERY response — success, placeholder, error — goes through the one
  header-baking helper ``_ui_response``: a single branch missing the CSP
  ``sandbox`` directive would render same-origin with the dashboard.

The stored file is the agent's RAW content; fragments are wrapped at serve
time (doctype + tokens CSS + runtime), so agents Read/Edit their artifact
cleanly and historical artifacts pick up wrapper improvements automatically.
A full document (leading ``<!doctype``/``<html``) is served verbatim —
documented as opting out of auto theme/kit/auto-height, NOT an injection
path (no splicing into agent markup).
"""

import html as html_escape
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

import config
from api.media.access import can_serve_token
from auth.providers import UserContext, get_current_user
from storage import database as task_store

logger = logging.getLogger("claude-proxy.ui")
router = APIRouter()


# The artifact runtime, injected into wrapped fragments:
#   * theme: ?theme=dark|light on load + live {source:"otodock-host",
#     type:"theme"} messages toggle `.dark` on <html> (runtime-side so the
#     serve-time wrap stays byte-identical for both themes).
#   * auto-height: rAF-throttled ResizeObserver posts {source:
#     "otodock-artifact", v:1, type:"height"} to the parent. targetOrigin is
#     "*" because the sandboxed child cannot know the parent's origin; the
#     payload carries nothing sensitive. The child-side throttle is
#     best-effort only — the REAL flood defense is the parent's delta-gate.
#   * backchannel: window.otodock.send(payload) posts an "action" message the
#     host delivers into the chat (consent + rate-limited + provenance-framed;
#     see ws/artifact_interactions.py). The host answers with an "action_ack"
#     message, re-fired in-frame as an `otodock:action-ack` window event
#     (detail: {status: sent|queued|blocked|denied|unavailable, reason}).
UI_RUNTIME = """<script>
(function(){
  var q = new URLSearchParams(location.search);
  if (q.get('theme') === 'dark') document.documentElement.classList.add('dark');
  addEventListener('message', function(e){
    if (!e.data || e.data.source !== 'otodock-host') return;
    if (e.data.type === 'theme'){
      document.documentElement.classList.toggle('dark', e.data.theme === 'dark');
      window.dispatchEvent(new Event('otodock:theme'));
    }
    if (e.data.type === 'action_ack'){
      try {
        window.dispatchEvent(new CustomEvent('otodock:action-ack', {
          detail: {status: String(e.data.status || ''), reason: String(e.data.reason || '')}
        }));
      } catch (err) {}
    }
  });
  var last = 0;
  function postH(){
    var h = document.documentElement.scrollHeight;
    if (Math.abs(h - last) < 2) return; last = h;
    parent.postMessage({source:'otodock-artifact', v:1, type:'height', height:h}, '*');
  }
  var raf = 0;
  new ResizeObserver(function(){
    if (raf) return;
    raf = requestAnimationFrame(function(){ raf = 0; postH(); });
  }).observe(document.documentElement);
  addEventListener('load', function(){ postH();
    parent.postMessage({source:'otodock-artifact', v:1, type:'ready'}, '*'); });
  window.otodock = { send: function(p){
    parent.postMessage({source:'otodock-artifact', v:1, type:'action', payload:p}, '*');
  } };
})();
// Swipe forwarding: the sandboxed frame swallows touches, so the dashboard's
// drawer gestures die over an artifact/app. Recognize the SAME horizontal
// swipe the host uses (useSwipeGesture's thresholds, incl. the
// horizontally-scrollable-ancestor skip) and post it up — the host validates
// the source window and routes it into its gesture bus. Full-document HTML
// is served verbatim (no runtime), so only fragment-authored content
// forwards; that matches the authoring contract for apps/artifacts.
(function(){
  var sx = 0, sy = 0, st = 0, on = false;
  function hscroll(t){
    var n = (t && t.nodeType === 1) ? t : null;
    while (n && n !== document.documentElement){
      if (n.scrollWidth > n.clientWidth + 2){
        var ox = getComputedStyle(n).overflowX;
        if (ox === 'auto' || ox === 'scroll') return true;
      }
      n = n.parentElement;
    }
    return false;
  }
  addEventListener('touchstart', function(e){
    if (e.touches.length > 1 || hscroll(e.target)) { on = false; return; }
    on = true;
    sx = e.touches[0].clientX; sy = e.touches[0].clientY; st = Date.now();
  }, {passive:true});
  addEventListener('touchend', function(e){
    if (!on) return;
    on = false;
    var t = e.changedTouches[0];
    if (!t) return;
    var dx = t.clientX - sx, dy = t.clientY - sy, el = Date.now() - st;
    if (el < 50 || el > 800) return;
    var ax = Math.abs(dx), ay = Math.abs(dy);
    if (ax < 50 || ax < ay * 1.5 || ax / el < 0.25) return;
    parent.postMessage({source:'otodock-artifact', v:1, type:'swipe',
      dir: dx > 0 ? 'right' : 'left'}, '*');
  }, {passive:true});
  addEventListener('touchcancel', function(){ on = false; }, {passive:true});
})();
</script>"""


def request_origin(request: Request) -> str:
    """The concrete origin the browser used for this request (also where the
    document's ``/ui-kit/*`` subresources resolve — the CSP pins it because a
    sandboxed document's origin is opaque, so ``'self'`` matches nothing).

    When the request's ``Host`` matches ``DASHBOARD_PUBLIC_URL``'s host, that
    URL's origin is used VERBATIM: behind a reverse-proxy chain
    (cloudflared → nginx → proxy) a dropped/re-set ``X-Forwarded-Proto``
    otherwise pins an ``http://`` CSP under an ``https://`` page and the
    browser blocks every kit subresource — artifacts render unstyled (found
    live on the trusted-VM install, 2026-07-10). All other accesses (LAN-IP
    dev boxes, secondary hostnames) derive from the request: ``Host`` header
    + ``X-Forwarded-Proto`` (first hop) falling back to the socket scheme."""
    host = (request.headers.get("host") or request.url.netloc or "").strip()
    pub = (config.DASHBOARD_PUBLIC_URL or "").strip().rstrip("/")
    if pub and host:
        from urllib.parse import urlsplit
        p = urlsplit(pub)
        if p.scheme in ("http", "https") and p.netloc \
                and host.lower() == p.netloc.lower():
            return f"{p.scheme}://{p.netloc}"
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = proto or request.url.scheme or "http"
    return f"{scheme}://{host}"


def _csp(origin: str) -> str:
    return (
        "sandbox allow-scripts; default-src 'none'; "
        f"script-src {origin} 'unsafe-inline'; style-src {origin} 'unsafe-inline'; "
        f"img-src {origin} data: blob:; font-src {origin} data:; "
        f"media-src {origin} data: blob:; connect-src 'none'; frame-src 'none'; "
        "object-src 'none'; form-action 'none'; base-uri 'none'; "
        "frame-ancestors 'self'"
    )


def _ui_response(body: str, origin: str, status_code: int = 200) -> HTMLResponse:
    """Bake the isolation headers onto EVERY branch of this route. Explicit,
    never middleware-`setdefault`: the opaque-origin sandbox and the
    top-level-open safety both live or die on these being present."""
    return HTMLResponse(
        content=body,
        status_code=status_code,
        headers={
            "Content-Security-Policy": _csp(origin),
            "X-Frame-Options": "SAMEORIGIN",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            # The file may be edited + re-displayed; always serve fresh.
            "Cache-Control": "no-store",
        },
    )


def is_full_document(content: str) -> bool:
    """Leading ``<!doctype``/``<html`` (after whitespace/comments,
    case-insensitive) marks a full document → served verbatim."""
    lead = content.lstrip()
    while lead.startswith("<!--"):
        end = lead.find("-->")
        if end < 0:
            return False
        lead = lead[end + 3:].lstrip()
    return lead[:16].lower().startswith(("<!doctype", "<html"))


def wrap_fragment(content: str, runtime_extra: str = "") -> str:
    """Serve-time wrapper for a body fragment: doctype + viewport + tokens
    CSS + runtime. The stored file stays the agent's raw content.
    ``runtime_extra`` lets the mini-app route append its STATIC action
    runtime — it must never carry per-row interpolation (script-assembly
    injection)."""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<link rel=\"stylesheet\" href=\"/ui-kit/otodock-tokens.css\">"
        f"{UI_RUNTIME}{runtime_extra}</head><body>{content}</body></html>"
    )


def _placeholder(message: str) -> str:
    """Styled in-frame notice (tokens-CSS look, theme-aware via the wrapper).
    Callers pass PRE-ESCAPED text only."""
    return wrap_fragment(
        f'<div class="card" style="text-align:center; margin:1rem">'
        f'<p class="muted" style="margin:0">{message}</p></div>'
    )


@router.get("/v1/ui/{token}")
async def serve_ui(
    token: str,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Serve a display_ui artifact by capability token (sandboxed on every
    branch — see `_ui_response`)."""
    origin = request_origin(request)
    if user is None:
        # Signed-out (or a leaked link opened outside the platform): styled
        # in-frame notice, still sandboxed.
        return _ui_response(
            _placeholder("Sign in to OtoDock to view this artifact."), origin, 401,
        )
    info = task_store.get_media_token(token)
    # A non-ui token is 404 here (and serve_media rejects ui tokens): the two
    # routes must never serve each other's rows — this one is the only place
    # text/html from that table may render, and only under the sandbox CSP.
    # Access-denied is the SAME 404 (no liveness oracle for leaked tokens).
    if (
        not info
        or (info.get("media_kind") or "") != "ui"
        or not can_serve_token(info, user)
    ):
        return _ui_response(_placeholder("This artifact no longer exists."), origin, 404)
    path = Path(info["abs_path"])
    if not path.is_file():
        name = html_escape.escape(path.name)
        return _ui_response(
            _placeholder(f"The artifact file <code>{name}</code> was deleted from the workspace."),
            origin, 404,
        )
    content = path.read_text(encoding="utf-8", errors="replace")
    if is_full_document(content):
        return _ui_response(content, origin)
    return _ui_response(wrap_fragment(content), origin)
