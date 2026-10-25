import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import Dialog from '../components/ui/Dialog'
import InfoTip from '../components/ui/InfoTip'
import { clampBubble } from '../components/ui/bubblePosition'
import { pressEscape } from './dialogsHarness'

// ─── The info button beside a setting's label ───────────────────────────────
// Hover (300 ms) and keyboard focus open it on a desktop; a click, a tap or
// Enter pins it, whatever opened it, and the next one closes it; Escape, an
// outside press, a scroll and a blur close it; the bubble portals into the
// dialog it sits in.

const TEXT = 'The longer description of the setting.'

afterEach(() => vi.useRealTimers())

function renderTip() {
  render(
    <div>
      <input aria-label="Name" />
      <span>Alerts</span>
      <InfoTip label="Alerts">{TEXT}</InfoTip>
      <button type="button">Elsewhere</button>
    </div>,
  )
  return screen.getByRole('button', { name: 'About Alerts' })
}

/** A tap as a phone sends it: a touch press, the focus Android gives a
 *  tapped button, the click. */
function tap(el: Element) {
  fireEvent.pointerDown(el, { pointerType: 'touch' })
  act(() => { (el as HTMLElement).focus() })
  fireEvent.click(el, { detail: 1 })
}

/** A mouse click: the press, the focus it brings, the click. */
function click(el: Element) {
  fireEvent.pointerDown(el, { pointerType: 'mouse' })
  act(() => { (el as HTMLElement).focus() })
  fireEvent.click(el, { detail: 1 })
}

describe('InfoTip', () => {
  it('is a named button, collapsed, with no text until it opens', () => {
    const button = renderTip()
    expect(button).toHaveAttribute('type', 'button')
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(button).not.toHaveAttribute('aria-describedby')
    expect(screen.queryByText(TEXT)).toBeNull()
  })

  it('opens on a resting mouse after the delay, not before, and closes when it leaves', () => {
    vi.useFakeTimers()
    const button = renderTip()
    fireEvent.pointerEnter(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(250) })
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(100) })
    const tip = screen.getByRole('tooltip')
    expect(tip.textContent).toBe(TEXT)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(button.getAttribute('aria-describedby')).toBe(tip.id)
    // The pointer crossing onto the bubble keeps it: it can be read there.
    fireEvent.pointerLeave(button, { pointerType: 'mouse' })
    fireEvent.pointerEnter(tip, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(300) })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    fireEvent.pointerLeave(tip, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(200) })
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(button).not.toHaveAttribute('aria-describedby')
  })

  it('a mouse that leaves before the delay opens nothing', () => {
    vi.useFakeTimers()
    const button = renderTip()
    fireEvent.pointerEnter(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(200) })
    fireEvent.pointerLeave(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(400) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a click while the hover shows it pins it, and the next click closes it', () => {
    vi.useFakeTimers()
    const button = renderTip()
    fireEvent.pointerEnter(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(350) })
    click(button)
    fireEvent.pointerLeave(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(400) })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    click(button)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('opens on keyboard focus and closes on blur', () => {
    const button = renderTip()
    act(() => { button.focus() })
    expect(screen.getByRole('tooltip').textContent).toBe(TEXT)
    act(() => { screen.getByText('Elsewhere').focus() })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('Enter after the focus opened it pins it, the next Enter closes it, and Enter opens it again', () => {
    const button = renderTip()
    act(() => { button.focus() })
    // A screen reader focuses, then activates: the bubble must stay.
    fireEvent.click(button, { detail: 0 })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    fireEvent.click(button, { detail: 0 })
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.click(button, { detail: 0 })
    expect(screen.getByRole('tooltip')).toBeTruthy()
  })

  it('a bubble pinned from closed puts its text in a polite live region, and only while pinned', () => {
    const button = renderTip()
    const live = screen.getByRole('status')
    expect(live.textContent).toBe('')
    tap(button)
    expect(live.textContent).toBe(TEXT)
    tap(button)
    expect(live.textContent).toBe('')
    // A close other than the toggle empties it too.
    tap(button)
    expect(live.textContent).toBe(TEXT)
    fireEvent.pointerDown(screen.getByText('Elsewhere'), { pointerType: 'touch' })
    expect(live.textContent).toBe('')
  })

  it('a bubble the keyboard focus opened is not read a second time when Enter pins it', () => {
    const button = renderTip()
    const live = screen.getByRole('status')
    act(() => { button.focus() })
    // The focus reads it through aria-describedby.
    expect(button.getAttribute('aria-describedby')).toBe(screen.getByRole('tooltip').id)
    fireEvent.click(button, { detail: 0 })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    expect(live.textContent).toBe('')
  })

  it('a bubble the hover opened is read when a click pins it', () => {
    vi.useFakeTimers()
    const button = renderTip()
    const live = screen.getByRole('status')
    fireEvent.pointerEnter(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(350) })
    expect(live.textContent).toBe('')
    fireEvent.pointerDown(button, { pointerType: 'mouse' })
    fireEvent.click(button, { detail: 1 })
    expect(live.textContent).toBe(TEXT)
  })

  it('a hover-opened bubble that then takes the keyboard focus stays when the mouse leaves', () => {
    vi.useFakeTimers()
    const button = renderTip()
    fireEvent.pointerEnter(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(350) })
    act(() => { button.focus() })
    fireEvent.pointerLeave(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(300) })
    expect(screen.getByRole('tooltip')).toBeTruthy()
  })

  it('a tap opens it pinned, never as a keyboard focus, and the next tap closes it', () => {
    const button = renderTip()
    tap(button)
    expect(screen.getByRole('tooltip')).toBeTruthy()
    // The touch's own pointer enter starts no hover.
    fireEvent.pointerLeave(button, { pointerType: 'touch' })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    tap(button)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a pinned bubble survives a press on its own text and closes on a press outside', () => {
    const button = renderTip()
    tap(button)
    const tip = screen.getByRole('tooltip')
    fireEvent.pointerDown(tip, { pointerType: 'touch' })
    act(() => { button.blur() })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    fireEvent.pointerDown(screen.getByText('Elsewhere'), { pointerType: 'touch' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a pinned bubble closes when the focus moves on to another control', () => {
    const button = renderTip()
    click(button)
    expect(screen.getByRole('tooltip')).toBeTruthy()
    act(() => { screen.getByText('Elsewhere').focus() })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('closes on Escape and gives the focus back to the button', () => {
    const button = renderTip()
    tap(button)
    act(() => { button.blur() })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    pressEscape(document.body)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(document.activeElement).toBe(button)
    // The focus given back does not open it again.
    expect(screen.queryByRole('tooltip')).toBeNull()
    // The next keyboard focus still does.
    act(() => { screen.getByText('Elsewhere').focus() })
    act(() => { button.focus() })
    expect(screen.getByRole('tooltip')).toBeTruthy()
  })

  it('Escape on a bubble the hover opened leaves the focus in the field being typed in', () => {
    vi.useFakeTimers()
    const button = renderTip()
    const field = screen.getByLabelText('Name')
    act(() => { field.focus() })
    fireEvent.pointerEnter(button, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(350) })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    pressEscape(field)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(document.activeElement).toBe(field)
  })

  it('moves along with the scroll that brought the focused button into view, closes on a later one', () => {
    vi.useFakeTimers()
    const button = renderTip()
    const rect = (top: number) => ({ top, bottom: top + 32, left: 100, right: 132, width: 32, height: 32, x: 100, y: top, toJSON: () => ({}) }) as DOMRect
    const spy = vi.spyOn(button, 'getBoundingClientRect').mockReturnValue(rect(400))
    act(() => { button.focus() })
    const before = screen.getByRole('tooltip').style.top
    spy.mockReturnValue(rect(200))
    fireEvent.scroll(window)
    expect(screen.getByRole('tooltip')).toBeTruthy()
    expect(screen.getByRole('tooltip').style.top).not.toBe(before)
    act(() => { vi.advanceTimersByTime(300) })
    fireEvent.scroll(document.body)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a scroll inside the bubble does not close it', () => {
    vi.useFakeTimers()
    const button = renderTip()
    fireEvent.click(button, { detail: 0 })
    act(() => { vi.advanceTimersByTime(300) })
    fireEvent.scroll(screen.getByRole('tooltip'))
    expect(screen.getByRole('tooltip')).toBeTruthy()
  })

  it('shows one bubble at a time', () => {
    render(
      <div>
        <InfoTip label="First">One text</InfoTip>
        <InfoTip label="Second">Two text</InfoTip>
      </div>,
    )
    const first = screen.getByRole('button', { name: 'About First' })
    click(first)
    expect(screen.getByRole('tooltip').textContent).toBe('One text')
    fireEvent.click(screen.getByRole('button', { name: 'About Second' }), { detail: 0 })
    expect(screen.getAllByRole('tooltip')).toHaveLength(1)
    expect(screen.getByRole('tooltip').textContent).toBe('Two text')
  })

  it('a tap or a click on the button beside a checkbox\'s label never toggles the checkbox', () => {
    function Row() {
      const [on, setOn] = useState(true)
      return (
        <div>
          <input id="alerts" type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} />
          <label htmlFor="alerts">Alerts</label>
          <InfoTip label="Alerts">{TEXT}</InfoTip>
        </div>
      )
    }
    render(<Row />)
    const box = screen.getByRole('checkbox', { name: 'Alerts' })
    const button = screen.getByRole('button', { name: 'About Alerts' })
    tap(button)
    expect(screen.getByRole('tooltip')).toBeTruthy()
    expect(box).toBeChecked()
    click(button)
    expect(box).toBeChecked()
    fireEvent.click(screen.getByText('Alerts'))
    expect(box).not.toBeChecked()
  })

  it('inside a dialog it portals into the dialog, Escape and a backdrop press close the bubble alone', () => {
    const onClose = vi.fn()
    render(
      <Dialog onClose={onClose} aria-label="Add user">
        <span>Role</span>
        <InfoTip label="Role">{TEXT}</InfoTip>
      </Dialog>,
    )
    const dialog = screen.getByRole('dialog')
    const button = within(dialog).getByRole('button', { name: 'About Role' })
    click(button)
    const tip = screen.getByRole('tooltip')
    expect(dialog.contains(tip)).toBe(true)
    pressEscape(button)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(button)

    fireEvent.click(button, { detail: 0 })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    // A press on the bubble's text may focus the dialog element: it stays.
    fireEvent.pointerDown(screen.getByRole('tooltip'))
    act(() => { dialog.focus() })
    expect(screen.getByRole('tooltip')).toBeTruthy()
    fireEvent.pointerDown(dialog)
    fireEvent.click(dialog, { detail: 1 })
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    pressEscape(dialog)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Escape after a press on the bubble\'s text inside a dialog gives the focus back to the button', () => {
    const onClose = vi.fn()
    render(
      <Dialog onClose={onClose} aria-label="Add user">
        <span>Role</span>
        <InfoTip label="Role">{TEXT}</InfoTip>
      </Dialog>,
    )
    const dialog = screen.getByRole('dialog')
    const button = within(dialog).getByRole('button', { name: 'About Role' })
    click(button)
    fireEvent.pointerDown(screen.getByRole('tooltip'))
    act(() => { dialog.focus() })
    expect(document.activeElement).toBe(dialog)
    pressEscape(dialog)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(button)
  })
})

describe('bubble placement', () => {
  const viewport = { width: 400, height: 800 }
  const size = { width: 200, height: 100 }

  it('sits above the trigger, centred', () => {
    expect(clampBubble({ x: 200, top: 300, bottom: 332 }, size, viewport)).toEqual({ left: 100, top: 194, below: false })
  })

  it('goes below when there is no room above', () => {
    expect(clampBubble({ x: 200, top: 40, bottom: 72 }, size, viewport)).toEqual({ left: 100, top: 78, below: true })
  })

  it('stays inside the left and right edges', () => {
    expect(clampBubble({ x: 10, top: 300, bottom: 332 }, size, viewport).left).toBe(8)
    expect(clampBubble({ x: 395, top: 300, bottom: 332 }, size, viewport).left).toBe(192)
  })

  it('above a button below the fold, stays inside the bottom edge', () => {
    expect(clampBubble({ x: 200, top: 900, bottom: 932 }, size, viewport).top).toBe(692)
  })

  it('taller than both sides, takes the roomier side, clamped', () => {
    const tall = { width: 200, height: 500 }
    const small = { width: 400, height: 600 }
    expect(clampBubble({ x: 200, top: 100, bottom: 132 }, tall, small)).toEqual({ left: 100, top: 92, below: true })
    expect(clampBubble({ x: 200, top: 450, bottom: 482 }, tall, small)).toEqual({ left: 100, top: 8, below: false })
  })
})
