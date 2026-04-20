import { useCallback, useEffect, useRef, useState } from 'react'
import { useTheme } from '../../../contexts/ThemeContext'
import { onFileUpdate } from '../../../lib/fileUpdates'
import { emitIframeSwipe } from '../../../lib/iframeGestures'
import FilePreviewPortal from '../../workspace/FilePreviewPortal'

/**
 * Sandboxed display_ui artifact — agent-authored HTML served by /v1/ui/{token}.
 *
 * STANDALONE by design: never reuse DocumentPreview / FilePreviewBody or their
 * `sandbox` string here — those carry `allow-same-origin` for the TRUSTED
 * Collabora editor, and applying it to agent HTML is a full sandbox escape
 * (the document would run same-origin with the dashboard: cookies, storage,
 * ambient-credential API). This iframe is `sandbox="allow-scripts"` ONLY —
 * no same-origin, popups, forms, downloads, or top-navigation tokens.
 *
 * The artifact runs at an opaque origin; the served response's own CSP
 * `sandbox` directive keeps even the open-in-tab path sandboxed.
 */

interface Props {
  token: string
  uiUrl: string
  title?: string
  height?: number
  path?: string
  /** Chat's agent slug — enables live update-in-place: a display_ui rewrite
      of this artifact's workspace file broadcasts `file_updated` and matching
      instances reload (the token serves the file at request time). */
  agent?: string
  /** Interactive PiP window mode: fill the window height, no auto-height. */
  embedded?: boolean
  /** Backchannel sender (otodock.send → chat). Absent on read-only surfaces
      (history, task runs, PiP terminals) — actions then ack `unavailable`. */
  onInteraction?: (token: string, title: string, payload: unknown) => Promise<{ status: string; reason?: string }>
}

type Consent = 'unset' | 'allowed' | 'blocked'

// Keyed by the artifact's workspace path when present (stable across
// same-save_path iterations; falls back to the per-display token). Client-side
// UX guard only — the server-side provenance/rate checks are the boundary.
function consentKey(path: string | undefined, token: string): string {
  return `otodock-artifact-consent:${path || token}`
}

function readConsent(key: string): Consent {
  try {
    const v = localStorage.getItem(key)
    return v === 'allowed' || v === 'blocked' ? v : 'unset'
  } catch { return 'unset' }
}

const MIN_H = 60
const DEFAULT_H = 320

function clampHeight(h: number): number {
  const max = Math.min(Math.round(window.innerHeight * 0.7) || 900, 900)
  return Math.max(MIN_H, Math.min(Math.round(h), max))
}

export default function UiArtifact({ token, uiUrl, title, height, path, agent, embedded, onInteraction }: Props) {
  const { resolvedTheme } = useTheme()
  // Initial theme is baked into the URL; live switches ride postMessage —
  // remounting on theme change would re-run the artifact's scripts.
  const initialThemeRef = useRef(resolvedTheme)
  // Live update-in-place: nonce bumps on a matching file_updated → full
  // reload with the file's current content (same pattern as AppFrame).
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    if (!agent || !path) return
    return onFileUpdate((u) => {
      if (u.agent_slug === agent && u.rel_path === path) setNonce((n) => n + 1)
    })
  }, [agent, path])
  const src = `${uiUrl}?theme=${initialThemeRef.current}${nonce ? `&v=${nonce}` : ''}`
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  const fsIframeRef = useRef<HTMLIFrameElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const fixedHeight = typeof height === 'number' && height > 0
  const [frameH, setFrameH] = useState<number>(fixedHeight ? clampHeight(height) : DEFAULT_H)
  const [visible, setVisible] = useState(!!embedded)
  const [fullscreen, setFullscreen] = useState(false)
  const rafRef = useRef(0)
  const pendingHRef = useRef<number | null>(null)

  // --- Backchannel (otodock.send → chat) ---
  const [consentPrompt, setConsentPrompt] = useState(false)
  const chipRef = useRef<HTMLDivElement | null>(null)
  const pendingActionRef = useRef<{ win: Window; payload: unknown } | null>(null)
  const lastSendAtRef = useRef(0)
  const onInteractionRef = useRef(onInteraction)
  onInteractionRef.current = onInteraction

  const ackTo = (win: Window, status: string, reason?: string) => {
    try {
      win.postMessage(
        { source: 'otodock-host', type: 'action_ack', status, ...(reason ? { reason } : {}) },
        '*',
      )
    } catch { /* frame gone */ }
  }

  const deliverAction = async (win: Window, payload: unknown) => {
    const send = onInteractionRef.current
    if (!send) return ackTo(win, 'unavailable', 'not available in this view')
    const now = Date.now()
    if (now - lastSendAtRef.current < 1000) return ackTo(win, 'denied', 'rate limited')
    lastSendAtRef.current = now
    const ack = await send(token, title || '', payload)
    ackTo(win, ack.status, ack.reason)
  }

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      // Same identity rule as the height listener: the source-window
      // reference, never e.origin ("null" for sandboxed frames). Both of THIS
      // component's instances (inline + fullscreen) are accepted; the ack
      // returns to whichever frame sent the action.
      const win = e.source as Window | null
      const mine = !!win && (
        win === iframeRef.current?.contentWindow ||
        win === fsIframeRef.current?.contentWindow
      )
      if (!mine) return
      const d: any = e.data
      if (!d || d.source !== 'otodock-artifact') return
      if (d.type === 'swipe') {
        // Drawer gesture forwarded out of the frame (iframes swallow
        // touches) — emit the MATCHING frame element; useSwipeGesture
        // containment routes it like a direct touch.
        const frame = win === fsIframeRef.current?.contentWindow
          ? fsIframeRef.current
          : iframeRef.current
        if (frame && (d.dir === 'left' || d.dir === 'right')) {
          emitIframeSwipe({ el: frame, dir: d.dir })
        }
        return
      }
      if (d.type !== 'action') return
      if (!onInteractionRef.current) return ackTo(win!, 'unavailable', 'not available in this view')
      // Consent is read per interaction, not cached at mount — a grant or
      // block from another tab/instance of the same artifact is honored here.
      const consent = readConsent(consentKey(path, token))
      if (consent === 'blocked') { setConsentPrompt(false); return ackTo(win!, 'blocked') }
      if (consent === 'allowed') { setConsentPrompt(false); void deliverAction(win!, d.payload); return }
      // First use: hold the LATEST payload behind the consent chip.
      pendingActionRef.current = { win: win!, payload: d.payload }
      setConsentPrompt(true)
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, path])

  const resolveConsent = (allow: boolean) => {
    const consent: Consent = allow ? 'allowed' : 'blocked'
    try { localStorage.setItem(consentKey(path, token), consent) } catch { /* private mode */ }
    setConsentPrompt(false)
    const pending = pendingActionRef.current
    pendingActionRef.current = null
    if (!pending) return
    if (allow) void deliverAction(pending.win, pending.payload)
    else ackTo(pending.win, 'blocked')
  }

  // Nudge the chip into view when it appears — sticky can't help while the
  // artifact sits entirely below the fold ('nearest' no-ops when already
  // visible; optional call — jsdom and old engines lack scrollIntoView).
  useEffect(() => {
    if (consentPrompt) chipRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [consentPrompt])

  // Sticky inside the wrapper, so on an artifact taller than the viewport
  // (or scrolled past its top edge) the chip pins to the VISIBLE portion
  // instead of rendering offscreen at the wrapper top — an invisible prompt
  // silently held the pending action. The h-0 sentinel keeps it out of flow
  // (no layout shift); the chip overflows downward over the iframe. Must be
  // the wrapper's FIRST child: sticky can only pin from its flow position.
  const consentChip = consentPrompt ? (
    <div className="sticky top-2 z-10 mx-2 h-0" data-testid="ui-consent-chip">
      <div ref={chipRef} className="flex flex-wrap items-center gap-2 rounded-lg border border-p-border-light/60 bg-white/95 px-3 py-2 text-xs shadow-md backdrop-blur-sm dark:bg-p-surface/95">
        <span className="text-p-text-secondary">
          This artifact wants to send an interaction to the chat.
        </span>
        <span className="ml-auto flex gap-1.5">
          <button
            onClick={() => resolveConsent(true)}
            className="rounded-md bg-blue-500 px-2.5 py-1 font-medium text-white transition-colors hover:bg-blue-600"
          >
            Allow
          </button>
          <button
            onClick={() => resolveConsent(false)}
            className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface"
          >
            Block
          </button>
        </span>
      </div>
    </div>
  ) : null

  // Lazy-mount off-screen artifacts: each one may boot ECharts/Tailwind, and
  // /v1/ui is no-store, so scroll-back would otherwise re-run them all.
  useEffect(() => {
    if (embedded) return
    const el = wrapRef.current
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') {
      setVisible(true)
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true)
          io.disconnect()
        }
      },
      { rootMargin: '400px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [embedded])

  // Auto-height from the artifact runtime's height messages.
  useEffect(() => {
    if (fixedHeight || embedded) return
    const onMsg = (e: MessageEvent) => {
      // Identity is the source-window reference. Never trust e.origin — it is
      // "null" for sandboxed frames — and never react to other windows.
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      const d: any = e.data
      if (!d || d.source !== 'otodock-artifact' || d.type !== 'height') return
      if (typeof d.height !== 'number' || !isFinite(d.height)) return
      // rAF-coalesce + delta-gate: a hostile artifact can flood postMessage
      // (the child-side throttle is best-effort only), and applying no-op
      // heights would feed the child's ResizeObserver back-and-forth.
      pendingHRef.current = d.height
      if (rafRef.current) return
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = 0
        const h = pendingHRef.current
        pendingHRef.current = null
        if (h == null) return
        const clamped = clampHeight(h)
        setFrameH((prev) => (Math.abs(prev - clamped) > 1 ? clamped : prev))
      })
    }
    window.addEventListener('message', onMsg)
    return () => {
      window.removeEventListener('message', onMsg)
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
    }
  }, [fixedHeight, embedded])

  // Live theme switch → child (payload carries only the theme string).
  useEffect(() => {
    if (resolvedTheme === initialThemeRef.current) return
    iframeRef.current?.contentWindow?.postMessage(
      { source: 'otodock-host', type: 'theme', theme: resolvedTheme },
      '*',
    )
  }, [resolvedTheme])

  const label = title || 'Generated UI artifact'

  if (embedded) {
    // PiP window mode: the window supplies chrome; fill its height. The
    // consent chip renders here too — PTY chats deliver via terminal
    // injection, gated by the same first-use consent.
    return (
      <div className="relative h-full w-full">
        {consentChip}
        <iframe
          ref={iframeRef}
          src={src}
          title={label}
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          allow=""
          className="block h-full w-full border-0 bg-transparent"
        />
      </div>
    )
  }

  return (
    <>
      <div ref={wrapRef} className="group relative my-1 w-full" data-testid="ui-artifact" data-ui-path={path || undefined}>
        {consentChip}
        {visible ? (
          <iframe
            ref={iframeRef}
            src={src}
            title={label}
            sandbox="allow-scripts"
            referrerPolicy="no-referrer"
            allow=""
            className="block w-full border-0 bg-transparent"
            style={{ height: frameH }}
          />
        ) : (
          // Placeholder keeps scroll geometry until the artifact scrolls near.
          <div style={{ height: frameH }} />
        )}
        {/* Chrome footer: the persistent generated-content affordance
            (anti-phishing — the artifact is deliberately borderless and
            theme-matched, so this ALWAYS-visible marker is what
            distinguishes agent UI from dashboard chrome; never hover-only)
            plus the fullscreen control. A hairline row BELOW the frame, not
            an overlay: any overlay position eventually sits on artifact
            content (bottom-right hit chart axes, top-right hit headers).
            Controls can't be hover-revealed anyway — :hover does NOT
            propagate from a sandboxed (out-of-process) iframe to the
            parent's ancestor chain. */}
        <div className="flex items-center justify-end gap-1 pr-1 pt-0.5">
          <span className="pointer-events-none select-none text-[9px] font-semibold uppercase tracking-[0.08em] text-p-text-light/80">
            generated
          </span>
          {/* No open-in-new-tab affordance: an externally-opened artifact is
              a dead end for the v2 backchannel (top-level document, no parent
              listener) — fullscreen gives the big view WITH the backchannel.
              The route itself stays safe for manual top-level opens (its CSP
              sandbox header), there's just no UI inviting them. */}
          <button
            onClick={() => setFullscreen(true)}
            title="Fullscreen"
            className="rounded-sm p-0.5 text-p-text-light transition-colors hover:bg-p-surface hover:text-p-text-secondary"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15" />
            </svg>
          </button>
        </div>
      </div>

      {/* Fullscreen portal — a SECOND sandboxed instance (no-store content;
          same URL). Mirrors DocumentPreview's STRUCTURE only, never its
          sandbox string. */}
      {fullscreen && (
        <FilePreviewPortal
          filename={label}
          onClose={() => setFullscreen(false)}
          bodyBg="bg-p-bg"
        >
          <div className="relative h-full w-full">
            {consentChip}
            <iframe
              ref={fsIframeRef}
              src={src}
              title={label}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
              allow=""
              className="h-full w-full border-0 bg-transparent"
            />
          </div>
        </FilePreviewPortal>
      )}
    </>
  )
}
