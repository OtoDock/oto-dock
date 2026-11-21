/**
 * The design system's collapsed card (components/ui/CollapsedCard): one
 * button holds the header and the preview, names the body it opens, and
 * leaves the aside outside it; the body is hidden and empty while closed.
 * `keepInView` scrolls the card into view one frame after it opens and while
 * its body grows, until the person scrolls.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'

import CollapsedCard from '@/components/ui/CollapsedCard'

function Card({ initial = false, keepInView = false }: { initial?: boolean; keepInView?: boolean }) {
  const [open, setOpen] = useState(initial)
  return (
    <CollapsedCard
      open={open}
      onToggle={() => setOpen((v) => !v)}
      header={<span>Header</span>}
      preview={<span>Preview</span>}
      aside={<a href="/x">A link</a>}
      keepInView={keepInView}
      rootProps={{ 'data-testid': 'card' }}
    >
      <p>The body</p>
    </CollapsedCard>
  )
}

describe('CollapsedCard', () => {
  const original = HTMLElement.prototype.scrollIntoView
  let scroll: ReturnType<typeof vi.fn<(arg?: boolean | ScrollIntoViewOptions) => void>>
  beforeEach(() => {
    scroll = vi.fn<(arg?: boolean | ScrollIntoViewOptions) => void>()
    HTMLElement.prototype.scrollIntoView = scroll
  })
  afterEach(() => {
    HTMLElement.prototype.scrollIntoView = original
  })

  it('is one button that names its body, the aside outside it', () => {
    render(<Card />)
    const button = screen.getByRole('button', { name: /Header/ })
    expect(button).toHaveAttribute('aria-expanded', 'false')
    const body = document.getElementById(button.getAttribute('aria-controls') ?? '')!
    expect(body).not.toBeNull()
    expect(body).not.toBeVisible()
    expect(body).toBeEmptyDOMElement()
    expect(button).toHaveTextContent('Preview')
    expect(screen.getByRole('link', { name: 'A link' }).closest('button')).toBeNull()
    expect(screen.getByTestId('card')).toContainElement(body)
  })

  it('opens under the button, drops the preview and closes again', () => {
    render(<Card />)
    const button = screen.getByRole('button', { name: /Header/ })
    fireEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    const body = document.getElementById(button.getAttribute('aria-controls') ?? '')!
    expect(body).toBeVisible()
    expect(body).toHaveTextContent('The body')
    expect(button).not.toHaveTextContent('Preview')
    fireEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(body).toBeEmptyDOMElement()
  })

  it('scrolls itself into view one frame after it opens, with keepInView', () => {
    vi.useFakeTimers()
    try {
      render(<Card keepInView />)
      const button = screen.getByRole('button', { name: /Header/ })
      expect(scroll).not.toHaveBeenCalled()
      fireEvent.click(button)
      expect(scroll).not.toHaveBeenCalled()
      vi.advanceTimersByTime(50)
      expect(scroll).toHaveBeenCalledTimes(1)
      expect(scroll.mock.contexts[0]).toBe(screen.getByTestId('card'))
      expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
      // Closing scrolls nothing.
      fireEvent.click(button)
      vi.advanceTimersByTime(50)
      expect(scroll).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps an opened card in view while its body grows, for a moment only', () => {
    vi.useFakeTimers()
    const observers: Array<() => void> = []
    vi.stubGlobal('ResizeObserver', class {
      constructor(private cb: () => void) { observers.push(() => this.cb()) }
      observe() { /* the test fires it */ }
      disconnect() { observers.length = 0 }
    })
    try {
      render(<Card keepInView initial />)
      vi.advanceTimersByTime(50)
      expect(scroll).toHaveBeenCalledTimes(1)
      // The body filled in: the card grew.
      observers.forEach((fire) => fire())
      expect(scroll).toHaveBeenCalledTimes(2)
      // Later growth is the person's business.
      vi.advanceTimersByTime(2000)
      observers.forEach((fire) => fire())
      expect(scroll).toHaveBeenCalledTimes(2)
    } finally {
      vi.unstubAllGlobals()
      vi.useRealTimers()
    }
  })

  it.each([
    ['a wheel', () => fireEvent.wheel(window)],
    ['a finger or the scroll bar', () => fireEvent.pointerDown(window)],
    ['a key', () => fireEvent.keyDown(window, { key: 'ArrowDown' })],
  ])('stops following the card once the person scrolls with %s', (_label, scrollByHand) => {
    vi.useFakeTimers()
    const observers: Array<() => void> = []
    vi.stubGlobal('ResizeObserver', class {
      constructor(private cb: () => void) { observers.push(() => this.cb()) }
      observe() { /* the test fires it */ }
      disconnect() { observers.length = 0 }
    })
    try {
      render(<Card keepInView initial />)
      vi.advanceTimersByTime(50)
      expect(scroll).toHaveBeenCalledTimes(1)
      scrollByHand()
      // The details fill in after the person took over: no pull back.
      observers.forEach((fire) => fire())
      expect(scroll).toHaveBeenCalledTimes(1)
    } finally {
      vi.unstubAllGlobals()
      vi.useRealTimers()
    }
  })

  it('scrolls once for a card mounted open, never for one mounted closed or without keepInView', () => {
    vi.useFakeTimers()
    try {
      const { unmount } = render(<Card keepInView initial />)
      vi.advanceTimersByTime(50)
      expect(scroll).toHaveBeenCalledTimes(1)
      unmount()
      render(<Card keepInView />)
      render(<Card initial />)
      vi.advanceTimersByTime(50)
      expect(scroll).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
