import { useCallback, useRef } from 'react'

/**
 * Touch long-press with the platform's established constants (FileTile /
 * FileTree twin: 500ms hold, cancelled by >8px movement so scrolling always
 * wins). Returns touch handlers to spread on the target plus a click-capture
 * guard: browsers fire a click after a held touch release, and without the
 * guard a long-press on a sidebar row would ALSO navigate to the chat.
 */
const LONG_PRESS_MS = 500
const MOVE_CANCEL_PX = 8

export function useLongPress(onLongPress: () => void) {
  const timerRef = useRef<number | null>(null)
  const startRef = useRef<{ x: number; y: number } | null>(null)
  const firedRef = useRef(false)

  const cancel = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    startRef.current = null
  }, [])

  const onTouchStart = useCallback((e: React.TouchEvent) => {
    const t = e.touches[0]
    if (!t) return
    firedRef.current = false
    startRef.current = { x: t.clientX, y: t.clientY }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      firedRef.current = true
      onLongPress()
    }, LONG_PRESS_MS)
  }, [onLongPress])

  const onTouchMove = useCallback((e: React.TouchEvent) => {
    if (!startRef.current) return
    const t = e.touches[0]
    if (!t) return
    const dx = Math.abs(t.clientX - startRef.current.x)
    const dy = Math.abs(t.clientY - startRef.current.y)
    if (dx > MOVE_CANCEL_PX || dy > MOVE_CANCEL_PX) cancel()
  }, [cancel])

  const onTouchEnd = useCallback(() => cancel(), [cancel])

  // Capture-phase: swallow the synthetic click that follows an activated
  // long-press before it reaches the row's onClick.
  const onClickCapture = useCallback((e: React.MouseEvent) => {
    if (firedRef.current) {
      firedRef.current = false
      e.preventDefault()
      e.stopPropagation()
    }
  }, [])

  // Android fires contextmenu on long-press; iOS shows the callout. Both
  // would fight the tooltip — the CSS side (`touch-callout: none`) rides on
  // the consumer's element.
  const onContextMenu = useCallback((e: React.MouseEvent) => {
    if (firedRef.current || timerRef.current !== null) e.preventDefault()
  }, [])

  return { onTouchStart, onTouchMove, onTouchEnd, onClickCapture, onContextMenu }
}
