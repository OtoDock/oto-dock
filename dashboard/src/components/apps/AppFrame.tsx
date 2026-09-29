import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useTheme } from '../../contexts/ThemeContext'
import { onFileUpdate } from '../../lib/fileUpdates'
import { onAppDeployed, onAppPush, onAppState, onCatalog, subscribeCatalog, unsubscribeCatalog } from '../../lib/appLive'
import { appMounted, appUnmounted } from '../../lib/focus'
import { emitIframeSwipe } from '../../lib/iframeGestures'
import { hasStrictUserActivation, hasUserActivation, openExternalUrl, validateBridgedUrl } from '../../lib/openExternal'
import { buildOpenTarget, SETTINGS_KINDS } from '../../lib/openTarget'
import { callPlatformMethod, fetchCatalogFeed, fireAppActions, mintViewerToken, useAppState, warmApp, type AppActionCall, type AppActionResult, type AppState, type PinnedApp } from '../../api/apps'
import { meetsFloor } from '../../lib/permissions'
import { appKind } from '../../lib/kinds/app'
import { useActiveChats } from '../../hooks/useActiveChats'
import { onIdle as speechIdle } from '../../audio/speechActivity'
import { APP_SERVER_STATE, FRAME_STATE, type AppServerState, type FrameState } from '../../lib/status/appServer'

/**
 * Sandboxed pinned app frame — agent-authored HTML served cookie-authed
 * by /v1/apps/{id}/html under the same opaque-origin CSP as /v1/ui.
 *
 * Same STANDALONE iframe hygiene as UiArtifact (sandbox="allow-scripts" ONLY,
 * source-window identity on every message, e.origin never trusted). The
 * interaction contract differs: apps invoke DECLARED actions by id
 * (otodock.action) gated by the user-approved manifest — there is no per-send
 * consent chip (the approval IS the standing consent) and free-form
 * otodock.send acks `unavailable` here.
 *
 * Actions arrive coalesced (`app_actions`, one macrotask's calls) and go to
 * the platform as ONE batch request; every call ends in exactly one
 * `action_result` keyed by its call_id. Batches are paced to the server's
 * one-per-second rule: calls that arrive inside the window queue for the
 * next batch instead of being refused.
 *
 * Data feeds (`otodock.feed`): the page subscribes to DECLARED read-only
 * platform feeds; THIS component answers from the viewing user's own
 * authenticated context (the iframe never holds a credential, CSP
 * connect-src 'none' stays) — initial snapshot on subscribe, a push on every
 * change. `active_chats` is self-served (the sidebar widget's hook);
 * `project_lanes` rows arrive as a prop from the Dock overlay, which owns
 * the viewer-scoped /project poll — absent elsewhere, so the subscription
 * answers with an error the page can render.
 *
 * Navigation (`otodock.open`): the page names a kind and an id, the host
 * builds the route (lib/openTarget) and moves the viewer through the router
 * behind the same gates as an external link — approval, a real user
 * gesture, the burst window — plus a first-use chip for the settings kinds.
 */

interface Props {
  app: PinnedApp
  agent: string
  /** send_prompt router (current chat / new chat / PTY) — wired by the host
      page. Absent on surfaces with no chat context → acks unavailable. */
  onSendPrompt?: (app: PinnedApp, action: { id: string; label: string; prompt: string }, args: unknown) => Promise<{ status: string; reason?: string }>
  /** Viewer-scoped rows for the `project_lanes` feed — wired by the Dock
      overlay only. */
  projectLanes?: unknown[]
  /** Size the frame to the app's reported content height (the shim's
      `content_height` messages) instead of filling the parent — the Dock's
      single-scroll layout: the PAGE scrolls, the frame never does. */
  autoHeight?: boolean
  /** Read-only hosts (history, task runs) have nowhere to navigate to:
      open_target acks `unavailable`. */
  readOnly?: boolean
  /** Folder apps: show the working copy the agent is editing (the owner or
      an editor, after a preview_app) instead of the live release. */
  preview?: boolean
  /** The platform's own render (APPS.md "Deploy pipeline"): the copy to
      load, by its tree hash; no keep-warm, no dashboard socket. */
  renderSha?: string
}

// The viewer token lives ten minutes; the host re-mints well before that.
const VIEWER_TOKEN_REFRESH_MS = 8 * 60 * 1000
// A refused mint (a burst of reloads, a blip) is retried this many times
// with a widening wait before the frame says the app cannot be reached.
const MINT_RETRIES = 6

// The viewer claims in hand, per app id and instance, shared by every frame
// instance: a remount (a keyed tab switch and back) reuses the claim
// instead of minting again inside the route's 2 s pace, and a claim is only
// ever posted into the frame of the app it was minted for (a claim carries
// the viewer's identity and role, so one app's page must never receive
// another's). The claim also names the server the page's calls reach: the
// working copy's page must never hold the live release's claim, nor the
// live page the preview's.
const viewerClaims = new Map<string, { token: string; exp: number; at: number }>()
const claimKey = (appId: string, preview: boolean) => `${appId}|${preview ? 'preview' : 'live'}`
export function _resetViewerClaimsForTests(): void { viewerClaims.clear() }

// autoHeight clamp: a hostile/broken page can post any number — keep the
// frame tall enough to be usable and short enough to never trap the page.
const AUTO_HEIGHT_MIN = 120
const AUTO_HEIGHT_MAX = 8000

// open_url / open_target bridge guards (mirrors UiArtifact — the two hosts
// deliberately don't share a message vocabulary, so the handler is written
// twice). One burst window serves both bridges.
const OPEN_URL_BURST = 3
const OPEN_URL_WINDOW_MS = 10_000

// The server accepts one batch per second per (app, viewer) and at most 16
// calls in one; calls inside the window wait for the next batch.
const BATCH_MIN_INTERVAL_MS = 1000
const BATCH_MAX_CALLS = 16
// A viewed dashboard asks the platform to keep its tools warm; the server
// dedups per (app, viewer) inside its own window.
const WARM_EVERY_MS = 10 * 60 * 1000
// A deploy announces itself twice (file_updated for older dashboards, the
// deploy frame for this one): reloads within this window are one reload.
const RELOAD_DEDUPE_MS = 800

type OpenConsent = 'unset' | 'allowed' | 'blocked'

function openUrlConsentKey(appId: string): string {
  return `otodock-app-openurl:${appId}`
}

function openSettingsConsentKey(appId: string): string {
  return `otodock-app-opensettings:${appId}`
}

function readOpenConsent(key: string): OpenConsent {
  try {
    const v = localStorage.getItem(key)
    return v === 'allowed' || v === 'blocked' ? v : 'unset'
  } catch { return 'unset' }
}

type OpenPrompt =
  | { kind: 'url'; origin: string }
  | { kind: 'settings'; label: string }

type PendingOpen =
  | { kind: 'url'; win: Window; url: string; callId?: string }
  | { kind: 'settings'; win: Window; path: string }

interface QueuedCall {
  win: Window
  call: AppActionCall
  actionId: string
  isTool: boolean
}

export default function AppFrame({ app, agent, onSendPrompt, projectLanes, autoHeight = false, readOnly = false, preview = false, renderSha = '' }: Props) {
  const { resolvedTheme } = useTheme()
  const navigate = useNavigate()
  const initialThemeRef = useRef(resolvedTheme)
  // Nonce bumps on file_updated → full reload (the served content is
  // no-store; re-setting src re-runs the app's scripts on fresh HTML).
  const [nonce, setNonce] = useState(0)
  // A folder app's document is addressed by its release's tree hash, so
  // its relative assets resolve under the hashed prefix (APPS.md "Client
  // files"); a single file keeps the cookie-authed html route. A render
  // names the copy it judges by its own hash.
  const folder = appKind(app).servesTree
  const showPreview = folder && preview && !!app.preview_sha && !renderSha
  const docSha = renderSha || (showPreview ? app.preview_sha : app.release_sha)
  const src = folder && docSha
    ? `/v1/apps/${app.id}/client/${docSha}/?theme=${initialThemeRef.current}&v=${nonce}${showPreview ? '&preview=1' : ''}${renderSha ? '&render=1' : ''}`
    : `/v1/apps/${app.id}/html?theme=${initialThemeRef.current}&v=${nonce}${folder && preview ? '&preview=1' : ''}`
  const showPreviewRef = useRef(showPreview)
  showPreviewRef.current = showPreview
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  // Loads the host caused: the iframe is remounted per src (keyed below),
  // so each element owes exactly one; a load nobody asked for is a page
  // that navigated itself — blanked, and told nothing more. A remount
  // rather than a src change also keeps the host's reloads out of the
  // joint session history, where Back would walk the frame to an older
  // document and trip this guard.
  const expectedLoadsRef = useRef(0)
  const [navigatedAway, setNavigatedAway] = useState(false)
  const navigatedAwayRef = useRef(false)
  useEffect(() => { expectedLoadsRef.current = 1 }, [src])
  // The viewer token a folder app's page sends with its own API calls: the
  // reloads a deploy or an approval causes each say `ready` again and reuse
  // the claim in hand (`viewerClaims`) instead of minting (the route paces).
  const tokenTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mintFailuresRef = useRef(0)
  // The runtime shim's server_status word (the supervisor's states plus its
  // own `failed`) or the frame's own mint states (lib/status/appServer.ts).
  const [serverStatus, setServerStatus] = useState<{ state: AppServerState | FrameState; retry: number } | null>(null)
  const lastSendAtRef = useRef(0)
  const lastActionAtRef = useRef<Map<string, number>>(new Map())
  const onSendPromptRef = useRef(onSendPrompt)
  onSendPromptRef.current = onSendPrompt
  const navigateRef = useRef(navigate)
  navigateRef.current = navigate
  const readOnlyRef = useRef(readOnly)
  readOnlyRef.current = readOnly
  // The platform's own rendered check (APPS.md "The rendered check"): a
  // synthetic viewer that must never press a real button.
  const renderRef = useRef(!!renderSha)
  renderRef.current = !!renderSha
  const appRef = useRef(app)
  appRef.current = app
  const agentRef = useRef(agent)
  agentRef.current = agent

  // Task-driven refresh: pin_app broadcasts file_updated for the app's
  // workspace file — reload the frame live (same matching rule as
  // useCollaboraLiveReload: agent_slug + rel_path). Feed subscriptions die
  // with the outgoing document HERE, at reload initiation — NOT in onLoad:
  // the page subscribes while parsing, BEFORE the load event, so an onLoad
  // clear wipes a fresh subscription and every later push is skipped
  // (found live on T1 — the initial snapshot arrived, updates never did).
  // Agents write app files exactly at turn end, and a reload is a full
  // iframe document parse on the main thread — defer past live TTS
  // (speechIdle runs synchronously when nothing is speaking). The clear +
  // nonce bump stay ONE unit inside the deferred callback (see above —
  // splitting them strands the live document unsubscribed).
  const reloadAtRef = useRef(0)
  const requestReload = () => {
    const now = Date.now()
    if (now - reloadAtRef.current < RELOAD_DEDUPE_MS) return
    reloadAtRef.current = now
    speechIdle(() => {
      feedSubsRef.current.clear()
      dropServerSubs()
      setNonce((n) => n + 1)
    })
  }
  useEffect(() => onFileUpdate((u) => {
    if (u.agent_slug === agent && u.rel_path === app.rel_path) requestReload()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [agent, app.rel_path])
  // A deploy (a pin or a rollback) names the manifest it ships with: the
  // row is refetched first, and the frame reloads once the row in hand
  // carries that signature, so a newly declared feed is never refused by
  // a stale manifest (APPS.md "Releases and rollback").
  const pendingSigRef = useRef<string | null>(null)
  useEffect(() => onAppDeployed((f) => {
    if (f.app_id !== app.id) return
    qc.invalidateQueries({ queryKey: ['apps', agentRef.current] })
    qc.invalidateQueries({ queryKey: ['chat-pins'] })
    qc.invalidateQueries({ queryKey: ['app', app.id] })
    // A folder app's document is addressed by its release hash: the
    // refetched row changes the src, and that IS the reload (a nonce bump
    // here would load the old hash once more, before the row arrives).
    if (appKind(appRef.current).servesTree) return
    if (appRef.current.actions_sig === f.actions_sig) {
      pendingSigRef.current = null
      requestReload()
    } else {
      pendingSigRef.current = f.actions_sig
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [app.id])
  useEffect(() => {
    if (pendingSigRef.current && app.actions_sig === pendingSigRef.current) {
      pendingSigRef.current = null
      requestReload()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app.actions_sig])
  // An approval that lands while the document is open: the page subscribed
  // its feeds at parse time and was refused, so it reloads once to ask again.
  const approvedRef = useRef(app.actions_approved)
  const prevReleaseRef = useRef(app.release_sha)
  useEffect(() => {
    // A folder app's approval that takes a pending release live reloads
    // the document by the new hash; one that approves the release already
    // serving (a rollback across a manifest change, a stale approval
    // renewed) keeps the hash and reloads here like a file app.
    const newRelease = folder && app.release_sha !== prevReleaseRef.current
    if (app.actions_approved && !approvedRef.current && !newRelease) requestReload()
    approvedRef.current = app.actions_approved
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app.actions_approved])
  // After the effect above: it reads the previous render's hash.
  useEffect(() => { prevReleaseRef.current = app.release_sha })

  // Keep-warm: a dashboard someone is looking at should answer its first
  // click without the tool spawn. Only apps with tool buttons and a live
  // approval have anything to warm; the server dedups per viewer.
  const warmable = app.actions_approved && !renderSha && app.actions.some((a) => a.type === 'mcp_tool')
  useEffect(() => {
    if (!warmable) return
    const tick = () => { if (document.visibilityState === 'visible') warmApp(app.id) }
    tick()
    const timer = setInterval(tick, WARM_EVERY_MS)
    return () => clearInterval(timer)
  }, [app.id, warmable])

  // ── Data feeds ────────────────────────────────────────────────────────────
  // active_chats is fetched only when the manifest declares it (the hook is
  // a no-op otherwise). Subscriptions live per LOADED DOCUMENT — cleared on
  // every frame load, since a reloaded page must re-subscribe.
  const wantsActiveChats = app.actions.some(
    (a) => a.type === 'data_feed' && a.feed === 'active_chats',
  )
  const activeChats = useActiveChats(wantsActiveChats)
  const feedSubsRef = useRef<Set<string>>(new Set())
  const activeChatsRef = useRef(activeChats)
  activeChatsRef.current = activeChats
  const projectLanesRef = useRef(projectLanes)
  projectLanesRef.current = projectLanes

  const postFeed = (feed: string, rows: unknown[] | null, error?: string) => {
    try {
      iframeRef.current?.contentWindow?.postMessage(
        { source: 'otodock-host', type: 'feed_update', feed,
          ...(error ? { error } : { rows: rows ?? [] }) },
        '*',
      )
    } catch { /* frame gone */ }
  }
  const feedRows = (feed: string): { rows?: unknown[]; error?: string } => {
    if (feed === 'active_chats') return { rows: activeChatsRef.current }
    if (feed === 'project_lanes') {
      return projectLanesRef.current
        ? { rows: projectLanesRef.current }
        : { error: 'project_lanes only flows on a project dock' }
    }
    return { error: 'unknown feed' }
  }
  // Platform-answered feeds (APPS.md "Platform catalog"): the snapshot
  // comes over REST as the viewer, deltas as `catalog` frames with a
  // per-feed sequence; a gap, or a reopened socket, re-requests the snapshot.
  const CLIENT_FEEDS = ['active_chats', 'project_lanes']
  const catalogRowsRef = useRef<Map<string, Record<string, unknown>[]>>(new Map())
  const catalogSeqRef = useRef<Map<string, number>>(new Map())
  // The server feeds this document asked for: the socket carries deltas
  // for them only (`catalog_subscribe`), dropped with the document.
  const serverSubsRef = useRef<Set<string>>(new Set())
  const dropServerSubs = () => {
    for (const feed of serverSubsRef.current) unsubscribeCatalog(agentRef.current, feed)
    serverSubsRef.current.clear()
  }
  useEffect(() => () => dropServerSubs(), []) // eslint-disable-line react-hooks/exhaustive-deps
  // A folder app reloads through its hash (a deploy, an approval, a
  // rollback, a fresh preview copy): the outgoing document's subscriptions
  // die here, as requestReload drops them before a nonce bump.
  const docShaRef = useRef(docSha)
  useEffect(() => {
    if (docShaRef.current !== docSha) {
      feedSubsRef.current.clear()
      dropServerSubs()
    }
    docShaRef.current = docSha
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docSha])
  const loadCatalogFeed = async (feed: string) => {
    try {
      const snap = await fetchCatalogFeed(appRef.current.id, feed)
      catalogRowsRef.current.set(feed, snap.rows)
      catalogSeqRef.current.set(feed, snap.seq)
      if (feedSubsRef.current.has(feed)) postFeed(feed, snap.rows)
    } catch (e) {
      if (feedSubsRef.current.has(feed)) postFeed(feed, null, e instanceof Error ? e.message : 'unavailable')
    }
  }
  useEffect(() => onCatalog((f) => {
    if (f.type === 'catalog_resync') {
      for (const feed of feedSubsRef.current) {
        if (!CLIENT_FEEDS.includes(feed)) void loadCatalogFeed(feed)
      }
      return
    }
    if (!feedSubsRef.current.has(f.feed)) return
    if (f.agent && f.agent !== agentRef.current) return
    const last = catalogSeqRef.current.get(f.feed)
    if (f.snapshot) {
      catalogRowsRef.current.set(f.feed, f.snapshot as Record<string, unknown>[])
      catalogSeqRef.current.set(f.feed, f.seq)
      postFeed(f.feed, f.snapshot)
      return
    }
    if (last !== undefined && f.seq !== last + 1) {
      // A dropped delta: the rows in hand are no longer the truth.
      void loadCatalogFeed(f.feed)
      return
    }
    catalogSeqRef.current.set(f.feed, f.seq)
    const delta = f.delta || {}
    const rows = [...(catalogRowsRef.current.get(f.feed) || [])]
    const id = delta.id
    const at = id === undefined ? -1 : rows.findIndex((r) => r.id === id)
    if (delta.removed) {
      if (at >= 0) rows.splice(at, 1)
    } else if (at >= 0) {
      rows[at] = { ...rows[at], ...delta }
    } else {
      rows.unshift(delta)
    }
    catalogRowsRef.current.set(f.feed, rows)
    postFeed(f.feed, rows)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [])
  // Push on change to every live subscription. active_chats churns exactly
  // at turn end (finish + linger-drop) — while speech is live, defer the
  // push and let the trailing flush read the freshest rows from the ref
  // (same-origin iframe = same main thread; the postMessage itself is
  // cheap, the in-frame re-render is not).
  useEffect(() => {
    if (!feedSubsRef.current.has('active_chats')) return
    speechIdle(() => {
      if (feedSubsRef.current.has('active_chats')) {
        postFeed('active_chats', activeChatsRef.current)
      }
    })
  }, [activeChats])
  useEffect(() => {
    if (projectLanes && feedSubsRef.current.has('project_lanes')) {
      postFeed('project_lanes', projectLanes)
    }
  }, [projectLanes])

  // Content height reported by the shim (autoHeight mode only).
  const [contentHeight, setContentHeight] = useState<number | null>(null)

  // open bridges' state: first-use consent (destination ORIGIN, or the
  // settings kinds) + one shared burst window.
  const [openPrompt, setOpenPrompt] = useState<OpenPrompt | null>(null)
  const pendingOpenRef = useRef<PendingOpen | null>(null)
  const openTimesRef = useRef<number[]>([])

  // Batched actions: the next batch and its flush timer.
  const batchQueueRef = useRef<QueuedCall[]>([])
  const batchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastBatchAtRef = useRef(0)

  const postTo = (win: Window, msg: Record<string, unknown>) => {
    try { win.postMessage({ source: 'otodock-host', ...msg }, '*') } catch { /* frame gone */ }
  }
  const openAckTo = (win: Window, status: string, reason?: string, callId?: string) =>
    postTo(win, { type: 'open_url_ack', status, ...(reason ? { reason } : {}), ...(callId ? { call_id: callId } : {}) })
  const openTargetAckTo = (win: Window, status: string, reason?: string) =>
    postTo(win, { type: 'open_ack', status, ...(reason ? { reason } : {}) })

  // ── Live apps (APPS.md "Live apps") ──────────────────────────────
  // Focus: a mounted frame is what the viewer sees; a read-only view is not.
  useEffect(() => {
    if (readOnly) return
    appMounted({ id: app.id, title: app.title || app.slug, slug: app.slug }, app.pin_scope === 'chat')
    return () => appUnmounted(app.id)
  }, [app.id, app.title, app.slug, app.pin_scope, readOnly])
  // The state document: fetched once, kept current by app_state frames (the
  // higher rev always wins), posted to the page on load and on change. The
  // page reads it through the otodock:state event, never a host request.
  const qc = useQueryClient()
  const { data: appState } = useAppState(app.id)
  const appStateRef = useRef<AppState | undefined>(appState)
  appStateRef.current = appState
  const postState = () => {
    const s = appStateRef.current
    const win = iframeRef.current?.contentWindow
    if (!s || !win) return
    postTo(win, { type: 'state', doc: s.doc, rev: s.rev })
  }
  useEffect(() => { postState() }, [appState]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => onAppState((f) => {
    if (f.app_id !== app.id) return
    qc.setQueryData<AppState>(['app-state', app.id], (old) =>
      old && old.rev >= f.rev ? old : { doc: f.doc, rev: f.rev })
  }), [app.id, qc])
  // A push is delivered as it arrives; nothing is kept for a later load.
  useEffect(() => onAppPush((f) => {
    if (f.app_id !== app.id) return
    const win = iframeRef.current?.contentWindow
    if (win) postTo(win, { type: 'push', payload: f.payload, ts: f.ts })
  }), [app.id]) // eslint-disable-line react-hooks/exhaustive-deps

  // The viewer token (APPS.md "Viewer identity"): minted for the person at
  // the keyboard when a folder app's page says `ready`, posted into the
  // frame, re-minted before it expires. A read-only host and a file app
  // never mint one; the page keeps waiting and its API calls never leave.
  const postViewerToken = (win: Window, fresh = false) => {
    // Read the app at call time: the message handler that calls this was
    // registered once, at mount, and the app prop may have changed since.
    const current = appRef.current
    if (!appKind(current).mayServe || readOnlyRef.current) return
    const appId = current.id
    const preview = showPreviewRef.current
    const key = claimKey(appId, preview)
    if (tokenTimerRef.current) clearTimeout(tokenTimerRef.current)
    const later = (ms: number) => {
      tokenTimerRef.current = setTimeout(() => {
        const live = iframeRef.current?.contentWindow
        if (live && appRef.current.id === appId) postViewerToken(live, true)
      }, ms)
    }
    const held = viewerClaims.get(key)
    const age = held ? Date.now() - held.at : Infinity
    if (!fresh && held && age < VIEWER_TOKEN_REFRESH_MS) {
      postTo(win, { type: 'viewer_token', token: held.token, exp: held.exp })
      later(VIEWER_TOKEN_REFRESH_MS - age)
      return
    }
    void mintViewerToken(appId, { preview }).then((t) => {
      if (navigatedAwayRef.current) return
      mintFailuresRef.current = 0
      viewerClaims.set(key, { token: t.token, exp: t.exp, at: Date.now() })
      // The frame may show another app, or the other copy of this one, by
      // the time the mint lands: the claim is kept for what it was minted
      // for and never posted into this frame.
      if (appRef.current.id !== appId || showPreviewRef.current !== preview) return
      setServerStatus((s) => (s && (s.state === FRAME_STATE.CONNECTING || s.state === FRAME_STATE.UNREACHABLE) ? null : s))
      postTo(win, { type: 'viewer_token', token: t.token, exp: t.exp })
      later(Math.min(VIEWER_TOKEN_REFRESH_MS, Math.max(30_000, (t.ttl || 600) * 800)))
    }).catch(() => {
      if (navigatedAwayRef.current) return
      mintFailuresRef.current += 1
      if (mintFailuresRef.current >= MINT_RETRIES) {
        setServerStatus({ state: FRAME_STATE.UNREACHABLE, retry: 0 })
        return
      }
      setServerStatus({ state: FRAME_STATE.CONNECTING, retry: 0 })
      later(Math.min(30_000, 2_000 * 2 ** (mintFailuresRef.current - 1)))
    })
  }
  useEffect(() => () => {
    if (tokenTimerRef.current) clearTimeout(tokenTimerRef.current)
  }, [])

  const deliverOpen = (win: Window, url: string, callId?: string) => {
    void openExternalUrl(url).then((r) => {
      if (r === 'opened') openAckTo(win, 'opened', undefined, callId)
      else openAckTo(win, 'blocked', 'popup blocked — try again', callId)
    })
  }
  const deliverOpenTarget = (win: Window, path: string) => {
    navigateRef.current(path)
    openTargetAckTo(win, 'opened')
  }

  const resolveOpenConsent = (allow: boolean) => {
    const consent: OpenConsent = allow ? 'allowed' : 'blocked'
    const pending = pendingOpenRef.current
    pendingOpenRef.current = null
    setOpenPrompt(null)
    const key = pending?.kind === 'settings'
      ? openSettingsConsentKey(appRef.current.id)
      : openUrlConsentKey(appRef.current.id)
    try { localStorage.setItem(key, consent) } catch { /* private mode */ }
    if (!pending) return
    if (pending.kind === 'url') {
      if (allow) deliverOpen(pending.win, pending.url, pending.callId)
      else openAckTo(pending.win, 'blocked', undefined, pending.callId)
    } else if (allow) {
      deliverOpenTarget(pending.win, pending.path)
    } else {
      openTargetAckTo(pending.win, 'blocked')
    }
  }

  // Send the queued calls as one batch; the per-call results flow back to
  // the frame as they land. Reschedules itself while the queue overflows
  // the batch cap.
  const flushBatch = () => {
    batchTimerRef.current = null
    const queue = batchQueueRef.current
    if (!queue.length) return
    const now = Date.now()
    const wait = BATCH_MIN_INTERVAL_MS - (now - lastBatchAtRef.current)
    if (wait > 0) {
      batchTimerRef.current = setTimeout(flushBatch, wait)
      return
    }
    const chunk = queue.splice(0, BATCH_MAX_CALLS)
    lastBatchAtRef.current = now
    const byId = new Map(chunk.map((q) => [q.call.call_id, q]))
    const appId = appRef.current.id
    void fireAppActions(appId, chunk.map((q) => q.call), (callId, r: AppActionResult) => {
      const q = byId.get(callId)
      if (!q) return
      postTo(q.win, { type: 'action_ack', status: r.status, call_id: callId, ...(r.reason ? { reason: r.reason } : {}) })
      if (q.isTool) {
        postTo(q.win, {
          type: 'action_result', id: q.actionId, call_id: callId,
          ok: r.status === 'done' || r.status === 'sent',
          result: r.result ?? r.reason ?? r.status,
        })
      }
    })
    if (queue.length) batchTimerRef.current = setTimeout(flushBatch, BATCH_MIN_INTERVAL_MS)
  }
  const enqueue = (q: QueuedCall) => {
    batchQueueRef.current.push(q)
    if (!batchTimerRef.current) batchTimerRef.current = setTimeout(flushBatch, 0)
  }
  useEffect(() => () => {
    if (batchTimerRef.current) clearTimeout(batchTimerRef.current)
  }, [])

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const win = e.source as Window | null
      if (!win || win !== iframeRef.current?.contentWindow) return
      const d: any = e.data
      if (!d || d.source !== 'otodock-artifact') return
      if (navigatedAwayRef.current) return
      if (d.type === 'ready') {
        // The runtime is installed: a folder app's page gets its viewer
        // token now (a file app has no API to call).
        postViewerToken(win)
        return
      }
      if (d.type === 'viewer_token_expired') {
        postViewerToken(win, true)
        return
      }
      if (d.type === 'server_status') {
        const state = String(d.state || '') as AppServerState
        setServerStatus(state === APP_SERVER_STATE.UP ? null : { state, retry: Number(d.retry_after) || 0 })
        return
      }
      if (d.type === 'content_height') {
        const h = Number(d.height)
        if (Number.isFinite(h)) {
          setContentHeight(Math.min(AUTO_HEIGHT_MAX, Math.max(AUTO_HEIGHT_MIN, Math.ceil(h))))
        }
        return
      }
      if (d.type === 'swipe') {
        // Drawer gesture forwarded out of the frame (iframes swallow
        // touches) — route into the host gesture bus; useSwipeGesture
        // containment decides which drawer it drives.
        const frame = iframeRef.current
        if (frame && (d.dir === 'left' || d.dir === 'right')) {
          emitIframeSwipe({ el: frame, dir: d.dir })
        }
        return
      }
      if (d.type === 'session_set' || d.type === 'session_clear') {
        // Accounts kept by the host are a link's (APPS.md "External links");
        // an internal viewer has an identity in the claim already.
        return postTo(win, { type: 'app_session', token: null, status: 'unavailable' })
      }
      if (d.type === 'challenge') {
        return postTo(win, { type: 'challenge_result', call_id: String(d.call_id || ''), token: null, status: 'unavailable' })
      }
      // One burst window for both open bridges: a page that cannot open
      // three tabs in ten seconds cannot move the viewer three times either.
      const burstAllows = () => {
        const nowOpen = Date.now()
        const times = openTimesRef.current.filter((t) => nowOpen - t < OPEN_URL_WINDOW_MS)
        openTimesRef.current = times
        if (times.length >= OPEN_URL_BURST) return false
        times.push(nowOpen)
        return true
      }
      if (d.type === 'open_url') {
        // Links bridged out of the sandbox (a bridged <a href>, or
        // otodock.openExternal with a call id the ack echoes). Must NOT
        // bypass the app's standing approval (every other app channel is
        // actions_approved-gated; an actionless app approves vacuously and
        // the first-use origin chip below is its consent). Validation +
        // activation + burst mirror UiArtifact. A host the manifest
        // declares under external.links (APPS.md "External links") was
        // approved on the card, so it opens without the chip.
        const callId = typeof d.call_id === 'string' ? d.call_id.slice(0, 64) : undefined
        if (!appRef.current.actions_approved) {
          return openAckTo(win, 'denied', 'actions not approved', callId)
        }
        const v = validateBridgedUrl(d.url)
        if ('error' in v) return openAckTo(win, 'denied', v.error, callId)
        if (!hasUserActivation()) return openAckTo(win, 'denied', 'no user gesture', callId)
        if (!burstAllows()) return openAckTo(win, 'denied', 'rate limited', callId)
        const host = new URL(v.url).hostname.toLowerCase()
        if ((appRef.current.external?.links ?? []).some((h) => h.toLowerCase() === host)) {
          return deliverOpen(win, v.url, callId)
        }
        const consent = readOpenConsent(openUrlConsentKey(appRef.current.id))
        if (consent === 'blocked') return openAckTo(win, 'blocked', undefined, callId)
        if (consent === 'allowed') return deliverOpen(win, v.url, callId)
        pendingOpenRef.current = { kind: 'url', win, url: v.url, callId }
        setOpenPrompt({ kind: 'url', origin: v.origin })
        return
      }
      if (d.type === 'open_target') {
        // Platform navigation by kind. The page never supplies a URL; the
        // route builder validates the kind and the id shape, and the
        // destination enforces its own access. A missing User Activation
        // API fails closed here (unlike links, which the browser's popup
        // blocker still guards).
        if (readOnlyRef.current) return openTargetAckTo(win, 'unavailable', 'not available in this view')
        if (!appRef.current.actions_approved) {
          return openTargetAckTo(win, 'denied', 'actions not approved')
        }
        const route = buildOpenTarget(d.target, agentRef.current)
        if ('error' in route) return openTargetAckTo(win, 'denied', route.error)
        if (!hasStrictUserActivation()) return openTargetAckTo(win, 'denied', 'no user gesture')
        if (!burstAllows()) return openTargetAckTo(win, 'denied', 'rate limited')
        if (!SETTINGS_KINDS.has(route.kind)) return deliverOpenTarget(win, route.path)
        const consent = readOpenConsent(openSettingsConsentKey(appRef.current.id))
        if (consent === 'blocked') return openTargetAckTo(win, 'blocked')
        if (consent === 'allowed') return deliverOpenTarget(win, route.path)
        pendingOpenRef.current = { kind: 'settings', win, path: route.path }
        setOpenPrompt({ kind: 'settings', label: route.label })
        return
      }
      const ack = (status: string, reason?: string, callId?: string) =>
        postTo(win, { type: 'action_ack', status, ...(reason ? { reason } : {}), ...(callId ? { call_id: callId } : {}) })
      if (d.type === 'action') {
        // Free-form otodock.send has no chat binding in app context.
        return ack('unavailable', 'apps use declared actions')
      }
      if (d.type === 'feed_subscribe') {
        const feed = String(d.feed || '')
        const current = appRef.current
        const declared = current.actions.find((a) => a.type === 'data_feed' && a.feed === feed)
        if (!declared) {
          return postFeed(feed, null, 'feed not declared in this app\'s manifest')
        }
        if (!current.actions_approved) {
          return postFeed(feed, null, 'actions not approved')
        }
        if (!meetsFloor(declared, current.viewer_role)) {
          return postFeed(feed, null, `this feed needs the ${declared.min_role} role`)
        }
        feedSubsRef.current.add(feed)
        if (readOnlyRef.current && !CLIENT_FEEDS.includes(feed)) {
          return postFeed(feed, null, 'not available in this view')
        }
        if (!CLIENT_FEEDS.includes(feed)) {
          // Subscribe on the socket before the snapshot, so nothing between
          // the two is missed (a delta that outruns the snapshot heals on
          // the next one, by the sequence).
          if (!serverSubsRef.current.has(feed)) {
            serverSubsRef.current.add(feed)
            subscribeCatalog(agentRef.current, feed)
          }
          const cached = catalogRowsRef.current.get(feed)
          if (cached) postFeed(feed, cached)
          void loadCatalogFeed(feed)
          return
        }
        const { rows, error } = feedRows(feed)
        return postFeed(feed, rows ?? null, error)
      }
      if (d.type === 'platform_call') {
        // otodock.platform(method, args): a declared platform method, answered
        // as the viewer; every call ends in exactly one platform_result.
        const callId = typeof d.call_id === 'string' ? d.call_id.slice(0, 64) : ''
        const method = String(d.method || '')
        const current = appRef.current
        const reply = (ok: boolean, result?: unknown, reason?: string) =>
          postTo(win, { type: 'platform_result', call_id: callId, ok, ...(ok ? { result } : { reason }) })
        const declared = current.actions.find((a) => a.type === 'platform' && a.method === method)
        if (!declared) return reply(false, undefined, 'not declared in this app\'s manifest')
        if (!current.actions_approved) return reply(false, undefined, 'actions not approved')
        if (readOnlyRef.current) return reply(false, undefined, 'not available in this view')
        if (!meetsFloor(declared, current.viewer_role)) return reply(false, undefined, `needs the ${declared.min_role} role`)
        void callPlatformMethod(current.id, method, d.args).then(
          (r) => reply(!!r.ok, r.result, r.reason),
          () => reply(false, undefined, 'network error'),
        )
        return
      }
      // `app_actions` carries one macrotask's calls; a lone `app_action`
      // (a page authored against the older runtime) is a batch of one.
      let calls: { call_id: string; id: string; args: unknown }[]
      if (d.type === 'app_actions') {
        calls = Array.isArray(d.calls) ? d.calls.slice(0, 64) : []
      } else if (d.type === 'app_action') {
        calls = [{ call_id: '', id: d.id, args: d.args }]
      } else {
        return
      }
      const current = appRef.current
      for (const raw of calls) {
        const callId = typeof raw?.call_id === 'string' ? raw.call_id.slice(0, 64) : ''
        const action = current.actions.find((a) => a.id === String(raw?.id || ''))
        if (!action) { ack('denied', 'unknown action', callId); continue }
        if (action.type === 'data_feed') {
          ack('denied', 'feeds are subscriptions — use otodock.feed(name, cb)', callId)
          continue
        }
        // mcp_tool pages await `otodock:action-result` to re-enable controls
        // and clear spinners — EVERY accepted-or-refused invocation must end
        // in exactly one terminal result (rate-denials and network failures
        // included; a swallowed refusal is a forever-spinner, found live on
        // the trusted VM after fast button bursts).
        const postResult = (ok: boolean, result: string) => {
          if (action.type !== 'mcp_tool') return
          postTo(win, { type: 'action_result', id: action.id, ok, result, ...(callId ? { call_id: callId } : {}) })
        }
        if (!current.actions_approved) {
          const reason = current.approval_stale ? 'approval stale' : 'actions not approved'
          ack('denied', reason, callId)
          postResult(false, reason)
          continue
        }
        if (renderRef.current) {
          // A render never fires a task, a tool or a prompt: the platform
          // would refuse the call anyway (the render confinement), but a
          // refused request is a console error the report counts against
          // the page. Answer here instead, so the page shows its refused
          // state and the render stays clean.
          ack('unavailable', 'not available in the rendered check', callId)
          postResult(false, 'not available in the rendered check')
          continue
        }
        const now = Date.now()
        if (action.type === 'send_prompt') {
          // Global 1s across the frame — every delivery costs an agent turn.
          if (now - lastSendAtRef.current < 1000) { ack('denied', 'rate limited', callId); continue }
          lastSendAtRef.current = now
          const send = onSendPromptRef.current
          if (!send) { ack('unavailable', 'not available in this view', callId); continue }
          void send(current, { id: action.id, label: action.label, prompt: action.prompt || '' }, raw.args)
            .then((r) => ack(r.status, r.reason, callId))
          continue
        }
        // mcp_tool / fire_task: rate per ACTION+ARGS so a data panel fires
        // its queries together AND one parameterized action can serve many
        // widgets (a `toggle` with the entity in args). Short window — this
        // only swallows accidental double-events; the server enforces the
        // real args-aware min-interval + in-flight.
        let argsKey = ''
        try { argsKey = JSON.stringify(raw.args) ?? '' } catch { /* non-JSON args are denied server-side */ }
        const key = `${action.id}|${argsKey}`
        const last = lastActionAtRef.current.get(key) || 0
        if (now - last < 400) {
          ack('denied', 'rate limited', callId)
          postResult(false, 'Rate limited — slow down')
          continue
        }
        lastActionAtRef.current.set(key, now)
        // Args ride to the server's user-approved schema gate (client sends
        // them verbatim; validation is never client-side). The batch answers
        // per call: an ack, and for mcp_tool the tool's output as
        // action_result → the in-page `otodock:action-result` event.
        enqueue({
          win,
          call: { call_id: callId || `h${now}${Math.random().toString(36).slice(2, 8)}`, action_id: action.id, args: raw.args ?? null },
          actionId: action.id,
          isTool: action.type === 'mcp_tool',
        })
      }
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [])

  // Live theme switch → child (payload carries only the theme string).
  const themeRef = useRef(resolvedTheme)
  themeRef.current = resolvedTheme
  useEffect(() => {
    if (resolvedTheme === initialThemeRef.current) return
    iframeRef.current?.contentWindow?.postMessage(
      { source: 'otodock-host', type: 'theme', theme: resolvedTheme },
      '*',
    )
  }, [resolvedTheme])

  const openChip = openPrompt ? (
    <div className="absolute inset-x-2 top-2 z-10" data-testid="app-openurl-chip">
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-p-border-light/60 bg-white/95 px-3 py-2 text-xs shadow-md backdrop-blur-sm dark:bg-p-surface/95">
        <span className="text-p-text-secondary">
          {openPrompt.kind === 'url' ? (
            <>This app wants to open <span className="font-medium text-p-text">{openPrompt.origin}</span> in a new tab.</>
          ) : (
            <>This app wants to open <span className="font-medium text-p-text">{openPrompt.label}</span>.</>
          )}
        </span>
        <span className="ml-auto flex gap-1.5">
          <button
            onClick={() => resolveOpenConsent(true)}
            className="rounded-md bg-blue-500 px-2.5 py-1 font-medium text-white transition-colors hover:bg-blue-600"
          >
            Allow
          </button>
          <button
            onClick={() => resolveOpenConsent(false)}
            className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface"
          >
            Block
          </button>
        </span>
      </div>
    </div>
  ) : null

  const title = app.title || app.slug
  const serverBanner = serverStatus && !navigatedAway ? (
    <div className="absolute inset-x-2 top-2 z-10" data-testid="app-server-banner">
      <div className="rounded-lg border border-p-border-light/60 bg-white/95 px-3 py-2 text-xs text-p-text-secondary shadow-md backdrop-blur-sm dark:bg-p-surface/95">
        {serverStatus.state === FRAME_STATE.CONNECTING
          ? <>Connecting <span className="font-medium text-p-text">{title}</span>…</>
          : serverStatus.state === FRAME_STATE.UNREACHABLE
            ? <><span className="font-medium text-p-text">{title}</span> could not be reached — reload the app.</>
          : serverStatus.state === APP_SERVER_STATE.STARTING || serverStatus.state === APP_SERVER_STATE.STOPPED
          ? <>Starting <span className="font-medium text-p-text">{title}</span>…</>
          : serverStatus.state === APP_SERVER_STATE.UNAPPROVED
            ? <><span className="font-medium text-p-text">{title}</span> is waiting for approval.</>
            : serverStatus.state === APP_SERVER_STATE.SECRETS
              ? <><span className="font-medium text-p-text">{title}</span> is waiting for a secret to be set{app.can_manage ? ' — Settings in the menu.' : '.'}</>
            : serverStatus.state === APP_SERVER_STATE.QUOTA_FULL
              ? <><span className="font-medium text-p-text">{title}</span> stopped: its storage quota is full.</>
            : app.can_manage
              ? <><span className="font-medium text-p-text">{title}</span> failed to start — see Logs in the menu.</>
              : <><span className="font-medium text-p-text">{title}</span> is not available right now.</>}
      </div>
    </div>
  ) : null
  const awayNotice = navigatedAway ? (
    <div className="absolute inset-0 z-10 flex items-center justify-center bg-p-bg/90" data-testid="app-navigated-away">
      <div className="max-w-sm rounded-lg border border-p-border-light bg-p-surface px-4 py-3 text-center text-xs text-p-text-secondary shadow-md">
        <p>This app navigated away from its page, so it was stopped.</p>
        <button
          type="button"
          className="mt-2 rounded-md bg-blue-500 px-2.5 py-1 font-medium text-white transition-colors hover:bg-blue-600"
          onClick={() => {
            navigatedAwayRef.current = false
            setNavigatedAway(false)
            setServerStatus(null)
            setNonce((n) => n + 1)
          }}
        >
          Reload the app
        </button>
      </div>
    </div>
  ) : null

  return (
    <div className={`relative w-full ${autoHeight ? '' : 'h-full'}`}>
    {openChip}
    {serverBanner}
    {awayNotice}
    <iframe
      key={src}
      ref={iframeRef}
      src={navigatedAway ? 'about:blank' : src}
      title={title}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      allow=""
      // The src bakes the MOUNT theme; a live-reloaded document would come up
      // stale after a theme switch — re-push on every load (idempotent).
      // (No feed-subscription clear here: the page subscribes BEFORE the
      // load event fires — see the file_updated handler.)
      onLoad={() => {
        if (navigatedAwayRef.current) return
        if (expectedLoadsRef.current > 0) {
          expectedLoadsRef.current -= 1
        } else {
          // A load the host never asked for: the page moved itself (a
          // scripts-only sandbox still allows location.assign). Blank it
          // and post nothing further — the runtime that made the request
          // is gone with the document.
          navigatedAwayRef.current = true
          setNavigatedAway(true)
          if (tokenTimerRef.current) clearTimeout(tokenTimerRef.current)
          return
        }
        iframeRef.current?.contentWindow?.postMessage(
          { source: 'otodock-host', type: 'theme', theme: themeRef.current },
          '*',
        )
        // The runtime is installed by now (it lands before </body>); the
        // page learns its state document through the otodock:state event.
        postState()
      }}
      className={`block w-full border-0 bg-transparent ${autoHeight ? '' : 'h-full'}`}
      // autoHeight: adopt the app's reported content height so the frame
      // never scrolls internally (the page scroll owns it); sensible seed
      // height until the first report lands.
      style={autoHeight ? { height: contentHeight ?? 320 } : undefined}
      scrolling={autoHeight ? 'no' : undefined}
    />
    </div>
  )
}
