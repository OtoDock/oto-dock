"""Pinned apps — serve + CRUD + fire_task execution (``/v1/apps``).

Standing agent-authored dashboards: the registry row names a workspace
``.html`` file, served COOKIE-AUTHED (no capability tokens — a standing
surface has stable identity) into the same opaque-origin sandbox as
``/v1/ui`` (every helper reused from ``api.media.ui``, CSP on every
branch). Access rule mirrors ``can_serve_token`` discipline: personal rows
serve only their owner, shared rows anyone assigned to the agent; denied
is the SAME 404 as missing (no oracle).

Actions: buttons in app JS call ``otodock.action(id, args)`` — declared ids
only, validated against the user-approved manifest (api/apps/manifest.py).
fire_task and mcp_tool execute HERE; send_prompt rides the chat WS
(ws/dashboard_chat.py) with the backchannel authority downgrades. Page args
NEVER reach a prompt or a tool un-gated: fire_task substitutes them only
through a user-approved ``args_schema`` (schema-less = verbatim, args
rejected), and mcp_tool validates them against its schema then merges UNDER
the declared ``fixed_args`` before the headless executor
(services/apps/headless_exec.py) runs the one declared tool.
"""

import asyncio
import html as html_escape
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from core import placement
from api.apps import manifest as _mf
from auth import roles
from api.media.ui import (
    _placeholder,
    _ui_response,
    inject_runtime,
    is_full_document,
    request_origin,
    wrap_fragment,
)
from auth.providers import (
    UserContext,
    get_current_user,
    require_agent_access,
    require_auth,
    require_human,
)
from storage import database as task_store
from storage import db_apps
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy.apps")
router = APIRouter()

# Static action-runtime extension (NEVER per-row interpolation — see
# wrap_fragment). window.otodock exists: UI_RUNTIME defines it just above.
# otodock.action(id, args) returns a call id; calls issued within one
# macrotask coalesce into a single `app_actions` message the host runs as one
# batch, and every call ends in exactly one `action_result` (mirroring
# UI_RUNTIME's action_ack bridge: re-fired in-page as `otodock:action-result`
# with {id, ok, result, call_id} — pages that key on `id` keep working).
# otodock.feed(name, cb) subscribes to a declared read-only platform feed —
# the HOST answers from the viewer's authenticated context (`feed_update`
# messages: initial snapshot on subscribe, pushes on change); the frame
# itself still has no network. cb(rows, error) — error non-null when the
# feed is undeclared/unapproved/unavailable in this view.
# otodock.open(target) asks the host to move the viewer to a platform page
# by KIND and id ({kind:'chat', id} …; the host builds the route, never the
# page); acked in-page as `otodock:open-ack` {status, reason}.
# Live apps: the host posts `push` {payload, ts} (an agent's app_push) and
# `state` {doc, rev} (the state document, on load and on every write); the
# page sees them as `otodock:push` / `otodock:state` window events, and
# `otodock.state` / `otodock.stateRev` hold the latest delivery (-1 until
# the first one). The runtime lands AFTER a full document's own scripts, so
# pages listen for the window events rather than calling `otodock.onState`
# at parse time.
APP_RUNTIME = """<script>
window.otodock.action = function(id, args){
  var b = window.__otodockBatch = window.__otodockBatch || {calls: [], seq: 0, timer: 0};
  var callId = 'c' + (++b.seq);
  b.calls.push({call_id: callId, id: String(id||''), args: (args===undefined?null:args)});
  if (!b.timer){
    b.timer = setTimeout(function(){
      var calls = b.calls; b.calls = []; b.timer = 0;
      parent.postMessage({source:'otodock-artifact', v:1, type:'app_actions',
        calls: calls}, '*');
    }, 0);
  }
  return callId;
};
window.otodock.feed = function(name, cb){
  var n = String(name||'');
  var reg = window.__otodockFeeds = window.__otodockFeeds || {};
  (reg[n] = reg[n] || []).push(typeof cb === 'function' ? cb : function(){});
  parent.postMessage({source:'otodock-artifact', v:1, type:'feed_subscribe',
    feed:n}, '*');
};
window.otodock.open = function(target){
  parent.postMessage({source:'otodock-artifact', v:1, type:'open_target',
    target: (target && typeof target === 'object') ? target : {}}, '*');
};
// otodock.platform(method, args): a declared platform method (an action of
// type "platform"); resolves with the platform's answer, rejects with the
// reason. The host answers as the viewer; the frame never talks to it.
window.otodock.platform = function(method, args){
  var reg = window.__otodockPlatform = window.__otodockPlatform || {seq: 0, pending: {}};
  var callId = 'p' + (++reg.seq);
  return new Promise(function(resolve, reject){
    reg.pending[callId] = {resolve: resolve, reject: reject};
    parent.postMessage({source:'otodock-artifact', v:1, type:'platform_call',
      call_id: callId, method: String(method||''), args: (args===undefined?null:args)}, '*');
  });
};
window.otodock.state = {};
window.otodock.stateRev = -1;
window.otodock.sharedLink = null;
window.otodock.onPush = function(cb){
  addEventListener('otodock:push', function(e){
    try { cb(e.detail.payload, e.detail); } catch (err) {}
  });
};
window.otodock.onState = function(cb){
  addEventListener('otodock:state', function(e){
    try { cb(e.detail.doc, e.detail.rev); } catch (err) {}
  });
};
// Accounts kept by the host (APPS.md "External links"): on a link the host
// keeps the app's own session token in a cookie of its own and tells the
// page through `app_session` (after every viewer_token, and after a set or
// a clear); otodock.session.set(token) / .clear() ask the host and resolve
// with {token, status} — `unavailable` on the dashboard, where a viewer
// has an identity already. The app reads the session from the verified
// claim (`session`), never from a header the page sets.
window.otodock.session = {token: null, _pending: []};
function otodockSessionAsk(msg){
  return new Promise(function(resolve){
    window.otodock.session._pending.push(resolve);
    parent.postMessage(Object.assign({source:'otodock-artifact', v:1}, msg), '*');
    setTimeout(function(){
      var i = window.otodock.session._pending.indexOf(resolve);
      if (i >= 0){ window.otodock.session._pending.splice(i, 1); resolve({token: window.otodock.session.token, status: 'timeout'}); }
    }, 15000);
  });
}
window.otodock.session.set = function(t){ return otodockSessionAsk({type: 'session_set', token: String(t || '')}); };
window.otodock.session.clear = function(){ return otodockSessionAsk({type: 'session_clear'}); };
// otodock.challenge(): the host renders the bot check over the page and
// resolves with a token the page sends on its own request as
// X-OtoDock-Challenge (the proxy verifies it once and never forwards it);
// null when the host has none (the dashboard, an install without one) or
// the person gave up.
window.otodock.challenge = function(){
  var reg = window.__otodockChallenge = window.__otodockChallenge || {seq: 0, pending: {}};
  var callId = 'h' + (++reg.seq);
  return new Promise(function(resolve){
    reg.pending[callId] = resolve;
    parent.postMessage({source:'otodock-artifact', v:1, type:'challenge', call_id: callId}, '*');
    setTimeout(function(){ if (reg.pending[callId]){ delete reg.pending[callId]; resolve(null); } }, 120000);
  });
};
// otodock.openExternal(url): the host opens an absolute http(s) URL in a
// new tab — on a link only a host the manifest declares under
// external.links, on the dashboard behind its consent chip unless declared
// — from a user gesture; the frame itself never navigates. Resolves with
// {status: opened|blocked|denied, reason}. The <a href> bridge posts the
// same `open_url` without a call id and is acked by the window event alone.
window.otodock.openExternal = function(url){
  var reg = window.__otodockOpen = window.__otodockOpen || {seq: 0, pending: {}};
  var callId = 'o' + (++reg.seq);
  return new Promise(function(resolve){
    reg.pending[callId] = resolve;
    parent.postMessage({source:'otodock-artifact', v:1, type:'open_url', url: String(url || ''), call_id: callId}, '*');
    setTimeout(function(){ if (reg.pending[callId]){ delete reg.pending[callId]; resolve({status: 'denied', reason: 'no answer'}); } }, 15000);
  });
};
addEventListener('message', function(e){
  if (!e.data || e.data.source !== 'otodock-host') return;
  if (e.data.type === 'app_session'){
    var sst = String(e.data.status || (e.data.token ? 'ok' : 'none'));
    window.otodock.session.token = e.data.token ? String(e.data.token) : null;
    var spend = window.otodock.session._pending; window.otodock.session._pending = [];
    for (var si = 0; si < spend.length; si++){ try { spend[si]({token: window.otodock.session.token, status: sst}); } catch (err) {} }
    try { window.dispatchEvent(new CustomEvent('otodock:session', {detail: {token: window.otodock.session.token, status: sst}})); } catch (err) {}
  }
  if (e.data.type === 'challenge_result'){
    var creg = window.__otodockChallenge || {pending: {}};
    var cr = creg.pending[String(e.data.call_id || '')];
    if (cr){ delete creg.pending[String(e.data.call_id || '')]; try { cr(e.data.token ? String(e.data.token) : null); } catch (err) {} }
  }
  if (e.data.type === 'open_url_ack' && e.data.call_id){
    var oreg = window.__otodockOpen || {pending: {}};
    var op = oreg.pending[String(e.data.call_id || '')];
    if (op){ delete oreg.pending[String(e.data.call_id || '')]; try { op({status: String(e.data.status || ''), reason: String(e.data.reason || '')}); } catch (err) {} }
  }
  if (e.data.type === 'push'){
    try {
      window.dispatchEvent(new CustomEvent('otodock:push', {
        detail: {payload: e.data.payload, ts: Number(e.data.ts || 0)}
      }));
    } catch (err) {}
  }
  if (e.data.type === 'state'){
    var rev = Number(e.data.rev || 0);
    if (rev > window.otodock.stateRev){
      window.otodock.stateRev = rev;
      window.otodock.state = (e.data.doc && typeof e.data.doc === 'object') ? e.data.doc : {};
      try {
        window.dispatchEvent(new CustomEvent('otodock:state', {
          detail: {doc: window.otodock.state, rev: rev}
        }));
      } catch (err) {}
    }
  }
  if (e.data.type === 'shared_link'){
    // Opened through an external link: the page may hide what a link
    // cannot do (buttons unless `actions`, and never feeds or navigation);
    // `links` are the hosts otodock.openExternal may open from here, `url`
    // the link's own address without its query (what a vendor's return
    // URL points back to), `query` the link page's own query string (data
    // — a Stripe success_url comes back to the link with it).
    var lk = Array.isArray(e.data.links) ? e.data.links.filter(function(h){ return typeof h === 'string'; }) : [];
    window.otodock.sharedLink = {actions: !!e.data.actions, links: lk,
      url: String(e.data.url || '').slice(0, 512), query: String(e.data.query || '').slice(0, 512)};
    try {
      window.dispatchEvent(new CustomEvent('otodock:shared-link', {detail: window.otodock.sharedLink}));
    } catch (err) {}
  }
  if (e.data.type === 'action_result'){
    try {
      window.dispatchEvent(new CustomEvent('otodock:action-result', {
        detail: {id: String(e.data.id || ''), ok: !!e.data.ok,
                 result: String(e.data.result || ''),
                 call_id: String(e.data.call_id || '')}
      }));
    } catch (err) {}
  }
  if (e.data.type === 'open_ack'){
    try {
      window.dispatchEvent(new CustomEvent('otodock:open-ack', {
        detail: {status: String(e.data.status || ''), reason: String(e.data.reason || '')}
      }));
    } catch (err) {}
  }
  if (e.data.type === 'feed_update'){
    var subs = (window.__otodockFeeds || {})[String(e.data.feed || '')] || [];
    for (var i = 0; i < subs.length; i++){
      try { subs[i](e.data.rows || [], e.data.error || null); } catch (err) {}
    }
  }
  if (e.data.type === 'platform_result'){
    var preg = window.__otodockPlatform || {pending: {}};
    var p = preg.pending[String(e.data.call_id || '')];
    if (p){
      delete preg.pending[String(e.data.call_id || '')];
      try {
        if (e.data.ok) p.resolve(e.data.result);
        else p.reject(new Error(String(e.data.reason || 'unavailable')));
      } catch (err) {}
    }
  }
});
// Content-height reporting: hosts that size the frame to its content (the
// Dock's single-scroll layout) listen for this; fixed-height hosts ignore it.
// ResizeObserver catches feed-driven re-renders; 'load' covers the initial
// paint on browsers that fire RO before layout settles.
(function(){
  var last = 0;
  function report(){
    var d = document.documentElement;
    var h = Math.ceil(Math.max(d ? d.scrollHeight : 0,
                               document.body ? document.body.scrollHeight : 0));
    if (h > 0 && Math.abs(h - last) > 2){
      last = h;
      parent.postMessage({source:'otodock-artifact', v:1, type:'content_height',
        height:h}, '*');
    }
  }
  if (typeof ResizeObserver === 'function'){
    new ResizeObserver(report).observe(document.documentElement);
  } else {
    setInterval(report, 2000);
  }
  addEventListener('load', report);
})();
// Apps with a server (APPS.md): the host posts `viewer_token` after the
// page's `ready`; otodock.fetch(path, init) calls the app's own API under
// its base (derived from this document's path — the runtime stays static)
// with the token, credentials omitted, and waits through a 503 while the
// server starts; otodock.ws(path) opens the bridge, sends the auth frame
// first, re-sends it on rotation and reconnects with backoff.
(function(){
  var m = location.pathname.match(/^(\\/v1\\/apps\\/[0-9a-f-]{36}|\\/s\\/[A-Za-z0-9_-]+)\\//);
  var base = m ? m[1] : '';
  var tok = {value: '', exp: 0};
  var waiters = [];
  var sockets = [];
  window.otodock.appBase = base;
  window.otodock.viewerToken = function(){ return tok.value; };
  addEventListener('message', function(e){
    if (!e.data || e.data.source !== 'otodock-host' || e.data.type !== 'viewer_token') return;
    tok.value = String(e.data.token || ''); tok.exp = Number(e.data.exp || 0);
    while (waiters.length) { try { waiters.shift()(); } catch (err) {} }
    for (var i = 0; i < sockets.length; i++) { try { sockets[i]._auth(); } catch (err) {} }
    try { window.dispatchEvent(new CustomEvent('otodock:viewer-token', {detail: {exp: tok.exp}})); } catch (err) {}
  });
  function ready(){ return tok.value ? Promise.resolve() : new Promise(function(r){ waiters.push(r); }); }
  function status(state, retry){
    parent.postMessage({source:'otodock-artifact', v:1, type:'server_status', state: state, retry_after: retry || 0}, '*');
  }
  window.otodock.fetch = function(path, init){
    init = init || {};
    var p = String(path || '/');
    var url = base + '/api' + (p.charAt(0) === '/' ? p : '/' + p);
    var started = Date.now();
    return ready().then(function(){
      var h = new Headers(init.headers || {});
      h.set('Authorization', 'Bearer ' + tok.value);
      var opts = Object.assign({}, init, {headers: h, credentials: 'omit'});
      function attempt(){
        return fetch(url, opts).then(function(r){
          if (r.status === 503 && Date.now() - started < 30000){
            var ra = Number(r.headers.get('Retry-After') || 2);
            status(r.headers.get('X-OtoDock-Server') || 'starting', ra);
            return new Promise(function(res){ setTimeout(res, Math.min(ra, 5) * 1000); }).then(attempt);
          }
          status(r.status === 503 ? 'failed' : 'up', 0);
          return r;
        });
      }
      return attempt();
    });
  };
  // ONE socket per path, reused on every later call for it: this wrapper
  // reconnects with its own backoff, so a page that wraps a reconnect of
  // its own around it doubles the sockets on every close until the
  // platform's per-viewer cap refuses them all and the app looks
  // permanently offline (found live, 2026-09-13). Reuse makes that
  // impossible instead of merely bounded.
  var byPath = {};
  window.otodock.ws = function(path){
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    var p = path ? String(path) : '';
    var key = p ? (p.charAt(0) === '/' ? p : '/' + p) : '/';
    var held = byPath[key];
    if (held && !held._closed) return held;
    var url = proto + location.host + base + '/ws' + (p ? (p.charAt(0) === '/' ? p : '/' + p) : '');
    var w = {readyState: 0, onopen: null, onmessage: null, onclose: null, _q: [], _ws: null, _closed: false};
    var backoff = 1000;
    w._auth = function(){
      if (w._ws && w._ws.readyState === 1 && tok.value) w._ws.send(JSON.stringify({type: 'auth', token: tok.value}));
    };
    function open(){
      if (w._closed) return;
      ready().then(function(){
        if (w._closed) return;
        var s = new WebSocket(url); w._ws = s;
        s.onopen = function(){
          s.send(JSON.stringify({type: 'auth', token: tok.value}));
          w.readyState = 1; backoff = 1000;
          while (w._q.length) s.send(w._q.shift());
          if (w.onopen) w.onopen();
        };
        s.onmessage = function(ev){ if (w.onmessage) w.onmessage(ev); };
        s.onclose = function(ev){
          w.readyState = 3;
          if (w.onclose) w.onclose(ev);
          if (w._closed) return;
          if (ev.code === 4401){ tok.value = ''; parent.postMessage({source:'otodock-artifact', v:1, type:'viewer_token_expired'}, '*'); }
          if (ev.code === 4429){ try { console.warn('otodock.ws: the platform refused another socket for this app. This socket reconnects on its own — a page must not open or reconnect it a second time.'); } catch (err) {} }
          setTimeout(open, backoff); backoff = Math.min(backoff * 2, 30000);
        };
      });
    }
    w.send = function(d){ if (w._ws && w._ws.readyState === 1) w._ws.send(d); else w._q.push(d); };
    w.close = function(){ w._closed = true; delete byPath[key]; if (w._ws) w._ws.close(); };
    byPath[key] = w; sockets.push(w); open();
    return w;
  };
})();
</script>"""

# Minimum seconds between fires of the same button by the same user. Module
# level so it survives reconnects; the task dedup guard bounds concurrency.
_FIRE_MIN_INTERVAL_S = 2.0
_fire_rate: dict[tuple[str, str, str], float] = {}


def _check_fire_rate(app_id: str, action_id: str, sub: str,
                     args_key: str = "",
                     interval: float = _FIRE_MIN_INTERVAL_S) -> None:
    """Min-interval per (app, action[, args], user). ``args_key``
    distinguishes DIFFERENT parameter values of one declared action — a
    control panel often shares one schema-bound action across many widgets
    (one ``toggle`` action, entity in args), and keying without the args
    made pressing two different lights look like hammering one button.
    Same args stay limited (a toggle double-press must not fire twice)."""
    key = (app_id, f"{action_id}|{args_key}" if args_key else action_id, sub)
    now = time.monotonic()
    # A key never seen is not "fired at zero": the monotonic clock starts at
    # boot, so that reading refused every first press for ``interval`` after
    # a host restart (ten minutes for a warm).
    last = _fire_rate.get(key)
    if last is not None and now - last < interval:
        raise HTTPException(status_code=429, detail="Too fast — try again in a moment")
    _fire_rate[key] = now
    if len(_fire_rate) > 1024:
        for k in [k for k, t in _fire_rate.items() if now - t > 300]:
            _fire_rate.pop(k, None)


def _manifest_target_error(row: dict, u: UserContext) -> str:
    """Why ``u`` may not approve the row's manifest as it stands: an MCP
    the agent lacks, a task target that is gone or one they may not run.
    Empty when every target holds. Sync — call off the loop."""
    available_mcps: dict[str, str] | None = None
    for a in _mf.parse_actions(row):
        if a.get("type") == "mcp_tool":
            if available_mcps is None:
                available_mcps = _mf.assigned_mcp_keys(row["agent"])
            if available_mcps.get(a.get("mcp") or "") != a.get("mcp"):
                return f"action {a.get('id')!r}: its MCP is not available on this agent"
            continue
        if a.get("type") != "fire_task":
            continue
        err = _mf.check_task_target(
            a.get("task_id") or "", row["agent"], shared=not row.get("username"),
        )
        if err:
            return err
        dyn = task_store.get_dynamic_task(a.get("task_id") or "")
        if not _mf.user_can_run_task(u, dyn or {}):
            return f"action {a.get('id')!r}: you lack run authority for its task"
    return ""


def app_access(row: dict, user: UserContext) -> bool:
    """May ``user`` see/serve this app? Personal rows → owner only; shared
    rows → anyone assigned to the agent; a live internal share grants
    either (SHARING.md); admin always. A chat- or project-scoped pin is
    additionally reachable only from its scope (``_scope_access``), grant or
    not. Reads the DB for scoped rows and for non-member callers — call it
    off the loop (``_visible_row``)."""
    # A session acts on the agent it was minted for and never across one,
    # whoever drives it (the app API's agent basis keeps the same rule): its
    # bearer resolves to its user, who may belong to other agents too.
    if user.is_session and (row.get("agent") or "") != (user.agent or ""):
        return False
    if user.is_admin:
        return True
    # A personal app whose owner lost the agent is dormant (APPS.md
    # "Lifecycle"): to the owner, a grantee and the render alike it is gone.
    if db_apps.personal_row_dormant(row):
        return False
    # The platform's own headless render sees the one app it was minted for
    # (auth/render_principal.py), whatever the row's scope.
    if user.render_app and user.render_app == row.get("id"):
        return True
    if row.get("username"):
        direct = (row.get("owner_sub") or "") == user.sub
    else:
        direct = user.can_access_agent(row.get("agent") or "")
    if not direct and not _granted(row, user):
        return False
    return _scope_access(row, user)


def _granted(row: dict, user: UserContext) -> bool:
    """A live share admits the viewer: their own person share, or an agent
    or department placement on an agent they hold (SHARING.md)."""
    from storage.sharing import share_store
    app_id = row.get("id") or ""
    if share_store.internal_grant("app", app_id, user.sub) is not None:
        return True
    return bool(share_store.placements_for_user(app_id, user.sub, list(user.agents)))


def _viewer_share(row: dict, user: UserContext) -> tuple[dict | None, dict | None]:
    """How a share reaches this viewer, for the page's words: their person
    share (``granted``), and the placement with the agent it sits in for
    them: in the agent they placed their own share in, the row the Apps
    panel shows there (an agent share, then a department share, then their
    own: ``share_store._merged``), so the page offers that row's menu;
    otherwise the strongest agent or department placement on an agent they
    hold. None, None for an owner, a member or an admin."""
    from storage.sharing import share_store
    if user.is_admin:
        return None, None
    if row.get("username"):
        if (row.get("owner_sub") or "") == user.sub:
            return None, None
    elif user.can_access_agent(row.get("agent") or ""):
        return None, None
    app_id = row.get("id") or ""
    grant = share_store.internal_grant("app", app_id, user.sub)
    placed = share_store.placements_for_user(app_id, user.sub, list(user.agents))
    own = (grant.get("placed_agent") or "") if grant else ""
    if own and not user.can_access_agent(own):
        own = ""
    team = sorted((p for p in placed if p["agent"] == own),
                  key=lambda p: p["kind"] != share_store.AGENT) if own else []
    placement = None
    if own and not team:
        placement = {"kind": grant["grantee_kind"], "share_id": grant["id"],
                     "agent": own, "from_agent": row.get("agent") or "",
                     "role_cap": grant["role_cap"], "shared_by": grant.get("created_by") or ""}
    elif team or placed:
        p = team[0] if team else placed[0]
        placement = {"kind": p["kind"], "share_id": p["share_id"],
                     "agent": p["agent"], "from_agent": row.get("agent") or "",
                     "role_cap": p["role_cap"], "shared_by": p.get("shared_by") or ""}
    return grant, placement


def _scope_access(row: dict, user: UserContext) -> bool:
    """A Dock pin follows its chat's access rule: the chat itself, or for a
    project pin any lane of the project the viewer may open. Without this a
    dashboard pinned in a private chat would serve to any member holding
    its id (the Dock route checked the anchor chat; the app routes did
    not)."""
    chat_id = row.get("scope_chat_id") or ""
    project_id = row.get("scope_project_id") or ""
    if not chat_id and not project_id:
        return True
    from api.agents.chats import can_access_chat
    if chat_id:
        chat = task_store.get_chat(chat_id)
        return bool(chat) and can_access_chat(user, chat)
    return any(can_access_chat(user, c)
               for c in task_store.list_chats_by_project(project_id))


def _visible_row(app_id: str, user: UserContext) -> dict | None:
    """The row iff it exists, is not soft-unpinned and ``user`` may reach
    it — every other outcome is the same absence (no oracle). Synchronous:
    call via ``asyncio.to_thread``."""
    row = task_store.get_app(app_id)
    if not row or row.get("hidden") or not app_access(row, user):
        return None
    return row


def _viewer_username(user: UserContext) -> str:
    u = task_store.get_user(user.sub)
    return (u.get("username") or "") if u else ""


def _can_approve_surface(row: dict, user: UserContext) -> bool:
    """The APP surface of approval authority (the task surface is checked
    per fire_task action). A render principal counts for its own app: it is
    served the scratch copy the check cut (the approval routes themselves
    are closed to it by the render confinement)."""
    if user.is_admin:
        return True
    if user.render_app and user.render_app == row.get("id"):
        return True
    if row.get("username"):
        return (row.get("owner_sub") or "") == user.sub
    return user.can_edit_agent(row.get("agent") or "")


def _can_manage(row: dict, user: UserContext) -> bool:
    """Unpin / reorder authority for this row."""
    return _can_approve_surface(row, user)


def _app_document(row: dict, user: UserContext, preview: bool) -> tuple[str, str]:
    """``(kind, content)`` for the serve route: the release copy (verified
    against its hash) unless the row has none, or the owner or an editor
    asked for a preview of the working file; ``missing`` when the working
    file is gone, ``too_large`` when it is over ``FILE_APP_MAX_BYTES``,
    ``unreadable`` when it is not a regular file inside the agent's tree (a
    link is never followed), ``damaged`` when the release no longer
    matches. Sync."""
    from services.apps import releases
    from services.infra import safe_fs
    if row.get("release_path") and not (preview and _can_approve_surface(row, user)):
        try:
            data = releases.read_release(row)
        except releases.ReleaseDamaged:
            return "damaged", ""
        if data is not None:
            return "ok", data.decode("utf-8", "replace")
    name = (row.get("rel_path") or "").rsplit("/", 1)[-1]
    try:
        return "ok", releases.read_working_file(row).decode("utf-8", "replace")
    except FileNotFoundError:
        return "missing", name
    except safe_fs.FileTooLarge:
        return "too_large", name
    except OSError:
        return "unreadable", name


@router.get("/v1/apps/{app_id}/html")
async def serve_app(
    app_id: str,
    request: Request,
    preview: int = 0,
    user: UserContext | None = Depends(get_current_user),
):
    """Serve a pinned app (sandboxed on every branch — see
    ``api.media.ui._ui_response``): the release copy made at pin time,
    never the working file the agent edits; ``?preview=1`` serves the
    working file to the owner or an editor and the release, silently, to
    anyone else (APPS.md "Releases and rollback")."""
    origin = request_origin(request)
    if user is None:
        return _ui_response(
            _placeholder("Sign in to OtoDock to view this app."), origin, 401,
        )
    # Access-denied is the SAME 404 as missing (no liveness oracle); a
    # soft-unpinned row is gone to every viewer surface.
    row = await asyncio.to_thread(_visible_row, app_id, user)
    if not row:
        return _ui_response(_placeholder("This app no longer exists."), origin, 404)
    if db_apps.app_kind_of(row).serves_tree:
        # A folder app's document (its assets resolve under the hashed
        # client prefix the frame normally loads; this path serves the
        # same document for hosts that key on it).
        from api.apps.app_proxy import folder_document
        return await folder_document(row, user, request, preview=bool(preview))
    kind, content = await asyncio.to_thread(_app_document, row, user, bool(preview))
    if kind == "missing":
        name = html_escape.escape(content)
        return _ui_response(
            _placeholder(f"The app file <code>{name}</code> was deleted from the workspace."),
            origin, 404,
        )
    if kind == "too_large":
        from services.apps import releases
        name = html_escape.escape(content)
        return _ui_response(
            _placeholder(f"The app file <code>{name}</code> is larger than "
                         f"{releases.FILE_APP_MAX_BYTES // (1024 * 1024)} MB, the most a "
                         "single-file app can be."),
            origin, 404,
        )
    if kind == "unreadable":
        name = html_escape.escape(content)
        return _ui_response(
            _placeholder(f"The app file <code>{name}</code> is not a regular file the platform "
                         "can show. Ask the agent to pin it again."),
            origin, 404,
        )
    if kind == "damaged":
        return _ui_response(
            _placeholder("This app's release copy is damaged — ask the agent to pin it again."),
            origin, 404,
        )
    if is_full_document(content):
        return _ui_response(inject_runtime(content, runtime_extra=APP_RUNTIME), origin)
    return _ui_response(wrap_fragment(content, runtime_extra=APP_RUNTIME), origin)


# ── Releases (APPS.md "Releases and rollback") ────────────────────────
# One lock per row: a deploy is a copy plus one UPDATE of the pointer; two
# pins of the same app a moment apart must not interleave them.
_deploy_locks: dict[str, asyncio.Lock] = {}


def _deploy_lock(app_id: str) -> asyncio.Lock:
    return _deploy_locks.setdefault(app_id, asyncio.Lock())


async def cut_and_point(row: dict, source) -> dict:
    """Copy the working file into the next release and point the row at it;
    returns the fresh row."""
    from services.apps import releases
    async with _deploy_lock(row["id"]):
        try:
            rel, sha = await asyncio.to_thread(releases.cut_release, row, source)
        except releases.ReleaseInvalid as e:
            raise HTTPException(status_code=400, detail=f"{e.reason}: pin it again with html")
        fresh = await asyncio.to_thread(task_store.set_app_release, row["id"], rel, sha)
    return fresh or row


async def announce_deploy(row: dict, release: int, *, file_updated: bool = True) -> int:
    """Tell every screen that may see the row that a new release serves:
    ``app_deployed`` on the live queue (the frame reloads once its row
    carries this manifest signature) and, for dashboards built before the
    frame existed, the working file's ``file_updated``."""
    from services.apps.audience import app_audience
    from services.notifications import notification_manager
    if file_updated:
        await notification_manager.broadcast_file_updated(
            row["agent"], row["rel_path"], source="disk",
        )
    frame = {"type": wire.APP_DEPLOYED, "app_id": row["id"], "release": release,
             "actions_sig": task_store.manifest_sig(row)}
    subs = await asyncio.to_thread(app_audience, row)
    return sum(notification_manager.push_live(sub, frame) for sub in subs)


async def rollback_app_row(row: dict) -> dict:
    """Point the row at the release before the current one; 409 when there
    is none. Shared by the REST route and the session hook."""
    from services.apps import releases
    if db_apps.app_kind_of(row).keeps_data:
        # A folder app rolls its database back with its code (APPS.md).
        from services.apps import app_deploy
        async with _deploy_lock(row["id"]):
            try:
                return await app_deploy.rollback_folder(row)
            except app_deploy.DeployError as e:
                raise HTTPException(status_code=409, detail=str(e))
    async with _deploy_lock(row["id"]):
        prev = await asyncio.to_thread(releases.previous_number, row)
        if prev is None:
            raise HTTPException(status_code=409, detail="no previous release")
        rel, sha = await asyncio.to_thread(releases.point_to, row, prev)
        fresh = await asyncio.to_thread(task_store.set_app_release, row["id"], rel, sha) or row
    screens = await announce_deploy(fresh, prev)
    logger.info(f"App rolled back: app={row.get('slug')}, release={prev}, screens={screens}")
    return {"release": prev, "screens": screens}


@router.post("/v1/apps/{app_id}/rollback")
async def rollback_app(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """The menu's Roll back: whoever may manage the row, a human at the
    keyboard (``require_human``)."""
    u = require_human(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_manage(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to roll back this app")
    return {"status": "ok", **await rollback_app_row(row)}


# ── Folder deploys waiting for a human (APPS.md "Deploy pipeline") ─────────


@router.get("/v1/apps/{app_id}/deploy/status")
async def deploy_status(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """The deploy state, the pending release and what it changes — what the
    card shows whoever may see the app."""
    from services.apps import app_deploy
    u = require_auth(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    return await asyncio.to_thread(app_deploy.status, row)


class DeployApproveRequest(BaseModel):
    release: int | None = None
    sig: str | None = None


@router.post("/v1/apps/{app_id}/deploy/approve")
async def approve_deploy(
    app_id: str,
    req: DeployApproveRequest | None = None,
    user: UserContext | None = Depends(get_current_user),
):
    """Take the pending release live; the manifest it carries becomes the
    approved one in the same click. A person at the keyboard with the
    approval authority (``require_human``, the owner or an editor). The
    body names the release and the manifest sig the card RENDERED: a deploy
    while a release waits replaces the waiting copy and its manifest, so
    without them the click would approve whatever the agent shipped since
    the card was drawn (409, like the plain approve route)."""
    from services.apps import app_deploy
    u = require_human(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_approve_surface(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to approve this app's release")
    if req is not None and (
            (req.release is not None and req.release != int(row.get("pending_release") or 0))
            or (req.sig is not None and req.sig != task_store.manifest_sig(row))):
        raise HTTPException(status_code=409, detail="The release changed — review it again")
    err = await asyncio.to_thread(_manifest_target_error, row, u)
    if err:
        raise HTTPException(status_code=409, detail=err)
    try:
        return {"status": "ok", **await app_deploy.approve_pending(
            row, u.sub, expect_sig=req.sig if req else None,
            expect_release=req.release if req else None)}
    except app_deploy.DeployError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/v1/apps/{app_id}/deploy/reject")
async def reject_deploy(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    from services.apps import app_deploy
    u = require_human(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_approve_surface(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to reject this app's release")
    try:
        return await app_deploy.reject_pending(row)
    except app_deploy.DeployError as e:
        raise HTTPException(status_code=409, detail=str(e))


def shape_app_rows(rows: list[dict], u: UserContext) -> list[dict]:
    """Registry rows → the client app shape (approval/staleness derived
    live). Shared by the standing list below and the chat Dock pins route
    (``api/agents/chats.py``) so scoped pins carry the exact same approval
    semantics. Synchronous — call via ``asyncio.to_thread``."""
    # Keyed per agent: the pins route may mix rows from different agents
    # (a project spans agents), and mcp availability is per-agent.
    from services.apps import releases
    mcps_by_agent: dict[str, dict[str, str]] = {}
    out = []
    for row in rows:
        agent = row.get("agent") or ""
        actions = _mf.parse_actions(row)
        requires = _mf.parse_requires(row)
        approved = task_store.app_actions_approved(row)
        stale = False
        has_mcp_tool = any(a.get("type") == "mcp_tool" for a in actions)
        if has_mcp_tool and agent not in mcps_by_agent:
            mcps_by_agent[agent] = _mf.assigned_mcp_keys(agent)
        for a in actions:
            if a.get("type") == "mcp_tool":
                a["mcp_available"] = mcps_by_agent.get(agent, {}).get(
                    a.get("mcp") or "") == a.get("mcp")
        if approved and actions:
            for a in actions:
                if a.get("type") != "fire_task":
                    continue
                dyn = task_store.get_dynamic_task(a.get("task_id") or "")
                a["task_name"] = (dyn or {}).get("name") or ""
                if not dyn or not _mf.sub_can_run_task(row.get("approved_by") or "", dyn):
                    stale = True
            # mcp_tool runs on the APPROVER's standing surface authority —
            # a demoted approver stales the whole approval (mirrors the
            # exec-time re-check).
            if has_mcp_tool and not _mf.sub_can_approve_surface(
                    row.get("approved_by") or "", row):
                stale = True
        # A placed row (SHARING.md) is never managed here (its management is
        # the home agent's); its viewer_role is the role every floor judges,
        # the strongest share admitting the viewer, whichever panel lists it.
        placed = row.get("placement")
        can_approve = _can_approve_surface(row, u) and not placed
        steps = _mf.parse_steps(row)
        # A shared app whose steps receive the agent's service accounts is
        # approved by a manager only (APPS.md "Steps": the token leaves the
        # platform with the script); an editor's approval would run the
        # scripts with no token, so the card says who may approve instead.
        steps_need_manager = bool(steps and not row.get("username")
                                  and (requires.get("providers") or []))
        if steps_need_manager and can_approve and not u.can_manage_agent(agent):
            can_approve = False
        if can_approve:
            for a in actions:
                if a.get("type") == "mcp_tool" and not a.get("mcp_available"):
                    can_approve = False
                if a.get("type") != "fire_task":
                    continue
                dyn = task_store.get_dynamic_task(a.get("task_id") or "")
                if "task_name" not in a:
                    a["task_name"] = (dyn or {}).get("name") or ""
                if not dyn or not _mf.user_can_run_task(u, dyn):
                    can_approve = False
        out.append({
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            # The row's own agent: a placed row's frame and cards subscribe
            # to it, never to the panel's host.
            "agent": agent,
            "scope": db_apps.app_scope(row.get("username")),
            "pin_scope": ("chat" if row.get("scope_chat_id")
                          else "project" if row.get("scope_project_id")
                          else "standing"),
            "position": row["position"],
            "rel_path": row["rel_path"],
            "updated_at": row["updated_at"],
            "actions": actions,
            "actions_sig": task_store.manifest_sig(row),
            "actions_approved": approved and not stale,
            "approval_stale": stale,
            "can_approve": can_approve,
            "can_manage": _can_manage(row, u) and not placed,
            # The role this viewer's action floors are judged against
            # (``min_role``); the host page hides nothing but can say why.
            "viewer_role": _mf.caller_role(row, u),
            # S2: this viewer parked the shared row off their own strip
            # (rows still return — the client's hidden affordance restores).
            "hidden_for_me": bool(row.get("hidden_for_me")),
            # Another user's personal app the viewer holds a share on
            # (SHARING.md): a row of their "Shared with you", never managed.
            "granted": bool(row.get("granted")),
            # The release served (0 = the working file) and whether the
            # menu may offer Roll back.
            "release": releases.current_number(row),
            "has_previous_release": releases.previous_number(row) is not None,
            # The manifest's other blocks, for the card's words (APPS.md
            # "The approval card"); `requires_status` says which needs are
            # met for this viewer; `manifest_empty` is the server's
            # "nothing to approve" (the card keys off it, not the actions).
            "files": _mf.parse_files(row),
            "egress": _mf.parse_egress(row),
            "handlers": _mf.parse_handlers(row),
            "exports": _mf.parse_exports(row),
            "bindings": _mf.parse_bindings(row),
            "requires": requires,
            "requires_status": _requires_status(agent, requires, u, mcps_by_agent, row),
            "manifest_empty": task_store.canonical_manifest(row) == "[]",
            # APPS.md "Steps": the scripts, where they would run, whether a
            # live link reaches the app (the card's warning line) and who
            # may approve.
            "steps": steps,
            **(_step_fields(row) if steps else {}),
            "steps_need_manager": steps_need_manager,
            # APPS.md "Secrets": the declared names, where the platform
            # sends each, and whether a value is set — never a value.
            "secrets": _secret_fields(row),
            # APPS.md "Inbound hooks": the public routes a vendor may call.
            "inbound": _mf.parse_inbound(row),
            # APPS.md "External links": what a link may do, with defaults.
            "external": _mf.parse_external(row),
            **_runtime_fields(row, u),
            **(_placement_fields(placed, u) if placed else {}),
        })
    return out


def _placement_fields(info: dict, u: UserContext) -> dict:
    """What a placed row adds (SHARING.md): where it comes from, who shared
    it, the cap, and whether this viewer may remove it from this agent (an
    editor or manager of the receiving agent for an agent share, an admin
    for a department share, which ends it for the whole department; nobody
    for a person's own placement: they remove it for themselves, which
    revokes their share). The source team's pending release is theirs alone. The names
    ride the list read; a single-row read looks them up."""
    from storage.agents import agent_store
    from storage.sharing import share_store
    kind = info.get("kind") or ""
    host = info.get("agent") or ""
    source = {} if "from_agent_name" in info else (
        agent_store.get_agent(info.get("from_agent") or "") or {})
    sharer = {} if "shared_by_name" in info else (
        task_store.get_user(info.get("shared_by") or "") or {})
    if kind == share_store.AGENT:
        can_remove = u.can_edit_agent(host)
    elif kind == share_store.DEPARTMENT:
        can_remove = u.is_admin
    else:
        can_remove = False
    return {
        "placement": {
            "kind": kind,
            "share_id": info.get("share_id") or "",
            "from_agent": info.get("from_agent") or "",
            "from_agent_name": (info.get("from_agent_name") or source.get("display_name")
                                or info.get("from_agent") or ""),
            "agent": host,
            "role_cap": info.get("role_cap") or roles.VIEWER,
            "shared_by": info.get("shared_by") or "",
            "shared_by_name": (info.get("shared_by_name") or sharer.get("display_name")
                               or sharer.get("name") or ""),
            "can_remove": can_remove,
        },
        "deploy_state": db_apps.DEPLOY_IDLE,
        "pending_release": 0,
    }


def _secret_fields(row: dict) -> list[dict]:
    """The card's secrets list: each declared name with its use and whether
    it is set (a names-only read of the store, nothing decrypted). Best
    effort: a lookup that fails leaves the flags out rather than the row."""
    if not _mf.parse_secrets(row):
        return []
    try:
        from services.apps import app_secrets
        return [{k: v for k, v in s.items() if k in ("name", "required", "description",
                                                     "sends_to", "env", "set", "declared")}
                for s in app_secrets.status_for(row)]
    except Exception:
        logger.exception("App %s: the secrets listing failed", row.get("slug"))
        return [{**s, "set": False, "declared": True} for s in _mf.parse_secrets(row)]


def _step_fields(row: dict) -> dict:
    """Where the app's steps run, in the card's words, and whether the app
    has a live external link (a step-running app reachable by a link gets
    the "link input is data" line). Synchronous, best effort: a lookup that
    fails leaves the field out rather than the row."""
    out: dict = {}
    try:
        from services.apps import app_steps
        from storage import remote_store
        from storage.remote_store import resolve_execution_target
        identity, _vis = app_steps.identity_for(row)
        target, _reason = resolve_execution_target(
            row.get("agent") or "", identity.creds_user_sub or None, identity.role)
        machine_id = placement.machine_of(target)
        if not machine_id:
            out["step_target"] = {"kind": placement.SITE_LOCAL}
        else:
            machine = remote_store.get_remote_machine(machine_id) or {}
            out["step_target"] = {"kind": "machine",
                                  "name": machine.get("name") or target[:8]}
    except Exception:
        logger.debug("app %s: step placement lookup failed", row.get("slug"), exc_info=True)
    try:
        from storage.sharing import share_store
        out["has_live_link"] = any(
            s.get("scope") == "external" and share_store.is_live(s)
            for s in share_store.list_target_shares("app", row["id"]))
    except Exception:
        logger.debug("app %s: share lookup failed", row.get("slug"), exc_info=True)
    return out


def _requires_status(agent: str, requires: dict, u: UserContext,
                     mcps_by_agent: dict[str, dict[str, str]], row: dict) -> dict:
    """Which of an app's declared needs are met: an MCP when the agent has
    it assigned, a provider when the identity the app's calls RUN WITH has
    an account on one of the agent's MCPs that uses it — the agent's
    service account for a shared app, the owner's for a personal one (the
    lookup ``integrations.status`` performs; the card names the account
    and whose it is)."""
    mcps = [m for m in (requires.get("mcps") or []) if isinstance(m, str)]
    providers = [p for p in (requires.get("providers") or []) if isinstance(p, str)]
    if not mcps and not providers:
        return {"mcps": [], "providers": []}
    if agent not in mcps_by_agent:
        mcps_by_agent[agent] = _mf.assigned_mcp_keys(agent)
    keys = mcps_by_agent[agent]
    status: dict = {"mcps": [{"name": m, "assigned": bool(keys.get(m))} for m in mcps],
                    "providers": []}
    if providers:
        from api.apps import catalog
        known = {p["provider"]: p for p in catalog.provider_status(agent, row, u)}
        for p in providers:
            hit = known.get(p) or {}
            status["providers"].append({
                "provider": p, "mcp": hit.get("mcp", ""),
                "connected": bool(hit.get("connected")),
                "account": hit.get("account", ""), "identity": hit.get("identity", ""),
            })
    return status


def _runtime_fields(row: dict, u: UserContext) -> dict:
    """What a folder app adds to the shape (APPS.md): the kind, the tree
    hash the client document is addressed by, the preview copy's hash for
    whoever may open it, the deploy state and the server's state."""
    from services.apps import app_sandbox, app_supervisor, releases
    if not db_apps.app_kind_of(row).serves_tree:
        return {"kind": db_apps.APP_KIND_FILE}
    preview_sha = ""
    if _can_approve_surface(row, u):
        pd = releases.preview_dir(row)
        if (pd / releases.MANIFEST_NAME).is_file():
            preview_sha = releases.tree_sha(pd)
    try:
        live = releases.live_release_dir(row)
    except releases.ReleaseDamaged:
        live = None
    status = app_supervisor.status(row["id"])
    return {
        "kind": db_apps.APP_KIND_FOLDER,
        "release_sha": row.get("release_sha256") or "",
        "preview_sha": preview_sha,
        "has_server": bool(live is not None and app_sandbox.server_entry(live)),
        "deploy_state": row.get("deploy_state") or db_apps.DEPLOY_IDLE,
        "pending_release": int(row.get("pending_release") or 0),
        "deploying": _deploy_lock(row["id"]).locked(),
        "server": status["server"],
        "server_error": status["error"],
    }


@router.get("/v1/apps")
async def list_apps(
    agent: str,
    user: UserContext | None = Depends(get_current_user),
):
    """The viewer's merged app list: shared rows first, then their own
    personal rows (each by position; order[0] is the default tab). Standing
    rows only — chat/project Dock pins serve through
    ``GET /v1/chats/{chat_id}/pins``. Shared rows the viewer hid for
    themselves (S2) still return, flagged ``hidden_for_me`` — the client
    keeps them off the strip and offers restore."""
    u = require_auth(user)
    require_agent_access(u, agent)

    def _load() -> list[dict]:
        username = _viewer_username(u)
        # A per-user template app this member never got (a crash mid-loop,
        # a restore) is queued for the seeder here, at most once a minute
        # (COMMUNITY-AGENTS-REGISTRY.md "Per-user template apps").
        if not u.is_api_key:
            from services.community import template_app_seeder
            try:
                template_app_seeder.heal_missing(agent, u.sub, username)
            except Exception:
                logger.exception("template app heal failed for %s", agent)
        return shape_app_rows(task_store.list_apps(
            agent, username, viewer_sub=u.sub, with_placements=not u.is_api_key), u)

    return {"apps": await asyncio.to_thread(_load)}


@router.get("/v1/apps/{app_id}")
async def read_app(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """One app in the list shape (the full-screen page's header and
    manifest), plus its agent and, for a chat-scoped pin, the chat it
    belongs to; for a viewer a share admits, how (``granted`` for their own
    share, ``placement`` with the agent it sits in for them). Denied is the
    same 404 as missing, like the serve route."""
    u = require_auth(user)

    def _load() -> dict | None:
        row = _visible_row(app_id, u)
        if not row:
            return None
        shaped = shape_app_rows([row], u)[0]
        shaped["agent"] = row.get("agent") or ""
        shaped["chat_id"] = row.get("scope_chat_id") or ""
        if not u.is_api_key:
            grant, placement = _viewer_share(row, u)
            if grant:
                shaped["granted"] = True
                shaped["share_id"] = grant["id"]
            if placement:
                shaped.update(_placement_fields(placement, u))
                shaped["can_manage"] = False
                shaped["can_approve"] = False
        return shaped

    out = await asyncio.to_thread(_load)
    if out is None:
        raise HTTPException(status_code=404, detail="App not found")
    return out


@router.get("/v1/apps/{app_id}/state")
async def read_app_state(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """The app's state document for a viewer's page (APPS.md "Live
    apps"): ``{doc, rev}``, ``rev`` 0 for an app never written. Denied is
    the same 404 as missing."""
    u = require_auth(user)

    def _load() -> dict | None:
        if not _visible_row(app_id, u):
            return None
        doc, rev = task_store.get_app_state(app_id)
        return {"doc": doc, "rev": rev}

    out = await asyncio.to_thread(_load)
    if out is None:
        raise HTTPException(status_code=404, detail="App not found")
    return out


@router.post("/v1/apps/{app_id}/hide")
async def hide_app_for_me(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """S2 hide-for-me: park a SHARED standing app off the CALLER's strip
    only — any role (this is the viewer's ✕; the team-wide soft-unpin stays
    ``DELETE /v1/apps/{id}`` behind ``_can_manage``). Personal rows are
    refused: their owner already has the real unpin."""
    u = require_auth(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if row.get("username"):
        # A grantee parks another user's personal app off their own list
        # through the share row (SHARING.md); the owner has the real unpin.
        if await asyncio.to_thread(_set_grant_hidden, row, u, True):
            return {"status": "ok"}
        raise HTTPException(
            status_code=400,
            detail="hide-for-me applies to shared apps only — unpin your "
                   "personal app instead",
        )
    if task_store.app_is_scoped(row):
        raise HTTPException(status_code=400,
                            detail="Dock pins cannot be hidden per-user")
    await asyncio.to_thread(task_store.hide_app_for_user, app_id, u.sub)
    return {"status": "ok"}


def _set_grant_hidden(row: dict, u: UserContext, hidden: bool) -> bool:
    """Flip the caller's own hide on their grant of a personal app. False
    when the caller is the owner or holds no grant (the caller then takes
    the row's own path)."""
    if (row.get("owner_sub") or "") == u.sub:
        return False
    from services.apps import audience
    from storage.sharing import share_store
    if not share_store.set_grant_hidden("app", row["id"], u.sub, hidden):
        return False
    audience.forget(row["id"])
    return True


@router.post("/v1/apps/{app_id}/unhide")
async def unhide_app_for_me(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Restore a hide-for-me (S2). Idempotent."""
    u = require_auth(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if row.get("username") and await asyncio.to_thread(_set_grant_hidden, row, u, False):
        return {"status": "ok"}
    await asyncio.to_thread(task_store.unhide_app_for_user, app_id, u.sub)
    return {"status": "ok"}


# ── The platform catalog (APPS.md "Platform catalog") ─────────────────


def _catalog_entry(row: dict, u: UserContext, *, feed: str = "", method: str = "",
                   unattended: bool = False, role: str | None = None) -> dict:
    """The declared, approved entry the viewer clears the floor of; raises
    the refusal otherwise. ``unattended`` is the platform basis (a handler
    of the app itself, APPS.md "Handlers"): the approval is the floor.
    ``role`` judges the floor instead of ``u``'s standing: an agent session
    is judged at its per-agent row, never its owner's platform role."""
    for a in _mf.parse_actions(row):
        if feed and a.get("type") == "data_feed" and a.get("feed") == feed:
            break
        if method and a.get("type") == "platform" and a.get("method") == method:
            break
    else:
        raise HTTPException(status_code=404, detail="not declared in this app's manifest")
    if not task_store.app_actions_approved(row):
        raise HTTPException(status_code=409, detail="actions not approved")
    if not unattended and not _mf.meets_floor(a, role or _mf.caller_role(row, u)):
        raise HTTPException(status_code=403, detail=_mf.floor_reason(a))
    return a


@router.get("/v1/apps/{app_id}/catalog/{feed}")
async def read_catalog_feed(
    app_id: str,
    feed: str,
    user: UserContext | None = Depends(get_current_user),
):
    """A feed's snapshot for this viewer, with the sequence deltas continue
    from. The two host-answered feeds are not served here."""
    from api.apps import catalog
    u = require_auth(user)
    if feed not in catalog.FEEDS or feed in catalog.CLIENT_FEEDS:
        raise HTTPException(status_code=404, detail="unknown feed")
    if feed == "notifications" and u.is_api_key:
        # The inbox spans every agent; ``/v1/notifications`` answers a
        # bearer the definitions only, and an app's page is no way round it.
        raise HTTPException(status_code=403, detail="the inbox is a person's own page")

    def _load() -> dict:
        row = _visible_row(app_id, u)
        if not row:
            raise HTTPException(status_code=404, detail="App not found")
        _catalog_entry(row, u, feed=feed)
        agent = row.get("agent") or ""
        return {"rows": catalog.snapshot(feed, agent, u),
                "seq": catalog.current_seq(u.sub, "" if feed == "notifications" else agent, feed)}

    return await asyncio.to_thread(_load)


class PlatformCallRequest(BaseModel):
    args: Any = None


@router.post("/v1/apps/{app_id}/catalog/{method}")
async def call_catalog_method(
    app_id: str,
    method: str,
    req: PlatformCallRequest | None = None,
    user: UserContext | None = Depends(get_current_user),
):
    """One platform method as the viewer: ``{ok, result}`` or ``{ok: false,
    reason}``; four calls per second per (app, viewer)."""
    from api.apps import catalog
    u = require_auth(user)
    if method not in catalog.METHODS:
        raise HTTPException(status_code=404, detail="unknown platform method")
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    role = await asyncio.to_thread(_mf.caller_role, row, u)
    entry = _catalog_entry(row, u, method=method, role=role)
    args = req.args if req else None
    if entry.get("args_schema"):
        validated, err = _mf.validate_args(entry["args_schema"], args)
        if err:
            raise HTTPException(status_code=400, detail=err)
        args = validated
    _check_fire_rate(app_id, f"\x00platform:{method}", u.sub, interval=0.25)

    def _run() -> dict:
        try:
            return {"ok": True, "result": catalog.run_method(method, row.get("agent") or "", row, u, args,
                                                              role=role)}
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        except PermissionError as e:
            return {"ok": False, "reason": str(e)}

    out = await asyncio.to_thread(_run)
    await finish_platform_result(out, u.sub)
    return out


async def finish_platform_result(out: dict, user_sub: str) -> None:
    """The async tail of a platform method: a notification to oneself goes
    out like any other (toast, inbox, the catalog's own feed); a file the
    method wrote is announced like a pin's (dashboards and satellites)."""
    if not out.get("ok") or not isinstance(out.get("result"), dict):
        return
    delivery = out["result"].pop("_delivery", None)
    if delivery:
        from services.notifications import notification_manager
        await notification_manager._deliver_to_user(user_sub, delivery)
    written = out["result"].pop("_written", None)
    if written:
        agent, rel, host_path = written
        from pathlib import Path as _Path
        from api.media.uploads import _push_upload_to_active_remote_sessions
        from services.notifications import notification_manager
        await notification_manager.broadcast_file_updated(agent, rel, source="disk")
        await _push_upload_to_active_remote_sessions(agent, rel, _Path(host_path))
    setup = out["result"].pop("_setup", None)
    if setup:
        # ``setup.complete`` (COMMUNITY-AGENTS-REGISTRY.md): the same service
        # the complete_setup tool's route runs, for the viewer alone.
        from services.agents import setup_state
        if setup["scope"] == "user":
            done = await setup_state.complete_user_setup(setup["agent"], setup["username"], setup["sub"])
        else:
            from storage.agents import agent_store
            row = await asyncio.to_thread(agent_store.get_agent, setup["agent"]) or {}
            done = await setup_state.complete_agent_setup(setup["agent"], row)
            done.pop("agent", None)
        out["result"].update(done)


# One keep-warm per (app, viewer) per window: the host page asks on every
# open; the pool entry it builds outlives the window on its own grace.
_WARM_MIN_INTERVAL_S = 600.0
_warm_tasks: set[asyncio.Task] = set()


@router.post("/v1/apps/{app_id}/warm")
async def warm_app(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Keep the app's tool manager warm: any accessor of an approved app may
    ask, the build runs in the background under the app's own identity
    (exactly what a click would build), and the entry stays for the
    keep-warm grace. 202 when a build was scheduled, 204 when there is
    nothing to build (no tool buttons, stale approval, asked recently)."""
    u = require_auth(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    from services.apps import headless_exec

    def _warmable() -> bool:
        if not headless_exec.manifest_mcps(row):
            return False
        if not task_store.app_actions_approved(row):
            return False
        return _mf.sub_can_approve_surface(row.get("approved_by") or "", row)

    if not await asyncio.to_thread(_warmable):
        return Response(status_code=204)
    try:
        _check_fire_rate(app_id, "\x00warm", u.sub, interval=_WARM_MIN_INTERVAL_S)
    except HTTPException:
        return Response(status_code=204)

    async def _build() -> None:
        try:
            await headless_exec.warm(row)
        except Exception:
            logger.exception(f"app warm failed: app={row.get('slug')}")

    task = asyncio.get_running_loop().create_task(_build())
    _warm_tasks.add(task)
    task.add_done_callback(_warm_tasks.discard)
    return Response(status_code=202)


class OrderRequest(BaseModel):
    agent: str
    ids: list[str]


@router.put("/v1/apps/order")
async def reorder_apps(
    req: OrderRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Reorder the viewer's merged list. Positions renumber WITHIN each
    scope group; moving shared rows needs editor+ (viewers may still
    reorder their own personal rows — their shared subsequence must arrive
    unchanged)."""
    u = require_auth(user)
    require_agent_access(u, req.agent)

    def _apply() -> str:
        rows = {r["id"]: r for r in task_store.list_apps(req.agent, _viewer_username(u))}
        wanted = [rows[i] for i in req.ids if i in rows]
        if len(wanted) != len(rows):
            return "stale list — refresh and try again"
        shared_new = [r["id"] for r in wanted if not r["username"]]
        personal_new = [r["id"] for r in wanted if r["username"]]
        shared_cur = [r["id"] for r in task_store.list_apps(req.agent, "")]
        if shared_new != shared_cur and not (u.is_admin or u.can_edit_agent(req.agent)):
            return "editor role required to reorder shared apps"
        updates = [(i, pos) for pos, i in enumerate(shared_new)]
        updates += [(i, pos) for pos, i in enumerate(personal_new)]
        task_store.set_app_positions(updates)
        return ""

    err = await asyncio.to_thread(_apply)
    if err:
        raise HTTPException(status_code=409 if "stale" in err else 403, detail=err)
    return {"status": "ok"}


@router.delete("/v1/apps/{app_id}")
async def unpin_app(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Dashboard-side unpin is SOFT: the row is hidden, not deleted — the
    workspace ``.html``, the actions manifest AND its approval all survive,
    so an agent ``pin_app(slug)`` restores the app exactly as approved.
    The agent-side unpin hook is the hard delete."""
    u = require_auth(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_manage(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to unpin this app")
    # A folder app's server stops with the soft unpin (APPS.md "Lifecycle").
    from services.apps import app_supervisor
    await app_supervisor.stop(app_id)
    # The parked set stays bounded: rows the hide pruned take their release
    # copies with them (the database owns the rows, not the files).
    pruned: list[dict] = []
    await asyncio.to_thread(task_store.set_app_hidden, app_id, True, pruned=pruned)
    if pruned:
        from services.apps import app_lifecycle, releases
        await app_lifecycle.forget_rows(pruned)
        for gone in pruned:
            await app_supervisor.stop(gone["id"])
            await asyncio.to_thread(releases.remove_release_dir, gone)
    return {"status": "ok"}


class PurgeRequest(BaseModel):
    confirm: str = ""


@router.post("/v1/apps/{app_id}/purge")
async def purge_app(
    app_id: str,
    req: PurgeRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """"Delete app and its data" (APPS.md "Lifecycle"): the row, the
    releases, the database and the folder. A person at the keyboard who
    may manage the row, who typed the slug."""
    u = require_human(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_manage(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to delete this app")
    if (req.confirm or "").strip().lower() != row["slug"]:
        raise HTTPException(status_code=400, detail="Type the app's slug to confirm")
    from services.apps import app_lifecycle
    return await app_lifecycle.purge(row)
