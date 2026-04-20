import { useEffect } from 'react'
import { pushEscHandler } from '../../lib/escStack'

interface Props {
  open: boolean
  onClose: () => void
  children: React.ReactNode
  side?: 'left' | 'right'
  width?: string
  /** Pixel width for desktop transition (must match Tailwind width class) */
  widthPx?: number
}

export default function ResponsiveDrawer({
  open,
  onClose,
  children,
  side = 'left',
  width = 'w-56',
  widthPx = 224, // 14rem = w-56
}: Props) {
  // Close on Escape (precedence stack — only registered while open)
  useEffect(() => {
    if (!open) return
    return pushEscHandler(onClose)
  }, [open, onClose])

  const translateHidden = side === 'left' ? '-translate-x-full' : 'translate-x-full'

  return (
    <>
      {/* Mobile backdrop */}
      <div
        className={`fixed inset-0 bg-black/40 z-40 md:hidden transition-opacity duration-200
          ${open ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
        onClick={onClose}
      />

      {/* Mobile: slide-in overlay (80% width) */}
      <div
        className={`
          md:hidden fixed top-0 ${side === 'left' ? 'left-0' : 'right-0'} h-full z-50
          transition-transform duration-200 ease-in-out
          ${open ? 'translate-x-0' : translateHidden}
          w-[80vw]
        `}
      >
        {children}
      </div>

      {/* Desktop: animated width sidebar */}
      <div
        className="hidden md:block overflow-hidden transition-[width] duration-300 ease-in-out shrink-0"
        style={{ width: open ? widthPx : 0 }}
      >
        <div className={`${width} h-full`}>
          {children}
        </div>
      </div>
    </>
  )
}
