import { useEffect, useRef, useState } from 'react'
import { useTheme } from '../../contexts/ThemeContext'
import { onFileUpdate } from '../../lib/fileUpdates'
import { emitIframeSwipe } from '../../lib/iframeGestures'
import { fireAppAction, type PinnedApp } from '../../api/apps'
import { useActiveChats } from '../../hooks/useActiveChats'

/**
 * Sandboxed pinned mini-app frame — agent-authored HTML served cookie-authed
 * by /v1/apps/{id}/html under the same opaque-origin CSP as /v1/ui.
 *
 * Same STANDALONE iframe hygiene as UiArtifact (sandbox="allow-scripts" ONLY,
 * source-window identity on every message, e.origin never trusted). The
 * interaction contract differs: apps invoke DECLARED actions by id
 * (otodock.action) gated by the user-approved manifest — there is no per-send
 * consent chip (the approval IS the standing consent) and free-form
 * otodock.send acks `unavailable` here.
 *
 * Data feeds (`otodock.feed`): the page subscribes to DECLARED read-only
 * platform feeds; THIS component answers from the viewing user's own
 * authenticated context (the iframe never holds a credential, CSP
 * connect-src 'none' stays) — initial snapshot on subscribe, a push on every
 * change. `active_chats` is self-served (the sidebar widget's hook);
 * `project_lanes` rows arrive as a prop from the Dock overlay, which owns
 * the viewer-scoped /project poll — absent elsewhere, so the subscription
 * answers with an error the page can render.
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
}

// autoHeight clamp: a hostile/broken page can post any number — keep the
// frame tall enough to be usable and short enough to never trap the page.
const AUTO_HEIGHT_MIN = 120
const AUTO_HEIGHT_MAX = 8000

export default function AppFrame({ app, agent, onSendPrompt, projectLanes, autoHeight = false }: Props) {
  const { resolvedTheme } = useTheme()
  const initialThemeRef = useRef(resolvedTheme)
  // Nonce bumps on file_updated → full reload (the served content is
  // no-store; re-setting src re-runs the app's scripts on fresh HTML).
  const [nonce, setNonce] = useState(0)
  const src = `/v1/apps/${app.id}/html?theme=${initialThemeRef.current}&v=${nonce}`
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  const lastSendAtRef = useRef(0)
  const lastActionAtRef = useRef<Map<string, number>>(new Map())
  const onSendPromptRef = useRef(onSendPrompt)
  onSendPromptRef.current = onSendPrompt
  const appRef = useRef(app)
  appRef.current = app

  // Task-driven refresh: pin_app broadcasts file_updated for the app's
  // workspace file — reload the frame live (same matching rule as
  // useCollaboraLiveReload: agent_slug + rel_path). Feed subscriptions die
  // with the outgoing document HERE, at reload initiation — NOT in onLoad:
  // the page subscribes while parsing, BEFORE the load event, so an onLoad
  // clear wipes a fresh subscription and every later push is skipped
  // (found live on T1 — the initial snapshot arrived, updates never did).
  useEffect(() => onFileUpdate((u) => {
    if (u.agent_slug === agent && u.rel_path === app.rel_path) {
      feedSubsRef.current.clear()
      setNonce((n) => n + 1)
    }
  }), [agent, app.rel_path])

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
  // Push on change to every live subscription.
  useEffect(() => {
    if (feedSubsRef.current.has('active_chats')) postFeed('active_chats', activeChats)
  }, [activeChats])
  useEffect(() => {
    if (projectLanes && feedSubsRef.current.has('project_lanes')) {
      postFeed('project_lanes', projectLanes)
    }
  }, [projectLanes])

  // Content height reported by the shim (autoHeight mode only).
  const [contentHeight, setContentHeight] = useState<number | null>(null)

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const win = e.source as Window | null
      if (!win || win !== iframeRef.current?.contentWindow) return
      const d: any = e.data
      if (!d || d.source !== 'otodock-artifact') return
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
      const ack = (status: string, reason?: string) => {
        try {
          win.postMessage(
            { source: 'otodock-host', type: 'action_ack', status, ...(reason ? { reason } : {}) },
            '*',
          )
        } catch { /* frame gone */ }
      }
      if (d.type === 'action') {
        // Free-form otodock.send has no chat binding in app context.
        return ack('unavailable', 'mini-apps use declared actions')
      }
      if (d.type === 'feed_subscribe') {
        const feed = String(d.feed || '')
        const current = appRef.current
        if (!current.actions.some((a) => a.type === 'data_feed' && a.feed === feed)) {
          return postFeed(feed, null, 'feed not declared in this app\'s manifest')
        }
        if (!current.actions_approved) {
          return postFeed(feed, null, 'actions not approved')
        }
        feedSubsRef.current.add(feed)
        const { rows, error } = feedRows(feed)
        return postFeed(feed, rows ?? null, error)
      }
      if (d.type !== 'app_action') return
      const current = appRef.current
      const action = current.actions.find((a) => a.id === String(d.id || ''))
      if (!action) return ack('denied', 'unknown action')
      if (action.type === 'data_feed') {
        return ack('denied', 'feeds are subscriptions — use otodock.feed(name, cb)')
      }
      // mcp_tool pages await `otodock:action-result` to re-enable controls
      // and clear spinners — EVERY accepted-or-refused invocation must end
      // in exactly one terminal result (rate-denials and network failures
      // included; a swallowed refusal is a forever-spinner, found live on
      // the trusted VM after fast button bursts).
      const postResult = (ok: boolean, result: string) => {
        if (action.type !== 'mcp_tool') return
        try {
          win.postMessage(
            { source: 'otodock-host', type: 'action_result', id: action.id, ok, result },
            '*',
          )
        } catch { /* frame gone */ }
      }
      if (!current.actions_approved) {
        const reason = current.approval_stale ? 'approval stale' : 'actions not approved'
        ack('denied', reason)
        return postResult(false, reason)
      }
      const now = Date.now()
      if (action.type === 'send_prompt') {
        // Global 1s across the frame — every delivery costs an agent turn.
        if (now - lastSendAtRef.current < 1000) return ack('denied', 'rate limited')
        lastSendAtRef.current = now
      } else {
        // mcp_tool / fire_task: rate per ACTION+ARGS so a data panel fires
        // its queries together AND one parameterized action can serve many
        // widgets (a `toggle` with the entity in args). Short window — this
        // only swallows accidental double-events; the server enforces the
        // real args-aware min-interval + in-flight.
        let argsKey = ''
        try { argsKey = JSON.stringify(d.args) ?? '' } catch { /* non-JSON args are denied server-side */ }
        const key = `${action.id}|${argsKey}`
        const last = lastActionAtRef.current.get(key) || 0
        if (now - last < 400) {
          ack('denied', 'rate limited')
          return postResult(false, 'Rate limited — slow down')
        }
        lastActionAtRef.current.set(key, now)
      }
      if (action.type === 'fire_task' || action.type === 'mcp_tool') {
        // Args ride to the server's user-approved schema gate (client sends
        // them verbatim; validation is never client-side). mcp_tool resolves
        // with the tool's output — bridged into the frame as action_result →
        // the in-page `otodock:action-result` event.
        void fireAppAction(current.id, action.id, d.args).then((r) => {
          ack(r.status, r.reason)
          postResult(r.status === 'done' || r.status === 'sent',
                     r.result ?? r.reason ?? r.status)
        }).catch(() => {
          ack('error', 'network error')
          postResult(false, 'Network error — the call may not have reached the server')
        })
        return
      }
      const send = onSendPromptRef.current
      if (!send) return ack('unavailable', 'not available in this view')
      void send(current, { id: action.id, label: action.label, prompt: action.prompt || '' }, d.args)
        .then((r) => ack(r.status, r.reason))
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

  return (
    <iframe
      ref={iframeRef}
      src={src}
      title={app.title || app.slug}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      allow=""
      // The src bakes the MOUNT theme; a live-reloaded document would come up
      // stale after a theme switch — re-push on every load (idempotent).
      // (No feed-subscription clear here: the page subscribes BEFORE the
      // load event fires — see the file_updated handler.)
      onLoad={() => {
        iframeRef.current?.contentWindow?.postMessage(
          { source: 'otodock-host', type: 'theme', theme: themeRef.current },
          '*',
        )
      }}
      className={`block w-full border-0 bg-transparent ${autoHeight ? '' : 'h-full'}`}
      // autoHeight: adopt the app's reported content height so the frame
      // never scrolls internally (the page scroll owns it); sensible seed
      // height until the first report lands.
      style={autoHeight ? { height: contentHeight ?? 320 } : undefined}
      scrolling={autoHeight ? 'no' : undefined}
    />
  )
}
