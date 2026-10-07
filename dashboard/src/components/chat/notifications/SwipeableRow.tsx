import { useEffect, useRef, useState, useCallback } from 'react'

/** A row that swipes left to dismiss (touch): the notifications panel's
 * deliveries and the "Shared with you" section's received items. A
 * `resetKey` that turns to a new non-empty value brings a dismissed row
 * back (the dismiss failed and the row is still listed). */
export default function SwipeableRow({ onSwipeDismiss, resetKey, children }: {
  onSwipeDismiss: () => void
  resetKey?: string
  children: React.ReactNode
}) {
  const rowRef = useRef<HTMLDivElement>(null)
  const [offsetX, setOffsetX] = useState(0)
  const [swiping, setSwiping] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const startX = useRef(0)
  const startY = useRef(0)
  const locked = useRef(false)  // true = horizontal swipe confirmed

  useEffect(() => {
    if (!resetKey) return
    setDismissed(false)
    setOffsetX(0)
  }, [resetKey])

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    startX.current = e.touches[0].clientX
    startY.current = e.touches[0].clientY
    locked.current = false
    setSwiping(false)
  }, [])

  const handleTouchMove = useCallback((e: React.TouchEvent) => {
    const dx = e.touches[0].clientX - startX.current
    const dy = e.touches[0].clientY - startY.current

    // Lock direction after 10px of movement
    if (!locked.current && !swiping) {
      if (Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        locked.current = true
        setSwiping(true)
      } else if (Math.abs(dy) > 10) {
        return  // Vertical scroll, don't interfere
      } else {
        return  // Not enough movement yet
      }
    }
    if (!locked.current) return

    // Only allow left swipe (negative dx)
    const clampedX = Math.min(0, dx)
    setOffsetX(clampedX)
  }, [swiping])

  const handleTouchEnd = useCallback(() => {
    if (!locked.current) {
      setOffsetX(0)
      setSwiping(false)
      return
    }
    const width = rowRef.current?.offsetWidth || 300
    if (Math.abs(offsetX) > width * 0.3) {
      // Swiped past threshold — animate out and dismiss
      setDismissed(true)
      setTimeout(onSwipeDismiss, 200)
    } else {
      // Spring back
      setOffsetX(0)
    }
    setSwiping(false)
    locked.current = false
  }, [offsetX, onSwipeDismiss])

  return (
    <div ref={rowRef} className="relative overflow-hidden" style={{ maxHeight: dismissed ? 0 : undefined, transition: dismissed ? 'max-height 200ms ease-out' : undefined }}>
      {/* Red background revealed on swipe */}
      <div className="absolute inset-0 bg-p-accent-red flex items-center justify-end pr-4">
        <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
        </svg>
      </div>
      {/* Foreground content */}
      <div
        className="relative bg-white dark:bg-p-surface"
        style={{
          transform: dismissed ? 'translateX(-100%)' : `translateX(${offsetX}px)`,
          transition: swiping ? 'none' : 'transform 200ms ease-out',
        }}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
      >
        {children}
      </div>
    </div>
  )
}
