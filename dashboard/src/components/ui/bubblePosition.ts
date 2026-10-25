import { useCallback, useState } from 'react'

/**
 * Where a text bubble goes: the Tooltip's one-line label and the InfoTip's
 * longer text share this one rule. Centred over the trigger, kept a margin
 * inside the viewport, above the trigger unless the bubble does not fit
 * there, then below it; one taller than both sides takes the roomier side,
 * clamped. The bubble is `position: fixed` in the portal root, so the
 * trigger's own box is all that is read (`popoverPosition.ts` aligns the
 * menus to an edge instead).
 */

const EDGE_MARGIN_PX = 8
const GAP_PX = 6

export interface BubbleAnchor {
  /** The trigger's horizontal centre. */
  x: number
  top: number
  bottom: number
}

export interface BubblePlacement {
  left: number
  top: number
  below: boolean
}

/** Pure: the placement of a bubble of `size` beside `anchor` in `viewport`. */
export function clampBubble(
  anchor: BubbleAnchor,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
): BubblePlacement {
  const maxLeft = viewport.width - size.width - EDGE_MARGIN_PX
  const left = Math.max(EDGE_MARGIN_PX, Math.min(anchor.x - size.width / 2, maxLeft))
  const maxTop = viewport.height - size.height - EDGE_MARGIN_PX
  const above = anchor.top - GAP_PX - size.height
  if (above >= EDGE_MARGIN_PX) return { left, top: Math.min(above, Math.max(EDGE_MARGIN_PX, maxTop)), below: false }
  const below = anchor.bottom + GAP_PX
  if (below <= maxTop || viewport.height - anchor.bottom > anchor.top) {
    return { left, top: Math.max(EDGE_MARGIN_PX, Math.min(below, maxTop)), below: true }
  }
  return { left, top: EDGE_MARGIN_PX, below: false }
}

/**
 * The anchor a bubble is shown at, and the callback ref that places the
 * bubble once it has a size. The bubble is measured in the commit, before
 * the browser paints, so it never shows in the wrong place.
 */
export function useBubbleAnchor() {
  const [anchor, setAnchor] = useState<BubbleAnchor | null>(null)

  const showAt = useCallback((el: Element) => {
    const r = el.getBoundingClientRect()
    setAnchor({ x: r.left + r.width / 2, top: r.top, bottom: r.bottom })
  }, [])

  const hide = useCallback(() => setAnchor(null), [])

  const place = useCallback((el: HTMLElement | null) => {
    if (!el || !anchor) return
    const p = clampBubble(
      anchor,
      { width: el.offsetWidth, height: el.offsetHeight },
      { width: window.innerWidth, height: window.innerHeight },
    )
    el.style.left = `${p.left}px`
    el.style.top = `${p.top}px`
  }, [anchor])

  return { anchor, showAt, hide, place }
}
