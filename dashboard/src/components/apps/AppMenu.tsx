import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import type { PinnedApp } from '../../api/apps'
import { GRANTEE_KIND } from '../../api/shares'
import { appKind } from '../../lib/kinds/app'
import { unpinWords } from './appRow'
import { anchorBoxOf, usePopoverPlacement, type AnchorBox } from '../ui/popoverPosition'
import { usePortalRoot } from '../ui/portalRoot'
import { MENU_ITEM, POPOVER } from '../ui/classes'
import { pushEscHandler } from '../../lib/escStack'

// A destructive row: its own string, never MENU_ITEM plus a red text class.
const DANGER_ITEM =
  'flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-status-danger-fg ' +
  'transition-colors duration-fast hover:bg-status-danger-bg'

/**
 * The app's three-dot menu — one component for the active chip, the solo
 * frame corner and the full-screen page: Open full screen, Share, Hide for
 * me (shared rows, any role), Unpin (the host's two-step confirm; editor+
 * on shared rows, owner on personal ones; "Stop app" for an app whose live
 * release runs a server, `unpinWords`). A row placed here by a share
 * adds Where it comes from and its removal: "Remove from this agent" for a
 * team placement, "Remove for me" (the revoke of the viewer's own share)
 * in place of Hide for me on a person's own placement.
 *
 * The panel is portaled to the body at fixed coordinates: the chip strip is
 * a horizontal scroller, and a scroller clips its overflow on BOTH axes.
 */

interface Props {
  app: PinnedApp
  /** Hide the full-screen entry on the page that IS the full screen. */
  fullScreen?: boolean
  onHideForMe?: () => void
  onUnpin?: () => void
  /** Opens the share popover; offered to whoever may manage the row. */
  onShare?: () => void
  /** Points the app back at its previous release; shown only when one exists. */
  onRollback?: () => void
  /** Folder apps (APPS.md): the server log, for whoever may manage the row. */
  onLogs?: () => void
  /** Folder apps (APPS.md "Secrets"): the settings panel — the secrets the
      app needs, set by a person — for whoever may manage the row. */
  onSettings?: () => void
  /** Folder apps: switch between the live release and the working copy the
      agent started as a preview (only while one exists). */
  onTogglePreview?: () => void
  previewing?: boolean
  /** Folder apps: delete the app with its data (the host asks for the slug). */
  onDelete?: () => void
  /** A placed row (SHARING.md): where it comes from (the host shows a
      notice), and "Remove from this agent" for whoever may (an editor or
      manager of the receiving agent for an agent share, an admin for a
      department share). */
  onWhereFrom?: () => void
  onRemoveFromAgent?: () => void
  /** A person's own placement: revoke their share (the host confirms
      first); offered in place of Hide for me when the host passes it. */
  onRemoveForMe?: () => void
  /** Chip placement: light glyph on the chip's own color. */
  onChip?: boolean
}

export default function AppMenu({
  app, fullScreen = false, onHideForMe, onUnpin, onShare, onRollback, onLogs, onSettings,
  onTogglePreview, previewing = false, onDelete, onWhereFrom, onRemoveFromAgent, onRemoveForMe,
  onChip = false,
}: Props) {
  // What the app's kind can do (lib/kinds/app.ts) decides which rows show.
  const kind = appKind(app)
  // The panel is portaled to the body at fixed coordinates instead of sitting
  // absolute under the button: on the chip strip the button lives inside an
  // `overflow-x: auto` scroller, which per CSS computes `overflow-y` to
  // `auto` too, so an absolute child is clipped to the strip's height (the
  // same reason WorkspaceToolbar portals its New File menu). The panel is
  // right-aligned to the button and clamped to the viewport on both axes
  // (`usePopoverPlacement`): anchoring by the right edge alone pushed it off
  // the left of a phone screen for the first chip (2026-09-16).
  const [anchor, setAnchor] = useState<AnchorBox | null>(null)
  const open = anchor !== null
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const portalRoot = usePortalRoot()
  const placement = usePopoverPlacement(anchor, panelRef)
  const navigate = useNavigate()
  const title = app.title || app.slug

  const close = () => setAnchor(null)
  // An item gives the focus back to the menu's button before it acts, so a
  // dialog it opens returns the focus there when it closes (the item is gone).
  const pick = (act: () => void) => () => {
    close()
    buttonRef.current?.focus()
    act()
  }
  const toggle = () => {
    if (anchor) { close(); return }
    const rect = buttonRef.current?.getBoundingClientRect()
    if (!rect) return
    setAnchor(anchorBoxOf(rect))
  }

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!panelRef.current?.contains(t) && !buttonRef.current?.contains(t)) close()
    }
    // Fixed coordinates go stale the moment anything under them moves.
    const onMove = () => close()
    document.addEventListener('mousedown', onDown)
    window.addEventListener('resize', onMove)
    window.addEventListener('scroll', onMove, true)
    const popEsc = pushEscHandler(() => { close(); buttonRef.current?.focus() }, () => panelRef.current)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('resize', onMove)
      window.removeEventListener('scroll', onMove, true)
      popEsc()
    }
  }, [open])

  // A person's own placement is removed (its share revoked) rather than parked.
  const removesForMe = app.placement?.kind === GRANTEE_KIND.PERSON && !!onRemoveForMe
  // Shared rows, rows shared WITH this viewer and rows placed here by a
  // share can be parked off their own list; a personal row of one's own has
  // the real unpin instead.
  const canHide = !removesForMe && (app.scope === 'shared' || !!app.granted || !!app.placement) && app.pin_scope !== 'chat' && app.pin_scope !== 'project' && !!onHideForMe
  const canUnpin = app.can_manage && !!onUnpin

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={(e) => { e.stopPropagation(); toggle() }}
        aria-label={`Options for ${title}`}
        aria-haspopup="menu"
        aria-expanded={open}
        title="App options"
        className={onChip
          ? '-mr-1 rounded-full p-0.5 transition-colors hover:bg-surface-hover'
          : 'flex h-6 w-6 items-center justify-center rounded-full bg-surface-card text-tertiary shadow-elevation-1 transition-colors hover:text-primary'}
      >
        <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" />
        </svg>
      </button>
      {anchor && createPortal(
        <div
          ref={panelRef}
          role="menu"
          onClick={(e) => e.stopPropagation()}
          style={{
            position: 'fixed',
            top: placement?.top ?? anchor.bottom,
            left: placement?.left ?? anchor.left,
            visibility: placement ? 'visible' : 'hidden',
            zIndex: 60,
            maxHeight: 'min(60vh, 320px)',
          }}
          className={`w-52 overflow-y-auto py-1 ${POPOVER}`}
        >
          {!fullScreen && (
            <button role="menuitem" className={MENU_ITEM} onClick={() => { close(); navigate(`/apps/${app.id}`) }}>
              Open full screen
            </button>
          )}
          {onShare && (
            <button role="menuitem" className={MENU_ITEM} onClick={pick(() => { onShare() })}>
              Share
            </button>
          )}
          {onRollback && app.has_previous_release && (
            <button role="menuitem" className={MENU_ITEM} onClick={pick(() => { onRollback() })} title={kind.keepsData ? 'Serve the previous release again, with the data as it was before this one' : 'Serve the previous release again'}>
              Roll back
            </button>
          )}
          {onTogglePreview && kind.hasPreviewBuild && app.can_manage && !!app.preview_sha && (
            <button role="menuitem" className={MENU_ITEM} onClick={pick(() => { onTogglePreview() })} title="The working copy the agent started as a preview">
              {previewing ? 'View the live release' : 'View the working copy'}
            </button>
          )}
          {onLogs && kind.mayServe && app.can_manage && (
            <button role="menuitem" className={MENU_ITEM} onClick={pick(() => { onLogs() })}>
              Logs
            </button>
          )}
          {onSettings && kind.hasSettings && app.can_manage && (
            <button role="menuitem" className={MENU_ITEM} onClick={pick(() => { onSettings() })} title="The secrets the app needs, set by a person">
              Settings
            </button>
          )}
          {canHide && (
            <button role="menuitem" className={MENU_ITEM} onClick={pick(() => { onHideForMe?.() })}>
              Hide for me
            </button>
          )}
          {onWhereFrom && app.placement && (
            <button role="menuitem" className={MENU_ITEM} onClick={pick(() => { onWhereFrom() })}>
              Where it comes from
            </button>
          )}
          {onRemoveFromAgent && app.placement?.can_remove && (
            <button role="menuitem" className={DANGER_ITEM} onClick={pick(() => { onRemoveFromAgent() })}
              title="Everyone here loses it. The app stays with its own agent.">
              Remove from this agent
            </button>
          )}
          {removesForMe && (
            <button role="menuitem" className={DANGER_ITEM} onClick={pick(() => { onRemoveForMe?.() })}
              title="Removes the share. The person who shared it can share it again.">
              Remove for me
            </button>
          )}
          {canUnpin && (
            <button role="menuitem" className={DANGER_ITEM} onClick={pick(() => { onUnpin?.() })}>
              {unpinWords(app).action}
            </button>
          )}
          {onDelete && kind.deletable && app.can_manage && (
            <button role="menuitem" className={DANGER_ITEM} onClick={pick(() => { onDelete() })} title="Removes the app, its releases and its database">
              Delete app and its data
            </button>
          )}
        </div>,
        portalRoot,
      )}
    </div>
  )
}
