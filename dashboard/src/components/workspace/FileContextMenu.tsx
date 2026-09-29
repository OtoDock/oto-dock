import { useEffect, useMemo, useRef } from 'react'
import { pushEscHandler } from '../../lib/escStack'
import { usePopoverPlacement } from '../ui/popoverPosition'

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
      className="min-w-[170px] bg-white dark:bg-p-surface rounded-lg border border-p-border-light shadow-lg py-1"
    >
      {actions.map((a) => (
        <button
          key={a.key}
          onClick={() => {
            a.onClick()
            onClose()
          }}
          className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs ${
            a.tone === 'danger'
              ? 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20'
              : 'text-p-text hover:bg-p-surface-hover'
          }`}
        >
          {a.icon}
          <span>{a.label}</span>
        </button>
      ))}
    </div>
  )
}
