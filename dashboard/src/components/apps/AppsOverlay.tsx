import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useApps, useHideAppForMe, useReorderApps, useUnhideAppForMe, useUnpinApp, type PinnedApp } from '../../api/apps'
import { onFileUpdate } from '../../lib/fileUpdates'
import { useQueryClient } from '@tanstack/react-query'
import AppFrame from './AppFrame'
import AppApprovalCard, { appNeedsApproval } from './AppApprovalCard'

/**
 * Pinned mini-apps overlay — swaps the message-list slot like
 * ProjectsOverlay: a reorderable chip strip (shared apps first, then the
 * viewer's personal ones; order[0] is the default tab) over a sandboxed
 * AppFrame, with the declared-actions approval card when a manifest is
 * pending. Reorder: drag on desktop, long-press → move arrows on mobile.
 *
 * The strip copies the workspace ScopeChips design (single scrollable
 * snap-x row, fade gradients, active-chip auto-scroll) and its ownership
 * colors: personal apps brand-blue, shared apps accent-purple — the same
 * scope language as the workspace view, which also answers "where is this
 * app's file?" at a glance. Unpinning is a two-step confirm on the ACTIVE
 * chip's inline X (browser-tab style, so the X visually binds to what it
 * removes) — unpin is soft server-side: file, manifest and approval all
 * survive a re-pin.
 */

interface Props {
  agent: string
  onSendPrompt?: (app: PinnedApp, action: { id: string; label: string; prompt: string }, args: unknown) => Promise<{ status: string; reason?: string }>
  /** False when the host page already renders content above (the agent
      home's live-sessions strip carries the floating-TopBar clearance). */
  topPadding?: boolean
}

const IS_DESKTOP = typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && !window.matchMedia('(hover: none)').matches
const LONG_PRESS_MS = 450

export default function AppsOverlay({ agent, onSendPrompt, topPadding = true }: Props) {
  const { data: apps, isLoading } = useApps(agent)
  const unpin = useUnpinApp(agent)
  const hideForMe = useHideAppForMe(agent)
  const unhide = useUnhideAppForMe(agent)
  const reorder = useReorderApps(agent)
  const qc = useQueryClient()

  const [activeId, setActiveId] = useState<string | null>(null)
  const [armedId, setArmedId] = useState<string | null>(null) // mobile move-arrows
  // The app id the X was clicked on — confirm renders only while that app
  // is STILL the active tab, so the confirm can never unpin anything else.
  const [confirmUnpinId, setConfirmUnpinId] = useState<string | null>(null)
  const [showHidden, setShowHidden] = useState(false)
  // The app document's own scroll offset (AppFrame's onScrollY bridge) —
  // slides the solo-app ✕ away with the content instead of hovering
  // permanently over it. Reset on tab switch; AppFrame resets on reload.
  const [frameScrollY, setFrameScrollY] = useState(0)
  const dragIdRef = useRef<string | null>(null)
  const longPressRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const touchStartRef = useRef<{ x: number; y: number } | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const activeChipRef = useRef<HTMLDivElement | null>(null)
  const [fadeLeft, setFadeLeft] = useState(false)
  const [fadeRight, setFadeRight] = useState(false)

  // Per-user hidden shared rows stay OFF the strip but power the
  // "+N hidden" restore affordance (S2).
  const list = useMemo(() => (apps ?? []).filter((a) => !a.hidden_for_me), [apps])
  const hiddenList = useMemo(() => (apps ?? []).filter((a) => a.hidden_for_me), [apps])
  const active = list.find((a) => a.id === activeId) ?? list[0] ?? null
  const activeKey = active?.id ?? ''

  // A pin from the agent registers a NEW row without touching the list cache
  // — any file_updated under an apps/ path refreshes the registry view.
  useEffect(() => onFileUpdate((u) => {
    if (u.agent_slug === agent && /(^|\/)apps\/[^/]+\.html$/.test(u.rel_path)) {
      qc.invalidateQueries({ queryKey: ['apps', agent] })
    }
  }), [agent, qc])

  // Fade edges (ScopeChips): recompute on scroll/resize/list change.
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const update = () => {
      setFadeLeft(el.scrollLeft > 2)
      setFadeRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 2)
    }
    update()
    el.addEventListener('scroll', update, { passive: true })
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => {
      el.removeEventListener('scroll', update)
      ro.disconnect()
    }
  }, [list])

  // Scroll the active chip into view only when not already fully visible
  // (unguarded scrollIntoView on a snap-x container phantom-scrolls on
  // mount — same reasoning as ScopeChips).
  useEffect(() => {
    const chip = activeChipRef.current
    const container = scrollRef.current
    if (!chip || !container) return
    const chipLeft = chip.offsetLeft - container.offsetLeft
    const chipRight = chipLeft + chip.offsetWidth
    if (chipLeft < container.scrollLeft || chipRight > container.scrollLeft + container.clientWidth) {
      chip.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' })
    }
  }, [activeKey])

  // Tab switch shows a fresh document at scroll 0 — snap the ✕ back.
  useEffect(() => { setFrameScrollY(0) }, [activeKey])

  const applyOrder = (ids: string[]) => {
    // Optimistic tab order; the server renumbers within each scope group and
    // the invalidate reconciles (viewers moving shared rows get a 403 toast
    // state via the mutation error — the refetch restores truth).
    qc.setQueryData(['apps', agent], (prev: PinnedApp[] | undefined) => {
      if (!prev) return prev
      const by = new Map(prev.map((a) => [a.id, a]))
      return ids.map((id) => by.get(id)).filter(Boolean) as PinnedApp[]
    })
    reorder.mutate(ids)
  }

  const move = (id: string, delta: number) => {
    const ids = list.map((a) => a.id)
    const i = ids.indexOf(id)
    const j = i + delta
    if (i < 0 || j < 0 || j >= ids.length) return
    const next = [...ids]
    ;[next[i], next[j]] = [next[j], next[i]]
    applyOrder(next)
  }

  const onDrop = (targetId: string) => {
    const dragId = dragIdRef.current
    dragIdRef.current = null
    if (!dragId || dragId === targetId) return
    const ids = list.map((a) => a.id)
    const from = ids.indexOf(dragId)
    const to = ids.indexOf(targetId)
    if (from < 0 || to < 0) return
    const next = [...ids]
    next.splice(from, 1)
    next.splice(to, 0, dragId)
    applyOrder(next)
  }

  const startLongPress = (id: string, e: React.TouchEvent) => {
    touchStartRef.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }
    longPressRef.current = setTimeout(() => setArmedId((v) => (v === id ? null : id)), LONG_PRESS_MS)
  }
  const cancelLongPress = (e?: React.TouchEvent) => {
    if (e && touchStartRef.current) {
      const t = e.touches[0]
      if (t && Math.hypot(t.clientX - touchStartRef.current.x, t.clientY - touchStartRef.current.y) < 8) return
    }
    if (longPressRef.current) { clearTimeout(longPressRef.current); longPressRef.current = null }
  }

  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center bg-p-bg pt-16 text-sm text-p-text-light">
        Loading mini-apps…
      </div>
    )
  }

  if (!list.length) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 bg-p-bg px-6 pt-16 text-center">
        <svg className="h-8 w-8 text-p-text-light" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <rect x="3.75" y="3.75" width="7" height="7" rx="1.5" />
          <rect x="13.25" y="3.75" width="7" height="7" rx="1.5" />
          <rect x="3.75" y="13.25" width="7" height="7" rx="1.5" />
          <rect x="13.25" y="13.25" width="7" height="7" rx="1.5" />
        </svg>
        <p className="text-sm font-medium text-p-text-secondary">
          {hiddenList.length ? 'All shared apps are hidden' : 'No mini-apps pinned yet'}
        </p>
        {hiddenList.length ? (
          <div className="flex flex-wrap items-center justify-center gap-1.5">
            {hiddenList.map((a) => (
              <button
                key={a.id}
                onClick={() => unhide.mutate(a.id)}
                className="rounded-full border border-dashed border-p-border px-3 py-1 text-xs text-p-text-secondary transition-colors hover:bg-p-surface-hover"
                title="Restore to your strip"
              >
                {a.title || a.slug} ↺
              </button>
            ))}
          </div>
        ) : (
          <p className="max-w-sm text-xs text-p-text-light">
            Ask the agent to pin one — e.g. “pin a morning-brief dashboard as a
            mini-app” — and it appears here for every visit, refreshed by tasks.
          </p>
        )}
      </div>
    )
  }

  const needsApproval = appNeedsApproval(active)
  const confirmTarget = confirmUnpinId && active?.id === confirmUnpinId ? active : null
  const stripVisible = list.length > 1 || hiddenList.length > 0
  // With the strip hidden, the lone app's removal entry point moves to a
  // small overlay ✕ on the frame — REQUIRED for shared rows: the agent has
  // no per-user hide path (unpin_app is personal-own or shared-for-everyone
  // only), so without it a viewer could never park a team dashboard off
  // their own strip (S2).
  const soloRemovable =
    !stripVisible && !!active && (active.can_manage || active.scope === 'shared')

  return (
    <div className={`flex flex-1 min-h-0 flex-col bg-p-bg ${topPadding ? 'pt-14' : 'pt-1'}`}>
      {/* Chip strip (ScopeChips design; scope colors match the workspace).
          Hidden with a SINGLE visible app — personal OR shared alike
          (operator call, 2026-08-15): the common shape is one agent
          dashboard, and the front-page auto-open shows it clean, no tab
          chrome. Removal moves to the overlay ✕ (same confirm block; shared
          rows keep the S2 hide-for-me / unpin-for-everyone split). The
          strip returns the moment a second app is pinned — and stays
          whenever hidden apps exist: the "+N hidden" restore chips have no
          other home. */}
      {stripVisible && (
      <div className="relative border-b border-p-border-light/60">
        <div
          ref={scrollRef}
          className="flex gap-1.5 px-3 py-2 overflow-x-auto scrollbar-hide snap-x scroll-pl-3 scroll-pr-3"
          style={{ scrollBehavior: 'smooth' }}
        >
          {list.map((a) => {
            const isActive = a.id === activeKey
            const armed = armedId === a.id
            const pending = a.actions.length > 0 && (!a.actions_approved || a.approval_stale)
            const purple = a.scope === 'shared'
            const chipClass = purple
              ? isActive
                ? 'bg-p-accent-purple text-white border-p-accent-purple'
                : 'bg-p-accent-purple/10 text-p-accent-purple border-p-accent-purple/30 hover:bg-p-accent-purple/20'
              : isActive
                ? 'bg-brand text-white border-brand'
                : 'bg-brand/10 text-brand border-brand/30 hover:bg-brand/20'
            return (
              <div key={a.id} className="flex shrink-0 snap-start items-center">
                {armed && (
                  <button
                    onClick={() => move(a.id, -1)}
                    className="px-1 text-p-text-secondary hover:text-p-text"
                    aria-label="Move left"
                  >‹</button>
                )}
                {/* div+role, not <button>: the active chip nests the unpin
                    X (interactive elements can't nest). */}
                <div
                  ref={isActive ? activeChipRef : undefined}
                  role="button"
                  tabIndex={0}
                  draggable={IS_DESKTOP}
                  onDragStart={() => { dragIdRef.current = a.id }}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={() => onDrop(a.id)}
                  onTouchStart={(e) => startLongPress(a.id, e)}
                  onTouchMove={(e) => cancelLongPress(e)}
                  onTouchEnd={() => cancelLongPress()}
                  onClick={() => { setActiveId(a.id); setArmedId(null); setConfirmUnpinId(null) }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      setActiveId(a.id); setArmedId(null); setConfirmUnpinId(null)
                    }
                  }}
                  title={purple ? `${a.title || a.slug} (shared)` : `${a.title || a.slug} (personal)`}
                  className={`flex cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1 text-xs font-medium transition-colors ${chipClass}`}
                >
                  {a.title || a.slug}
                  {pending && (
                    <span className={`h-1.5 w-1.5 rounded-full ${isActive ? 'bg-white' : 'bg-amber-500'}`}
                          title="Actions pending approval" />
                  )}
                  {isActive && (a.can_manage || a.scope === 'shared') && (
                    <button
                      onClick={(e) => { e.stopPropagation(); setConfirmUnpinId(a.id) }}
                      title={a.scope === 'shared' && !a.can_manage
                        ? 'Hide this app from my strip' : 'Remove this app'}
                      aria-label={`Remove ${a.title || a.slug}`}
                      className="-mr-1 rounded-full p-0.5 text-white/70 transition-colors hover:bg-white/20 hover:text-white"
                    >
                      <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  )}
                </div>
                {armed && (
                  <button
                    onClick={() => move(a.id, 1)}
                    className="px-1 text-p-text-secondary hover:text-p-text"
                    aria-label="Move right"
                  >›</button>
                )}
              </div>
            )
          })}
          {hiddenList.length > 0 && (
            <div className="flex shrink-0 snap-start items-center gap-1.5">
              <button
                onClick={() => setShowHidden((v) => !v)}
                className="whitespace-nowrap rounded-full border border-dashed border-p-border px-3 py-1 text-xs text-p-text-light transition-colors hover:bg-p-surface-hover hover:text-p-text-secondary"
                title="Shared apps you hid from your strip"
              >
                +{hiddenList.length} hidden
              </button>
              {showHidden && hiddenList.map((a) => (
                <button
                  key={a.id}
                  onClick={() => { unhide.mutate(a.id); setShowHidden(hiddenList.length > 1) }}
                  className="whitespace-nowrap rounded-full border border-dashed border-p-accent-purple/40 px-3 py-1 text-xs text-p-accent-purple/70 transition-colors hover:bg-p-accent-purple/10 hover:text-p-accent-purple"
                  title="Restore to your strip"
                >
                  {a.title || a.slug} ↺
                </button>
              ))}
            </div>
          )}
        </div>
        {fadeLeft && (
          <div className="pointer-events-none absolute left-0 top-0 bottom-0 w-6 bg-linear-to-r from-p-bg to-transparent" />
        )}
        {fadeRight && (
          <div className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 bg-linear-to-l from-p-bg to-transparent" />
        )}
      </div>
      )}

      {/* Removal confirmation — always names its target; the X sits on the
          active chip (or the solo-app frame overlay when the strip is
          hidden), and a tab switch cancels the pending confirm. Shared rows
          split (S2): "Hide for me" parks it off THIS user's strip only (any
          role); "Unpin for everyone" is the team-wide soft-unpin (editor+,
          can_manage). Personal rows keep the single unpin. */}
      {confirmTarget && (
        <div className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-p-border-light bg-p-surface px-3 py-2.5 text-xs">
          <span className="text-p-text">
            {confirmTarget.scope === 'shared'
              ? `Remove “${confirmTarget.title || confirmTarget.slug}”? Hiding
                 affects only your strip. Restore it anytime from the “hidden”
                 chip, or ask your agents to pin it back later.`
              : `Unpin “${confirmTarget.title || confirmTarget.slug}”? The
                 workspace file and the approved actions are kept — ask the
                 agent to pin it back anytime.`}
          </span>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {confirmTarget.scope === 'shared' && (
              <button
                onClick={() => {
                  setConfirmUnpinId(null)
                  setActiveId(null)
                  hideForMe.mutate(confirmTarget.id)
                }}
                className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
              >
                Hide for me
              </button>
            )}
            {(confirmTarget.scope !== 'shared' || confirmTarget.can_manage) && (
              <button
                onClick={() => {
                  setConfirmUnpinId(null)
                  setActiveId(null)
                  unpin.mutate(confirmTarget.id)
                }}
                className="rounded-md bg-red-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-red-700"
              >
                {confirmTarget.scope === 'shared' ? 'Unpin for everyone' : 'Unpin'}
              </button>
            )}
            <button
              onClick={() => setConfirmUnpinId(null)}
              className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Declared-actions approval card (shared with the Dock) */}
      {needsApproval && active && (
        <AppApprovalCard key={active.id} app={active} agent={agent} />
      )}

      {/* The app itself. `isolate` caps the internal z-layers (✕ over the
          openurl chip) inside this subtree — without it the ✕'s z-20 ties
          with the absolute TopBar's z-20 and DOM order painted it OVER the
          notifications panel (whose z-50 lives INSIDE the TopBar's
          context). `overflow-hidden` clips the ✕ as it slides away. */}
      <div className="relative isolate overflow-hidden flex-1 min-h-0 p-2">
        {active && (
          <AppFrame
            app={active}
            agent={agent}
            onSendPrompt={onSendPrompt}
            onScrollY={setFrameScrollY}
          />
        )}
        {/* Solo-app overlay ✕ — small, semi-transparent, INSIDE the frame's
            top-right corner (no reserved chrome height). z-20 clears the
            frame's own overlays (openurl consent chip is z-10). Opens the
            same confirm block as the chip ✕. Anchored to the DOCUMENT's
            top-right: the shim reports the app's scroll offset and the ✕
            slides away with the content (no transition — it must track the
            finger), going inert once mostly gone. */}
        {soloRemovable && !confirmTarget && active && (
          <button
            onClick={() => setConfirmUnpinId(active.id)}
            title={active.scope === 'shared' && !active.can_manage
              ? 'Hide this app from my strip' : 'Remove this app'}
            aria-label={`Remove ${active.title || active.slug}`}
            style={{
              transform: `translateY(${-Math.min(frameScrollY, 96)}px)`,
              opacity: Math.max(0, 1 - frameScrollY / 56),
              pointerEvents: frameScrollY > 40 ? 'none' : 'auto',
            }}
            className="absolute top-3.5 right-3.5 z-20 flex h-6 w-6 items-center justify-center rounded-full border border-p-border-light bg-p-bg/70 text-p-text-light backdrop-blur-sm transition-colors hover:bg-p-surface-hover hover:text-p-text"
          >
            <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        )}
      </div>
    </div>
  )
}
