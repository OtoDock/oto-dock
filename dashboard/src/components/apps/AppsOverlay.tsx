import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { rollbackNoticeText, useApps, useHideAppForMe, usePurgeApp, useReorderApps, useRollbackApp, useUnhideAppForMe, useUnpinApp, type PinnedApp } from '../../api/apps'
import { GRANTEE_KIND, useHideShare, usePatchShare, useUnhideShare } from '../../api/shares'
import { roleLabel } from '../../lib/permissions'
import { onFileUpdate } from '../../lib/fileUpdates'
import { onAppDeployed } from '../../lib/appLive'
import { appKind } from '../../lib/kinds/app'
import { DEPLOY_STATE } from '../../lib/status/appDeploy'
import { useQueryClient } from '@tanstack/react-query'
import AppFrame from './AppFrame'
import AppMenu from './AppMenu'
import { appChipClass, cameByShare, unpinWords } from './appRow'

/** The sentence of "Where it comes from" that says which sessions may call
 * a placed app's exported methods (SHARING.md "Agents use a placed app"):
 * worded from the share's kind and cap, never from `viewer_role` (the
 * strongest share across every panel). */
function placedCallsNote(a: PinnedApp): string {
  const n = Object.keys(a.exports?.methods ?? {}).length
  if (!a.placement || !n) return ''
  const methods = `its ${n} exported method${n > 1 ? 's' : ''}`
  const cap = roleLabel(a.placement.role_cap).toLowerCase()
  if (a.placement.kind === GRANTEE_KIND.PERSON) return ` Your own chats and tasks here may call ${methods} as ${cap}, while they run in your personal space (never on a Shared-only agent).`
  return ` Chats and tasks of this agent may call ${methods}, up to ${cap}. A task with no person calls only those without a role floor.`
}
import SharePopover from '../sharing/SharePopover'
import AppApprovalCard, { appNeedsApproval } from './AppApprovalCard'
import AppDeployCard from './AppDeployCard'
import AppLogsPanel from './AppLogsPanel'
import AppSettingsPanel from './AppSettingsPanel'

/**
 * Pinned apps overlay — swaps the message-list slot like
 * ProjectsOverlay: a reorderable chip strip (shared apps first, then the
 * viewer's personal ones; order[0] is the default tab) over a sandboxed
 * AppFrame, with the declared-actions approval card when a manifest is
 * pending. Reorder: drag on desktop, long-press → move arrows on mobile.
 *
 * The strip copies the workspace ScopeChips design (single scrollable
 * snap-x row, fade gradients, active-chip auto-scroll) and its ownership
 * colors: personal apps brand-blue, shared apps accent-purple — the same
 * scope language as the workspace view, which also answers "where is this
 * app's file?" at a glance. A row a share brought here keeps the fill of
 * whose it is (blue for the viewer's own share, purple for a team
 * placement) and wears a teal border and the share mark (`appRow.ts`).
 * The ACTIVE chip carries the app's three-dot menu (open full screen,
 * share, hide for me, unpin); unpin is a two-step confirm and soft
 * server-side: file, manifest and approval all survive a re-pin (an app
 * whose live release has a server says "Stop app": the soft unpin stops it
 * and keeps its data). The active tab rides `?app=<id>` so a reload or a
 * shared link lands on the same dashboard.
 *
 * A row placed here by a share (SHARING.md; `placement` set) belongs to
 * another agent: its frame and cards get the row's own agent, its menu is
 * the reduced one (open, hide for me, where it comes from, remove from this
 * agent; on a person's own placement "Remove for me", the revoke of their
 * share, in place of hide), it is never reordered, and its hide is the
 * share's, so the home agent's list is untouched.
 */

interface Props {
  agent: string
  onSendPrompt?: (app: PinnedApp, action: { id: string; label: string; prompt: string }, args: unknown) => Promise<{ status: string; reason?: string }>
  /** False when the host page already renders content above (the agent
      home's live-sessions strip carries the floating-TopBar clearance). */
  topPadding?: boolean
  /** The active tab, when the page owns it (useOverlayPanels lifts it so an
      agent's open request can select a tab before the overlay mounts);
      absent, the overlay keeps its own from `?app=`. */
  activeId?: string | null
  onSelect?: (id: string) => void
}

const IS_DESKTOP = typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && !window.matchMedia('(hover: none)').matches
const LONG_PRESS_MS = 450

/** The rollback confirm, shared by the overlay and the full-screen page:
 * it names what moves (every viewer, and a folder app's data). */
export function RollbackConfirm({ app, busy, onConfirm, onCancel }: {
  app: PinnedApp; busy: boolean; onConfirm: () => void; onCancel: () => void
}) {
  const title = app.title || app.slug
  return (
    <div className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-amber-500/40 bg-amber-500/5 px-3 py-2.5 text-xs" data-testid="app-rollback-confirm">
      <span className="text-p-text">
        Roll back “{title}” to the previous release? Everyone switches at once.
        {appKind(app).keepsData && (
          <> Its data goes back to before this release. Newer changes are kept in a snapshot.</>
        )}
      </span>
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <button
          onClick={onConfirm}
          disabled={busy}
          className="rounded-md bg-amber-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-amber-700 disabled:opacity-60"
        >
          {busy ? 'Rolling back…' : 'Roll back'}
        </button>
        <button
          onClick={onCancel}
          className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

/** The unpin confirm, shared by the overlay and the full-screen page: it
 * names the app and what the soft unpin keeps; an app whose live release
 * runs a server says it stops (`unpinWords`). */
export function UnpinConfirm({ app, onConfirm, onCancel }: {
  app: PinnedApp; onConfirm: () => void; onCancel: () => void
}) {
  const words = unpinWords(app)
  return (
    <div className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-p-border-light bg-p-surface px-3 py-2.5 text-xs" data-testid="app-unpin-confirm">
      <span className="text-p-text">{words.question} {words.detail}</span>
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <button
          onClick={onConfirm}
          className="rounded-md bg-red-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-red-700"
        >
          {words.action}
        </button>
        <button
          onClick={onCancel}
          className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

/** What a rollback did (or why it was refused), until dismissed. */
export function RollbackNotice({ notice, onClose }: {
  notice: { text: string; error: boolean }; onClose: () => void
}) {
  return (
    <div
      className={`mx-3 mt-2 flex items-start gap-2 rounded-xl border px-3 py-2.5 text-xs ${notice.error ? 'border-red-500/40 bg-red-500/5' : 'border-emerald-500/40 bg-emerald-500/5'}`}
      data-testid="app-rollback-notice"
      role="status"
    >
      <span className="flex-1 text-p-text">{notice.error ? `Roll back failed: ${notice.text}` : notice.text}</span>
      <button
        onClick={onClose}
        aria-label="Dismiss"
        className="shrink-0 rounded-full px-1.5 text-p-text-light transition-colors hover:bg-p-surface-hover hover:text-p-text"
      >
        ×
      </button>
    </div>
  )
}

export default function AppsOverlay({
  agent, onSendPrompt, topPadding = true, activeId: controlledActiveId, onSelect,
}: Props) {
  const { data: apps, isLoading } = useApps(agent)
  const unpin = useUnpinApp(agent)
  const hideForMe = useHideAppForMe(agent)
  const unhide = useUnhideAppForMe(agent)
  const rollback = useRollbackApp(agent)
  const purge = usePurgeApp(agent)
  const reorder = useReorderApps(agent)
  const hideShare = useHideShare()
  const unhideShare = useUnhideShare()
  const patchShare = usePatchShare()
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [localActiveId, setLocalActiveId] = useState<string | null>(() => searchParams.get('app'))
  const activeId = controlledActiveId !== undefined ? controlledActiveId : localActiveId
  // null = "no preference" after the active tab went away: the first tab
  // takes over through the `?? list[0]` fallback, so a page-owned id needs
  // no reset (a stale id falls back the same way).
  const setActiveId = (id: string | null) => {
    if (onSelect) {
      if (id) onSelect(id)
      return
    }
    setLocalActiveId(id)
    if (!id) return
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('app', id)
      return next
    }, { replace: true })
  }
  const [armedId, setArmedId] = useState<string | null>(null) // mobile move-arrows
  // The app id the menu's Unpin was chosen on — confirm renders only while
  // that app is STILL the active tab, so the confirm can never unpin
  // anything else.
  const [confirmUnpinId, setConfirmUnpinId] = useState<string | null>(null)
  // Roll back is a two-step too: it moves every viewer and, for a folder
  // app, its data. The result stays on the card slot until dismissed.
  const [confirmRollbackId, setConfirmRollbackId] = useState<string | null>(null)
  const [rollbackNotice, setRollbackNotice] = useState<{ appId: string; text: string; error: boolean } | null>(null)
  const [shareId, setShareId] = useState<string | null>(null)
  const [showHidden, setShowHidden] = useState(false)
  // Folder apps (APPS.md): the logs panel and the working-copy preview are
  // per app id, so switching tabs never carries them over.
  const [logsId, setLogsId] = useState<string | null>(null)
  // The settings panel (APPS.md "Secrets"), per app id like the logs.
  const [settingsId, setSettingsId] = useState<string | null>(null)
  const [previewIds, setPreviewIds] = useState<Set<string>>(() => new Set())
  // "Delete app and its data": the slug typed back, on the active tab only.
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [deleteTyped, setDeleteTyped] = useState('')
  // A placed row's "Where it comes from" notice and its remove confirm
  // (the share's revoke: a team placement's, or a person's own), on the
  // active tab only.
  const [whereFromId, setWhereFromId] = useState<string | null>(null)
  // The confirm remembers the share it was opened for: a refetch that gives
  // the row another identity (a team share made or revoked meanwhile) hides
  // it rather than revoking a share the person did not choose.
  const [confirmRemove, setConfirmRemove] = useState<{ appId: string; shareId: string; kind: string } | null>(null)
  const togglePreview = (id: string) => setPreviewIds((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
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

  const selectTab = (id: string) => {
    setActiveId(id)
    setArmedId(null)
    setConfirmUnpinId(null)
    setConfirmRollbackId(null)
    setConfirmRemove(null)
    setWhereFromId(null)
  }
  const runRollback = (a: PinnedApp) => {
    setConfirmRollbackId(null)
    const left = a.release ?? 0
    rollback.mutate(a.id, {
      onSuccess: (r) => setRollbackNotice({ appId: a.id, text: rollbackNoticeText(a, left, r), error: false }),
      onError: (e) => setRollbackNotice({ appId: a.id, text: (e as Error).message, error: true }),
    })
  }

  // A pin from the agent registers a NEW row without touching the list cache
  // — any file_updated under an apps/ path refreshes the registry view. A
  // folder app never writes one file: its deploy frame does the same.
  useEffect(() => onFileUpdate((u) => {
    if (u.agent_slug === agent && /(^|\/)apps\/[^/]+\.html$/.test(u.rel_path)) {
      qc.invalidateQueries({ queryKey: ['apps', agent] })
    }
  }), [agent, qc])
  useEffect(() => onAppDeployed(() => {
    qc.invalidateQueries({ queryKey: ['apps', agent] })
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

  const applyOrder = (ids: string[]) => {
    // Optimistic tab order; the server renumbers within each scope group and
    // the invalidate reconciles (viewers moving shared rows get a 403 toast
    // state via the mutation error — the refetch restores truth). Placed
    // rows keep their place at the end and are never sent: their order is
    // the home agent's.
    qc.setQueryData(['apps', agent], (prev: PinnedApp[] | undefined) => {
      if (!prev) return prev
      const by = new Map(prev.map((a) => [a.id, a]))
      const ordered = ids.map((id) => by.get(id)).filter(Boolean) as PinnedApp[]
      return [...ordered, ...prev.filter((a) => a.placement && !ids.includes(a.id))]
    })
    reorder.mutate(ids)
  }
  const isPlaced = (id: string) => !!list.find((a) => a.id === id)?.placement

  const move = (id: string, delta: number) => {
    if (isPlaced(id)) return
    const ids = list.filter((a) => !a.placement).map((a) => a.id)
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
    if (!dragId || dragId === targetId || isPlaced(dragId) || isPlaced(targetId)) return
    const ids = list.filter((a) => !a.placement).map((a) => a.id)
    const from = ids.indexOf(dragId)
    const to = ids.indexOf(targetId)
    if (from < 0 || to < 0) return
    const next = [...ids]
    next.splice(from, 1)
    next.splice(to, 0, dragId)
    applyOrder(next)
  }

  const startLongPress = (id: string, e: React.TouchEvent) => {
    if (isPlaced(id)) return
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

  // Menu actions on a row. Hide-for-me is reversible from the "+N hidden"
  // chip, so it acts at once; unpin goes through the confirm block.
  const hideRow = (a: PinnedApp) => {
    setConfirmUnpinId(null)
    setActiveId(null)
    if (a.placement) hideShare.mutate({ id: a.placement.share_id, agent })
    else hideForMe.mutate(a.id)
  }
  const armRemove = (a: PinnedApp) => {
    if (!a.placement) return
    setWhereFromId(null)
    setConfirmRemove({ appId: a.id, shareId: a.placement.share_id, kind: a.placement.kind })
  }
  const restoreRow = (a: PinnedApp) => {
    if (a.placement) unhideShare.mutate({ id: a.placement.share_id, agent })
    else unhide.mutate(a.id)
  }
  const menuFor = (a: PinnedApp, onChip: boolean) => (
    <AppMenu
      app={a}
      onChip={onChip}
      onHideForMe={a.scope === 'shared' || a.granted || a.placement ? () => hideRow(a) : undefined}
      onWhereFrom={a.placement ? () => { setConfirmRemove(null); setWhereFromId(a.id) } : undefined}
      onRemoveFromAgent={a.placement?.can_remove ? () => armRemove(a) : undefined}
      onRemoveForMe={a.placement?.kind === GRANTEE_KIND.PERSON ? () => armRemove(a) : undefined}
      onUnpin={a.can_manage ? () => setConfirmUnpinId(a.id) : undefined}
      onShare={a.can_manage ? () => setShareId(a.id) : undefined}
      onRollback={a.can_manage ? () => { setConfirmUnpinId(null); setConfirmRollbackId(a.id) } : undefined}
      onLogs={appKind(a).mayServe && a.can_manage ? () => setLogsId(a.id) : undefined}
      onSettings={appKind(a).hasSettings && a.can_manage ? () => setSettingsId(a.id) : undefined}
      onTogglePreview={appKind(a).hasPreviewBuild && a.can_manage ? () => togglePreview(a.id) : undefined}
      previewing={previewIds.has(a.id)}
      onDelete={appKind(a).deletable && a.can_manage ? () => { setDeleteTyped(''); setConfirmDeleteId(a.id) } : undefined}
    />
  )
  const shareTarget = shareId ? (apps ?? []).find((a) => a.id === shareId) ?? null : null

  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center bg-p-bg pt-16 text-sm text-p-text-light">
        Loading apps…
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
          {hiddenList.length ? 'All shared apps are hidden' : 'No apps pinned yet'}
        </p>
        {hiddenList.length ? (
          <div className="flex flex-wrap items-center justify-center gap-1.5">
            {hiddenList.map((a) => (
              <button
                key={a.id}
                onClick={() => restoreRow(a)}
                className="rounded-full border border-dashed border-p-border px-3 py-1 text-xs text-p-text-secondary transition-colors hover:bg-p-surface-hover"
                title="Restore to your strip"
              >
                {a.title || a.slug} ↺
              </button>
            ))}
          </div>
        ) : (
          <p className="max-w-sm text-xs text-p-text-light">
            Ask the agent to pin one — e.g. “pin a morning-brief dashboard as an
            app” — and it appears here for every visit, refreshed by tasks.
          </p>
        )}
      </div>
    )
  }

  const needsApproval = appNeedsApproval(active)
  const confirmTarget = confirmUnpinId && active?.id === confirmUnpinId ? active : null
  const rollbackTarget = confirmRollbackId && active?.id === confirmRollbackId ? active : null
  const stripVisible = list.length > 1 || hiddenList.length > 0

  return (
    <div className={`flex flex-1 min-h-0 flex-col bg-p-bg ${topPadding ? 'pt-14' : 'pt-1'}`}>
      {/* Chip strip (ScopeChips design; scope colors match the workspace).
          Hidden with a SINGLE visible app — personal OR shared alike
          (operator call, 2026-08-15): the common shape is one agent
          dashboard, and the front-page auto-open shows it clean, no tab
          chrome. The menu moves to the frame's corner then. The strip
          returns the moment a second app is pinned — and stays whenever
          hidden apps exist: the "+N hidden" restore chips have no other
          home. */}
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
            const placed = !!a.placement
            const chipClass = appChipClass(a, isActive)
            return (
              <div key={a.id} className="flex shrink-0 snap-start items-center">
                {armed && (
                  <button
                    onClick={() => move(a.id, -1)}
                    className="px-1 text-p-text-secondary hover:text-p-text"
                    aria-label="Move left"
                  >‹</button>
                )}
                {/* div+role, not <button>: the active chip nests the menu
                    button (interactive elements can't nest). */}
                <div
                  ref={isActive ? activeChipRef : undefined}
                  role="button"
                  tabIndex={0}
                  draggable={IS_DESKTOP && !placed}
                  onDragStart={() => { dragIdRef.current = a.id }}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={() => onDrop(a.id)}
                  onTouchStart={(e) => startLongPress(a.id, e)}
                  onTouchMove={(e) => cancelLongPress(e)}
                  onTouchEnd={() => cancelLongPress()}
                  onClick={() => selectTab(a.id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      selectTab(a.id)
                    }
                  }}
                  title={placed ? `${a.title || a.slug} · from ${a.placement?.from_agent_name || a.placement?.from_agent}` : a.granted ? `${a.title || a.slug} (shared with you)` : purple ? `${a.title || a.slug} (shared)` : `${a.title || a.slug} (personal)`}
                  className={`flex cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1 text-xs font-medium transition-colors ${chipClass}`}
                >
                  {cameByShare(a) && (
                    <svg className="h-3 w-3 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true" data-testid="placed-mark">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M4 12v7a1 1 0 001 1h14a1 1 0 001-1v-7M12 4v12m0 0l-4-4m4 4l4-4" />
                    </svg>
                  )}
                  {a.title || a.slug}
                  {pending && (
                    <span className={`h-1.5 w-1.5 rounded-full ${isActive ? 'bg-white' : 'bg-amber-500'}`}
                          title="Actions pending approval" />
                  )}
                  {isActive && menuFor(a, true)}
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
                  onClick={() => { restoreRow(a); setShowHidden(hiddenList.length > 1) }}
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

      {/* Unpin confirmation — always names its target; reached from the
          menu's Unpin (Stop app for a server app), and a tab switch cancels
          the pending confirm. The team-wide soft-unpin is editor+
          (can_manage); personal rows keep the single unpin. */}
      {confirmTarget && (
        <UnpinConfirm
          app={confirmTarget}
          onConfirm={() => {
            setConfirmUnpinId(null)
            setActiveId(null)
            unpin.mutate(confirmTarget.id)
          }}
          onCancel={() => setConfirmUnpinId(null)}
        />
      )}

      {/* A placed row: where it comes from, and the remove confirm (the
          share's revoke, never the origin's unpin; on a person's own
          placement the revoke of their own share, which they cannot undo:
          only a new share brings the app back). */}
      {active?.placement && whereFromId === active.id && (
        <div className="mx-3 mt-2 flex items-start gap-2 rounded-xl border border-p-accent-teal/40 bg-p-accent-teal/5 px-3 py-2.5 text-xs" data-testid="app-where-from" role="status">
          <span className="flex-1 text-p-text">
            “{active.title || active.slug}” comes from {active.placement.from_agent_name || active.placement.from_agent}
            {active.placement.shared_by_name ? `, shared by ${active.placement.shared_by_name}` : ''}
            {active.placement.kind === GRANTEE_KIND.DEPARTMENT ? ' with your department' : active.placement.kind === GRANTEE_KIND.AGENT ? ' with this agent' : ' with you'}.
            {' '}You use it as {roleLabel(active.viewer_role).toLowerCase()}.
            {placedCallsNote(active)}
          </span>
          <button onClick={() => setWhereFromId(null)} aria-label="Dismiss"
            className="shrink-0 rounded-full px-1.5 text-p-text-light transition-colors hover:bg-p-surface-hover hover:text-p-text">×</button>
        </div>
      )}
      {active?.placement && confirmRemove?.appId === active.id
        && confirmRemove.shareId === active.placement.share_id && confirmRemove.kind === active.placement.kind && (
        <div className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-p-border-light bg-p-surface px-3 py-2.5 text-xs" data-testid="app-remove-confirm">
          <span className="text-p-text">
            {active.placement.kind === GRANTEE_KIND.PERSON
              ? <>Remove “{active.title || active.slug}” from your apps? {active.placement.shared_by_name || 'The person who shared it'} can share it again.</>
              : active.placement.kind === GRANTEE_KIND.DEPARTMENT
              ? <>Remove “{active.title || active.slug}” from every agent of the department? The share ends for all of them. The app stays with {active.placement.from_agent_name || active.placement.from_agent}.</>
              : <>Remove “{active.title || active.slug}” from this agent? Everyone here loses it. The app stays with {active.placement.from_agent_name || active.placement.from_agent}.</>}
          </span>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <button
              onClick={() => {
                const id = confirmRemove.shareId
                setConfirmRemove(null)
                setActiveId(null)
                patchShare.mutate({ id, revoke: true })
              }}
              className="rounded-md bg-red-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-red-700"
            >
              {active.placement.kind === GRANTEE_KIND.PERSON ? 'Remove for me' : active.placement.kind === GRANTEE_KIND.DEPARTMENT ? 'Remove from the department' : 'Remove from this agent'}
            </button>
            <button
              onClick={() => setConfirmRemove(null)}
              className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {rollbackTarget && (
        <RollbackConfirm
          app={rollbackTarget}
          busy={rollback.isPending}
          onConfirm={() => runRollback(rollbackTarget)}
          onCancel={() => setConfirmRollbackId(null)}
        />
      )}
      {active && rollbackNotice?.appId === active.id && (
        <RollbackNotice notice={rollbackNotice} onClose={() => setRollbackNotice(null)} />
      )}

      {active && confirmDeleteId === active.id && (
        <div className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-red-500/40 bg-red-500/5 px-3 py-2.5 text-xs" data-testid="app-delete-confirm">
          <span className="text-p-text">
            Delete “{active.title || active.slug}” with its data? The app, its releases and its
            database are removed for good. The folder goes to the recover bin. Type <code className="font-mono">{active.slug}</code> to confirm.
          </span>
          <input
            value={deleteTyped}
            onChange={(e) => setDeleteTyped(e.target.value)}
            placeholder={active.slug}
            aria-label="Type the app's slug to confirm"
            className="w-36 rounded-md border border-p-border-light bg-p-bg px-2 py-1 font-mono pointer-coarse:text-base text-p-text"
          />
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <button
              disabled={deleteTyped.trim().toLowerCase() !== active.slug || purge.isPending}
              onClick={() => { const id = active.id; setConfirmDeleteId(null); setActiveId(null); purge.mutate({ appId: id, confirm: deleteTyped.trim() }) }}
              className="rounded-md bg-red-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-red-700 disabled:opacity-50"
            >
              Delete app and its data
            </button>
            <button
              onClick={() => setConfirmDeleteId(null)}
              className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Declared-actions approval card (shared with the Dock); a folder
          app's pending release takes the slot instead. */}
      {active?.deploy_state === DEPLOY_STATE.PENDING && (
        <AppDeployCard key={`deploy-${active.id}`} app={active} agent={active.agent ?? agent} />
      )}
      {needsApproval && active && (
        <AppApprovalCard key={active.id} app={active} agent={active.agent ?? agent} />
      )}
      {active && logsId === active.id && (
        <AppLogsPanel key={`logs-${active.id}`} app={active} onClose={() => setLogsId(null)} />
      )}
      {active && settingsId === active.id && (
        <AppSettingsPanel key={`settings-${active.id}`} app={active} agent={active.agent ?? agent} onClose={() => setSettingsId(null)} />
      )}

      {/* The app itself. `isolate` caps the internal z-layers (the corner
          menu's button over the openurl chip) inside this subtree — without
          it that z ties with the absolute TopBar's z-20 and DOM order paints
          it OVER the notifications panel (whose z-50 lives INSIDE the
          TopBar's context). The menu's PANEL is portaled to the body and
          closes on any press outside it, so it never shares a moment with
          the notifications panel. */}
      <div className="relative isolate flex-1 min-h-0 p-2">
        {/* Keyed by the app: a tab switch mounts a fresh frame. A reused
            instance kept the first tab's closures (its message handler is
            registered once), so a folder app opened after a file app never
            got its viewer token (found live on the internal install). */}
        {active && (
          <AppFrame
            key={active.id}
            app={active}
            agent={active.agent ?? agent}
            onSendPrompt={active.placement ? undefined : onSendPrompt}
            preview={previewIds.has(active.id)}
          />
        )}
        {/* Solo app: no strip, so the menu sits in the frame's top-right
            corner (small, translucent; z-20 clears the frame's own overlays
            — the openurl chip is z-10). */}
        {!stripVisible && active && (
          <div className="absolute top-3.5 right-3.5 z-20">
            {menuFor(active, false)}
          </div>
        )}
      </div>
      {shareTarget && <SharePopover app={shareTarget} onClose={() => setShareId(null)} />}
    </div>
  )
}
