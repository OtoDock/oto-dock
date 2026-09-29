import { describe, it, expect } from 'vitest'

import { clampPopover } from '@/components/ui/popoverPosition'

// One placement rule for every portaled menu: align to the anchor, clamp
// both axes to the viewport, flip above when the panel would cross the
// bottom. The phone bug this covers: the first app chip's menu, anchored by
// its right edge alone, opened off the left of a 390px screen.

const phone = { width: 390, height: 844 }
const panel = { width: 176, height: 200 }

describe('clampPopover', () => {
  it('right-aligns the panel to the anchor when everything fits', () => {
    const p = clampPopover({ top: 60, bottom: 84, left: 300, right: 330 }, panel, phone)
    expect(p).toEqual({ top: 88, left: 154, flipped: false })
  })

  it('keeps a panel on screen for a chip at the far left', () => {
    const p = clampPopover({ top: 60, bottom: 84, left: 12, right: 40 }, panel, phone)
    expect(p.left).toBe(8)
    expect(p.flipped).toBe(false)
  })

  it('keeps a panel on screen for a chip at the far right', () => {
    const p = clampPopover({ top: 60, bottom: 84, left: 360, right: 388 }, panel, phone)
    expect(p.left).toBe(phone.width - panel.width - 8)
  })

  it('left-aligns when asked, and still clamps at the right edge', () => {
    const p = clampPopover({ top: 60, bottom: 84, left: 300, right: 330 }, panel, phone, { align: 'left' })
    expect(p.left).toBe(phone.width - panel.width - 8)
    const q = clampPopover({ top: 60, bottom: 84, left: 20, right: 50 }, panel, phone, { align: 'left' })
    expect(q.left).toBe(20)
  })

  it('flips above the anchor when the panel would cross the bottom', () => {
    const p = clampPopover({ top: 780, bottom: 800, left: 100, right: 130 }, panel, phone)
    expect(p.flipped).toBe(true)
    expect(p.top).toBe(780 - 4 - panel.height)
  })

  it('pins to the top margin when the panel fits neither below nor above', () => {
    const tall = { width: 176, height: 900 }
    const p = clampPopover({ top: 400, bottom: 420, left: 100, right: 130 }, tall, phone)
    expect(p.flipped).toBe(false)
    expect(p.top).toBe(8)
  })

  it('pins a panel wider than the viewport to the left margin', () => {
    const wide = { width: 500, height: 100 }
    const p = clampPopover({ top: 60, bottom: 84, left: 300, right: 330 }, wide, phone)
    expect(p.left).toBe(8)
  })

  it('honours a zero gap for a point anchor (the context menu)', () => {
    const p = clampPopover({ top: 300, bottom: 300, left: 200, right: 200 }, panel, phone, { align: 'left', gap: 0 })
    expect(p).toEqual({ top: 300, left: 200, flipped: false })
  })
})
