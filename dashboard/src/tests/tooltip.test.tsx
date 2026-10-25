import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

// ─── Tooltip onlyWhenClipped — the truncation-gated full-title bubble ───────
// Desktop: hover (300ms delay). Mobile: long-press (500ms, movement-
// cancelled, follow-up click swallowed). Bubble portals to <body> — the
// mobile drawer's translate makes any transformed ancestor the containing
// block for position:fixed, so an inline bubble would mis-position.

import Tooltip from '@/components/ui/Tooltip'

function renderTip(text = 'A very long chat title that clips') {
  const utils = render(
    <Tooltip text={text} onlyWhenClipped className="truncate">{text}</Tooltip>,
  )
  const label = screen.getByText(text)
  return { ...utils, label }
}

function setClipped(el: HTMLElement, clipped: boolean) {
  Object.defineProperty(el, 'scrollWidth', { configurable: true, value: clipped ? 300 : 100 })
  Object.defineProperty(el, 'clientWidth', { configurable: true, value: 100 })
}

afterEach(() => vi.useRealTimers())

describe('Tooltip onlyWhenClipped', () => {
  it('hover shows the bubble only when the label is actually clipped', () => {
    vi.useFakeTimers()
    const { label } = renderTip()
    setClipped(label, false)
    fireEvent.mouseEnter(label)
    act(() => { vi.advanceTimersByTime(400) })
    expect(screen.queryByRole('tooltip')).toBeNull()

    setClipped(label, true)
    fireEvent.mouseEnter(label)
    act(() => { vi.advanceTimersByTime(400) })
    const tip = screen.getByRole('tooltip')
    expect(tip.textContent).toBe('A very long chat title that clips')
    // Portaled to body (never inside the transformed drawer subtree).
    expect(tip.parentElement).toBe(document.body)
    fireEvent.mouseLeave(label)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('long-press opens it and the follow-up click is swallowed', () => {
    vi.useFakeTimers()
    const rowClick = vi.fn()
    const text = 'Long pressed title'
    render(
      <div onClick={rowClick}>
        <Tooltip text={text} onlyWhenClipped className="truncate">{text}</Tooltip>
      </div>,
    )
    const label = screen.getByText(text)
    setClipped(label, true)
    fireEvent.touchStart(label, { touches: [{ clientX: 10, clientY: 10 }] })
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    // The click browsers fire after a held release must not open the row.
    fireEvent.click(label)
    expect(rowClick).not.toHaveBeenCalled()
    // A later plain click passes through again.
    fireEvent.click(label)
    expect(rowClick).toHaveBeenCalledTimes(1)
  })

  it('movement cancels the long-press (scroll wins)', () => {
    vi.useFakeTimers()
    const { label } = renderTip('Scrolled away title')
    setClipped(label, true)
    fireEvent.touchStart(label, { touches: [{ clientX: 10, clientY: 10 }] })
    fireEvent.touchMove(label, { touches: [{ clientX: 10, clientY: 40 }] })
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('Tooltip on a focusable control', () => {
  it('describes the control by the bubble, unless its label already says it', () => {
    render(
      <>
        <Tooltip text="Remove the file"><button type="button" aria-label="Remove">x</button></Tooltip>
        <Tooltip text="Same words"><span role="img" tabIndex={0} aria-label="Same words">*</span></Tooltip>
      </>,
    )
    const remove = screen.getByRole('button', { name: 'Remove' })
    act(() => { remove.focus() })
    const tip = screen.getByRole('tooltip')
    expect(remove.getAttribute('aria-describedby')).toBe(tip.id)
    act(() => { remove.blur() })
    expect(remove.getAttribute('aria-describedby')).toBeNull()

    const same = screen.getByRole('img', { name: 'Same words' })
    act(() => { same.focus() })
    expect(screen.getByRole('tooltip').textContent).toBe('Same words')
    expect(same.getAttribute('aria-describedby')).toBeNull()
  })
})

