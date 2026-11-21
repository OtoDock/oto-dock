import { createPortal } from 'react-dom'
import type { DocumentVersion } from '@/api/documents'
import { usePortalMenu } from '@/hooks/usePortalMenu'
import { pushTime } from './documentTabs'
import { POPOVER } from '../../ui/classes'

interface Props {
  /** The versions of the file on show, newest first. */
  versions: DocumentVersion[]
  /** The snapshot id on show; null while Live. */
  current: string | null
  /** The listing could not be read. */
  failed?: boolean
  /** What the live file's token allows; null while unknown. */
  livePermissions?: 'edit' | 'view' | null
  onPick: (snapshotId: string | null) => void
}

/**
 * The Versions button of the document pane and its menu: "Live" first, then
 * every version of the file in this chat, newest first ("Version N", its
 * time, the turn). A radio menu by keyboard (arrows, Home, End, Enter,
 * Space; Tab and Esc close it and give the focus back to the button), on
 * `usePortalMenu` with the pane's options menu: also closed by the window's
 * blur (a press in the editor), a resize or a scroll that moves the button.
 */
export default function DocumentVersionsMenu({ versions, current, failed, livePermissions, onPick }: Props) {
  const { open, buttonRef, panelRef, style, portalRoot, close, toggle, onKeyDown } = usePortalMenu({
    align: 'right', items: '[role="menuitemradio"]', focus: '[aria-checked="true"]:not([disabled])', closeOnMove: true,
  })

  const pick = (snapshotId: string | null) => {
    close()
    onPick(snapshotId)
  }

  const item = 'flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-primary transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover focus:outline-none disabled:cursor-not-allowed disabled:text-tertiary disabled:hover:bg-transparent'
  const mark = (on: boolean) => (
    <span className={`w-3 shrink-0 text-primary ${on ? '' : 'invisible'}`} aria-hidden="true">✓</span>
  )

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Versions"
        title="Versions"
        className={`p-1.5 rounded-sm transition-colors hover:bg-surface-muted ${current ? 'text-status-warning-fg' : 'text-secondary'}`}
      >
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      </button>
      {open && createPortal(
        <div
          ref={panelRef}
          role="menu"
          aria-label="Versions"
          onKeyDown={onKeyDown}
          style={style}
          className={`w-60 max-h-80 overflow-y-auto ${POPOVER} py-1`}
        >
          <button type="button" role="menuitemradio" aria-checked={!current} className={item} onClick={() => pick(null)}>
            {mark(!current)}
            <span className="font-medium">Live</span>
            {livePermissions && (
              <span className="ml-auto text-3xs text-tertiary">
                {livePermissions === 'edit' ? 'Editable' : 'View only'}
              </span>
            )}
          </button>
          {failed && <p className="px-3 py-1.5 text-2xs text-tertiary">The versions could not be loaded.</p>}
          {versions.map((v) => (
            <button
              key={`${v.message_id}-${v.snapshot_id}`}
              type="button"
              role="menuitemradio"
              aria-checked={!!current && current === v.snapshot_id}
              disabled={!v.available}
              className={item}
              onClick={() => pick(v.snapshot_id)}
              title={v.available ? undefined : 'This version is no longer available'}
            >
              {mark(!!current && current === v.snapshot_id)}
              <span>Version {v.version}</span>
              <span className="ml-auto text-3xs text-tertiary">
                {v.available ? `${pushTime(v.generation)} · turn ${v.turn}` : 'no longer available'}
              </span>
            </button>
          ))}
        </div>,
        portalRoot,
      )}
    </>
  )
}
