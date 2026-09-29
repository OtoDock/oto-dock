import { useLayoutEffect, useState, type RefObject } from 'react'

/**
 * Where a fixed-position popover goes so that it stays on screen.
 *
 * One rule for every portaled menu (the app tab menu, the workspace New File
 * menu, the file context menu): align the panel to its anchor, clamp both
 * axes to the viewport with a margin, and flip above the anchor when the
 * panel would cross the bottom edge. The chip strip lives at the top of the
 * screen, so in practice the phone bug this fixes ("ll screen" instead of
 * "Full screen" on a chip at the left edge) is the horizontal clamp; the
 * flip covers menus opened near the bottom (a context menu on the last
 * file tile). `ui/TitleTooltip.tsx` clamps its bubble the same way.
 */

export interface AnchorBox {
  top: number
  bottom: number
  left: number
  right: number
}

export interface PanelSize {
  width: number
  height: number
}

export interface Viewport {
  width: number
  height: number
}

export interface Placement {
  top: number
  left: number
  /** The panel sits above the anchor because it did not fit below. */
  flipped: boolean
}

export interface ClampOptions {
  /** Distance kept from every viewport edge (px). */
  margin?: number
  /** Gap between the anchor and the panel (px). */
  gap?: number
  /** Which anchor edge the panel's matching edge lines up with. */
  align?: 'left' | 'right'
}

/** Pure: the placement for a panel of `panel` size next to `anchor` inside
 * `viewport`. A panel wider than the viewport pins to the left margin; one
 * taller than both the space below and above pins to the top margin. */
export function clampPopover(
  anchor: AnchorBox, panel: PanelSize, viewport: Viewport, opts: ClampOptions = {},
): Placement {
  const margin = opts.margin ?? 8
  const gap = opts.gap ?? 4
  const align = opts.align ?? 'right'

  let left = align === 'right' ? anchor.right - panel.width : anchor.left
  const maxLeft = viewport.width - panel.width - margin
  left = Math.max(margin, Math.min(left, maxLeft))

  const below = anchor.bottom + gap
  const fitsBelow = below + panel.height <= viewport.height - margin
  const above = anchor.top - gap - panel.height
  const fitsAbove = above >= margin
  if (fitsBelow || !fitsAbove) {
    const maxTop = viewport.height - panel.height - margin
    return { top: Math.max(margin, Math.min(below, maxTop)), left, flipped: false }
  }
  return { top: above, left, flipped: true }
}

/**
 * Measure the portaled panel after it mounts and place it. Returns `null`
 * until the panel has been measured — render the panel with
 * `visibility: hidden` meanwhile so the first paint never shows it in the
 * wrong place. Re-runs when the anchor changes (a re-open).
 */
export function usePopoverPlacement(
  anchor: AnchorBox | null,
  panelRef: RefObject<HTMLElement | null>,
  opts: ClampOptions = {},
): Placement | null {
  const [placement, setPlacement] = useState<Placement | null>(null)
  const { margin, gap, align } = opts
  useLayoutEffect(() => {
    if (!anchor) { setPlacement(null); return }
    const el = panelRef.current
    if (!el) return
    setPlacement(clampPopover(
      anchor,
      { width: el.offsetWidth, height: el.offsetHeight },
      { width: window.innerWidth, height: window.innerHeight },
      { margin, gap, align },
    ))
  }, [anchor, panelRef, margin, gap, align])
  return placement
}

/** The four numbers a `DOMRect` contributes, as a plain object React can
 * compare by value. */
export function anchorBoxOf(rect: DOMRect): AnchorBox {
  return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right }
}
