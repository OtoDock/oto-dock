import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { pushEscHandler } from '../../lib/escStack'
import { usePopoverPlacement } from '../ui/popoverPosition'
import { MENU_ITEM, POPOVER } from '../ui/classes'

// A destructive row: its own string, never MENU_ITEM plus a red text class.
const DANGER_ITEM =
  'flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-status-danger-fg ' +
  'transition-colors duration-fast hover:bg-status-danger-bg'

export interface MenuAction {
  key: string
  label: string
  /** Tailwind color class for the icon + label. */
  tone?: 'default' | 'danger'
  icon: React.ReactNode
  onClick: () => void
}

interface Props {
  x: number
  y: number
  actions: MenuAction[]
  onClose: () => void
}

/** Shared context menu rendered at a fixed (x, y) — right-click, long-press
 * and the 3-dot tile button all open this. The panel is measured after it
 * mounts and clamped to the viewport (`usePopoverPlacement`, the same rule
 * as the app tab menu): a menu opened near the right or bottom edge slides
 * in, and one opened near the bottom flips above the point. */
export default function FileContextMenu({ x, y, actions, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const anchor = useMemo(() => ({ top: y, bottom: y, left: x, right: x }), [x, y])
  const placement = usePopoverPlacement(anchor, ref, { align: 'left', gap: 0 })

  // Where the focus was when the menu opened (the tile's 3-dot button, the
  // field that held it): an item gives it back there before it acts, so a
  // dialog it opens (the delete confirm) returns the focus there too.
  const originRef = useRef<HTMLElement | null>(null)
  useLayoutEffect(() => {
    const a = document.activeElement
    originRef.current = a instanceof HTMLElement && a !== document.body ? a : null
  }, [])

  useEffect(() => pushEscHandler(onClose), [onClose])
  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', onMouseDown)
    return () => document.removeEventListener('mousedown', onMouseDown)
  }, [onClose])

  return (
    <div
      ref={ref}
      style={{
        position: 'fixed',
        top: placement?.top ?? y,
        left: placement?.left ?? x,
        visibility: placement ? 'visible' : 'hidden',
        zIndex: 60,
      }}
      className={`min-w-[170px] py-1 ${POPOVER}`}
    >
      {actions.map((a) => (
        <button
          key={a.key}
          onClick={() => {
            const origin = originRef.current
            if (origin?.isConnected) origin.focus({ preventScroll: true })
            a.onClick()
            onClose()
          }}
          className={a.tone === 'danger' ? DANGER_ITEM : MENU_ITEM}
        >
          {a.icon}
          <span>{a.label}</span>
        </button>
      ))}
    </div>
  )
}
