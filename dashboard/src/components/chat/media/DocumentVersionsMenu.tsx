import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import type { DocumentVersion } from '@/api/documents'
import { pushEscHandler } from '@/lib/escStack'
import { anchorBoxOf, usePopoverPlacement, type AnchorBox } from '@/components/ui/popoverPosition'
import { pushTime } from './documentTabs'

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
 * time, the turn). Portaled (the pane clips), a radio menu by keyboard
 * (arrows, Home, End, Enter, Space; Tab and Esc close it and give the focus
 * back to the button), closed by a press outside, a resize, a scroll or the
 * window losing focus (a press inside the editor).
 */
export default function DocumentVersionsMenu({ versions, current, failed, livePermissions, onPick }: Props) {
  const [anchor, setAnchor] = useState<AnchorBox | null>(null)
  const open = anchor !== null
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const placement = usePopoverPlacement(anchor, panelRef, { align: 'right' })

  const close = (refocus = true) => {
    setAnchor(null)
    if (refocus) buttonRef.current?.focus()
  }
  const toggle = () => {
    if (anchor) { close(); return }
    const rect = buttonRef.current?.getBoundingClientRect()
    if (rect) setAnchor(anchorBoxOf(rect))
  }

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!panelRef.current?.contains(t) && !buttonRef.current?.contains(t)) close(false)
    }
    const onMove = () => close(false)
    // Only a scroll that moves the button: not the menu's own list, not the
    // chat streaming beside the pane.
    const onScroll = (e: Event) => {
      const btn = buttonRef.current
      if (e.target instanceof Node && btn && !e.target.contains(btn)) return
      close(false)
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('resize', onMove)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('blur', onMove)
    const popEsc = pushEscHandler(() => close())
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('resize', onMove)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('blur', onMove)
      popEsc()
    }
  }, [open])

  // Focus the checked item when the menu opens.
  useEffect(() => {
    if (!open || !placement) return
    const items = panelRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]:not([disabled])')
    const checked = panelRef.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')
    ;(checked ?? items?.[0])?.focus()
  }, [open, placement])

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(panelRef.current?.querySelectorAll<HTMLButtonElement>(
      '[role="menuitemradio"]:not([disabled])') ?? [])
    const at = items.indexOf(document.activeElement as HTMLButtonElement)
    const move = (i: number) => { e.preventDefault(); items[(i + items.length) % items.length]?.focus() }
    if (e.key === 'ArrowDown') move(at + 1)
    else if (e.key === 'ArrowUp') move(at - 1)
    else if (e.key === 'Home') move(0)
    else if (e.key === 'End') move(items.length - 1)
    else if (e.key === 'Tab') { e.preventDefault(); close() }
  }

  const pick = (snapshotId: string | null) => {
    close()
    onPick(snapshotId)
  }

  const item = 'flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-p-text transition-colors hover:bg-p-surface-hover focus:bg-p-surface-hover focus:outline-none disabled:cursor-not-allowed disabled:text-p-text-light disabled:hover:bg-transparent'
  const mark = (on: boolean) => (
    <span className={`w-3 shrink-0 text-brand ${on ? '' : 'invisible'}`} aria-hidden="true">✓</span>
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
        className={`p-1.5 rounded-sm transition-colors hover:bg-p-surface ${current ? 'text-amber-600 dark:text-amber-400' : 'text-p-text-secondary'}`}
      >
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      </button>
      {anchor && createPortal(
        <div
          ref={panelRef}
          role="menu"
          aria-label="Versions"
          onKeyDown={onKeyDown}
          style={{
            position: 'fixed', zIndex: 60, top: placement?.top ?? 0, left: placement?.left ?? 0,
            visibility: placement ? 'visible' : 'hidden',
          }}
          className="w-60 max-h-80 overflow-y-auto rounded-lg border border-p-border-light bg-p-surface py-1 shadow-lg"
        >
          <button type="button" role="menuitemradio" aria-checked={!current} className={item} onClick={() => pick(null)}>
            {mark(!current)}
            <span className="font-medium">Live</span>
            {livePermissions && (
              <span className="ml-auto text-[10px] text-p-text-light">
                {livePermissions === 'edit' ? 'editable' : 'view only'}
              </span>
            )}
          </button>
          {failed && <p className="px-3 py-1.5 text-[11px] text-p-text-light">The versions could not be loaded.</p>}
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
              <span className="ml-auto text-[10px] text-p-text-light">
                {v.available ? `${pushTime(v.generation)} · turn ${v.turn}` : 'no longer available'}
              </span>
            </button>
          ))}
        </div>,
        document.body,
      )}
    </>
  )
}
