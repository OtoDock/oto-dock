import { useEffect, useRef } from 'react'
import { pushEscHandler } from '../../lib/escStack'

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
 * and the 3-dot tile button all open this. */
export default function FileContextMenu({ x, y, actions, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => pushEscHandler(onClose), [onClose])
  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', onMouseDown)
    return () => document.removeEventListener('mousedown', onMouseDown)
  }, [onClose])

  // Clamp inside viewport.
  const maxX = typeof window !== 'undefined' ? window.innerWidth - 200 : x
  const maxY = typeof window !== 'undefined' ? window.innerHeight - 220 : y
  const clampedX = Math.min(x, maxX)
  const clampedY = Math.min(y, maxY)

  return (
    <div
      ref={ref}
      style={{ position: 'fixed', top: clampedY, left: clampedX, zIndex: 60 }}
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
