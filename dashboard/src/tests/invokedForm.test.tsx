import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { StrictMode, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

// ─── A form opened where it was invoked: it scrolls into view, takes the
//     focus on a desktop and leaves it alone on a phone, closes on an
//     Escape typed inside it, and gives the focus back to its opener ───

import { useInvokedForm } from '@/components/common/useInvokedForm'
import { pushEscHandler } from '@/lib/escStack'

function Form({ onCancel, portal }: { onCancel: () => void; portal?: boolean }) {
  const formRef = useRef<HTMLDivElement>(null)
  const form = useInvokedForm(formRef)
  return (
    <div ref={formRef} onKeyDown={(e) => form.onKeyDown(e, onCancel)} data-testid="form">
      <button type="button">My own key</button>
      <input aria-label="Name" />
      <button type="button" onClick={() => form.close(onCancel)}>Cancel</button>
      {portal && createPortal(<input aria-label="In a portal" />, document.body)}
    </div>
  )
}

function Page({ portal }: { portal?: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Add</button>
      {open && <Form onCancel={() => setOpen(false)} portal={portal} />}
    </>
  )
}

const scrolled: Element[] = []
function stubPointer(coarse: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(pointer: coarse)' ? coarse : false,
    media: q, addEventListener: () => {}, removeEventListener: () => {},
  }))
  Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
}

afterEach(() => {
  vi.unstubAllGlobals()
  scrolled.length = 0
})

function open() {
  const add = screen.getByRole('button', { name: 'Add' })
  add.focus()
  fireEvent.click(add)
  return add
}

describe('useInvokedForm', () => {
  it('on a desktop the first field takes the focus and the form scrolls into view', () => {
    stubPointer(false)
    render(<Page />)
    open()
    expect(screen.getByLabelText('Name')).toHaveFocus()
    expect(scrolled).toContain(screen.getByTestId('form'))
  })

  it('on a phone the form scrolls into view and no field is focused', () => {
    stubPointer(true)
    render(<Page />)
    const add = open()
    expect(scrolled).toContain(screen.getByTestId('form'))
    expect(screen.getByLabelText('Name')).not.toHaveFocus()
    expect(add).toHaveFocus()
  })

  it('Escape inside the form closes it and the focus goes back to Add', () => {
    stubPointer(false)
    render(<Page />)
    const add = open()
    fireEvent.keyDown(screen.getByLabelText('Name'), { key: 'Escape' })
    expect(screen.queryByTestId('form')).toBeNull()
    expect(add).toHaveFocus()
  })

  it('Cancel closes it and the focus goes back to Add', () => {
    stubPointer(false)
    render(<Page />)
    const add = open()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByTestId('form')).toBeNull()
    expect(add).toHaveFocus()
  })

  it('an Escape from a portal child does not close the form', () => {
    stubPointer(false)
    render(<Page portal />)
    open()
    fireEvent.keyDown(screen.getByLabelText('In a portal'), { key: 'Escape' })
    expect(screen.getByTestId('form')).toBeInTheDocument()
  })

  it('an Escape a control already handled does not close the form', () => {
    stubPointer(false)
    render(<Page />)
    open()
    const name = screen.getByLabelText('Name')
    name.addEventListener('keydown', (e) => e.preventDefault())
    fireEvent.keyDown(name, { key: 'Escape' })
    expect(screen.getByTestId('form')).toBeInTheDocument()
  })

  it('under StrictMode the focus still goes back to Add, not to the field', () => {
    stubPointer(false)
    render(<StrictMode><Page /></StrictMode>)
    const add = open()
    expect(screen.getByLabelText('Name')).toHaveFocus()
    fireEvent.keyDown(screen.getByLabelText('Name'), { key: 'Escape' })
    expect(add).toHaveFocus()
  })

  it("an Escape in the form reaches no handler of the page's (a sidebar drawer's)", () => {
    stubPointer(false)
    const behind = vi.fn()
    const pop = pushEscHandler(behind)
    try {
      render(<Page />)
      open()
      fireEvent.keyDown(screen.getByLabelText('Name'), { key: 'Escape' })
      expect(screen.queryByTestId('form')).toBeNull()
      expect(behind).not.toHaveBeenCalled()
    } finally {
      pop()
    }
  })

  it('a menu open inside the form takes the Escape first', () => {
    stubPointer(false)
    render(<Page />)
    open()
    const name = screen.getByLabelText('Name')
    const menu = vi.fn()
    const pop = pushEscHandler(menu, name)
    try {
      fireEvent.keyDown(name, { key: 'Escape' })
      expect(menu).toHaveBeenCalledTimes(1)
      expect(screen.getByTestId('form')).toBeInTheDocument()
    } finally {
      pop()
    }
  })

  it('a close while the focus is outside the form leaves it there', () => {
    stubPointer(false)
    render(<><Page /><input aria-label="Elsewhere" /></>)
    open()
    screen.getByLabelText('Elsewhere').focus()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByLabelText('Elsewhere')).toHaveFocus()
  })
})
