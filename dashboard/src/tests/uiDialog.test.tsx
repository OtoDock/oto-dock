import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import Dialog from '../components/ui/Dialog'
import { usePortalRoot } from '../components/ui/portalRoot'
import { pushEscHandler } from '../lib/escStack'
import { pressEscape } from './dialogsHarness'

function Opener({ children, onClose }: { children?: React.ReactNode; onClose?: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <button>Behind</button>
      {open && (
        <Dialog title="Settings" onClose={() => { onClose?.(); setOpen(false) }}>
          <div className="p-4">
            {children ?? (
              <>
                <input aria-label="Name" />
                <button>Save</button>
              </>
            )}
          </div>
        </Dialog>
      )}
    </>
  )
}

describe('Dialog', () => {
  it('opens as a modal dialog labelled by its title, its content inside the <dialog>', () => {
    render(<Opener />)
    fireEvent.click(screen.getByText('Open'))
    const dialog = screen.getByRole('dialog', { name: 'Settings' })
    expect(dialog.tagName).toBe('DIALOG')
    expect(dialog).toHaveAttribute('open')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(within(dialog).getByLabelText('Name')).toBeTruthy()
  })

  it('focuses the dialog itself when nothing asks for the focus, and a child autoFocus when one does', () => {
    render(<Opener />)
    fireEvent.click(screen.getByText('Open'))
    expect(document.activeElement).toBe(screen.getByRole('dialog'))
    fireEvent.click(within(screen.getByRole('dialog')).getByLabelText('Close'))
    render(<Opener><input aria-label="First" autoFocus /></Opener>)
    fireEvent.click(screen.getAllByText('Open')[1])
    expect(document.activeElement).toBe(screen.getByLabelText('First'))
  })

  it('closes on Escape and gives the focus back to what opened it', () => {
    const onClose = vi.fn()
    render(<Opener onClose={onClose} />)
    const open = screen.getByText('Open')
    open.focus()
    fireEvent.click(open)
    pressEscape()
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(open)
  })

  it('wraps Tab and Shift+Tab inside the dialog', () => {
    render(<Opener />)
    fireEvent.click(screen.getByText('Open'))
    const dialog = screen.getByRole('dialog')
    const close = within(dialog).getByLabelText('Close')
    const save = within(dialog).getByText('Save')
    save.focus()
    fireEvent.keyDown(save, { key: 'Tab' })
    expect(document.activeElement).toBe(close)
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(save)
    // From the dialog itself, Tab enters at the first control.
    dialog.focus()
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(document.activeElement).toBe(close)
  })

  it('closes on a backdrop press and click, not on a drag that ends there nor a double click', () => {
    const onClose = vi.fn()
    render(<Opener onClose={onClose} />)
    fireEvent.click(screen.getByText('Open'))
    const dialog = screen.getByRole('dialog')
    const name = within(dialog).getByLabelText('Name')
    // A press in the panel released on the backdrop.
    fireEvent.pointerDown(name)
    fireEvent.click(dialog, { detail: 1 })
    // The second click of a double click.
    fireEvent.pointerDown(dialog)
    fireEvent.click(dialog, { detail: 2 })
    // A click in the panel.
    fireEvent.pointerDown(name)
    fireEvent.click(name, { detail: 1 })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.pointerDown(dialog)
    fireEvent.click(dialog, { detail: 1 })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('refuses the backdrop with dismissible="escape", everything while busy or with "none"', () => {
    const onClose = vi.fn()
    const { rerender } = render(<Dialog onClose={onClose} dismissible="escape" aria-label="Key"><p>secret</p></Dialog>)
    const dialog = screen.getByRole('dialog')
    fireEvent.pointerDown(dialog)
    fireEvent.click(dialog, { detail: 1 })
    expect(onClose).not.toHaveBeenCalled()
    pressEscape(dialog)
    expect(onClose).toHaveBeenCalledTimes(1)
    rerender(<Dialog onClose={onClose} dismissible="escape" busy aria-label="Key"><p>secret</p></Dialog>)
    pressEscape(dialog)
    rerender(<Dialog onClose={onClose} dismissible="none" aria-label="Key"><p>secret</p></Dialog>)
    pressEscape(dialog)
    fireEvent.pointerDown(dialog)
    fireEvent.click(dialog, { detail: 1 })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("turns the browser's own close request into onClose and keeps the dialog open while the parent does", () => {
    const onClose = vi.fn()
    render(<Dialog onClose={onClose} aria-label="Kept"><p>body</p></Dialog>)
    const dialog = screen.getByRole('dialog') as HTMLDialogElement
    // A close request that is not a key (the Android back gesture).
    const cancel = new Event('cancel', { cancelable: true })
    act(() => { dialog.dispatchEvent(cancel) })
    expect(cancel.defaultPrevented).toBe(true)
    expect(onClose).toHaveBeenCalledTimes(1)
    // The browser closing it anyway: shown again, asked again.
    act(() => { dialog.close() })
    expect(dialog.open).toBe(true)
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('gives Escape to a menu open inside it first, and mounts the menu inside the dialog', () => {
    function Menu() {
      const root = usePortalRoot()
      const [open, setOpen] = useState(true)
      const panel = useRef<HTMLDivElement>(null)
      // A portaled menu as usePortalMenu registers one: scoped to its panel.
      useEffect(() => (open ? pushEscHandler(() => setOpen(false), () => panel.current) : undefined), [open])
      return open
        ? createPortal(<div role="menu" ref={panel}><button role="menuitem">Item</button></div>, root)
        : null
    }
    const onClose = vi.fn()
    render(<Dialog onClose={onClose} aria-label="With menu"><Menu /></Dialog>)
    const dialog = screen.getByRole('dialog')
    const menu = screen.getByRole('menu')
    expect(dialog.contains(menu)).toBe(true)
    pressEscape(within(menu).getByRole('menuitem'))
    expect(screen.queryByRole('menu')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    pressEscape(dialog)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('stacks: Escape closes the top dialog only, and the focus returns into the one below', () => {
    function Stacked() {
      const [inner, setInner] = useState(false)
      const [outer, setOuter] = useState(true)
      return outer ? (
        <Dialog aria-label="Outer" onClose={() => setOuter(false)}>
          <button onClick={() => setInner(true)}>Ask</button>
          {inner && (
            <Dialog aria-label="Inner" role="alertdialog" onClose={() => setInner(false)}>
              <button>OK</button>
            </Dialog>
          )}
        </Dialog>
      ) : null
    }
    render(<Stacked />)
    const ask = screen.getByText('Ask')
    ask.focus()
    fireEvent.click(ask)
    expect(screen.getByRole('alertdialog', { name: 'Inner' })).toBeTruthy()
    pressEscape()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByRole('dialog', { name: 'Outer' })).toBeTruthy()
    expect(document.activeElement).toBe(ask)
  })

  it('a dialog nested in another one\'s React tree keeps its own Tab, the outer one does not take it', () => {
    function Nested() {
      return (
        <Dialog aria-label="Browser" onClose={() => {}}>
          <button>Outer one</button>
          <Dialog aria-label="Request" onClose={() => {}}>
            <button>First</button>
            <button>Middle</button>
            <button>Last</button>
          </Dialog>
        </Dialog>
      )
    }
    render(<Nested />)
    const inner = screen.getByRole('dialog', { name: 'Request' })
    const middle = within(inner).getByText('Middle')
    middle.focus()
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    middle.dispatchEvent(tab)
    // Not the outer dialog's wrap: the browser moves the focus on its own.
    expect(tab.defaultPrevented).toBe(false)
    expect(document.activeElement).toBe(middle)
    const last = within(inner).getByText('Last')
    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement).toBe(within(inner).getByText('First'))
    fireEvent.keyDown(within(inner).getByText('First'), { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('falls back to returnFocus when the element that opened it is gone', () => {
    function Gone() {
      const list = useRef<HTMLButtonElement>(null)
      const [row, setRow] = useState(true)
      const [open, setOpen] = useState(false)
      return (
        <>
          <button ref={list}>The list</button>
          {row && <button onClick={() => setOpen(true)}>Delete row</button>}
          {open && (
            <Dialog aria-label="Delete" returnFocus={list} onClose={() => setOpen(false)}>
              <button onClick={() => { setRow(false); setOpen(false) }}>Delete</button>
            </Dialog>
          )}
        </>
      )
    }
    render(<Gone />)
    const opener = screen.getByText('Delete row')
    opener.focus()
    fireEvent.click(opener)
    fireEvent.click(within(screen.getByRole('dialog')).getByText('Delete'))
    expect(document.activeElement).toBe(screen.getByText('The list'))
  })

  it('swallows a held Enter on a button until the first key-up after opening', () => {
    render(<Opener />)
    fireEvent.click(screen.getByText('Open'))
    const save = within(screen.getByRole('dialog')).getByText('Save')
    const held = new KeyboardEvent('keydown', { key: 'Enter', repeat: true, bubbles: true, cancelable: true })
    save.dispatchEvent(held)
    expect(held.defaultPrevented).toBe(true)
    fireEvent.keyUp(save, { key: 'Enter' })
    const again = new KeyboardEvent('keydown', { key: 'Enter', repeat: true, bubbles: true, cancelable: true })
    save.dispatchEvent(again)
    expect(again.defaultPrevented).toBe(false)
  })

  it('holds the page still while open', () => {
    render(<Opener />)
    fireEvent.click(screen.getByText('Open'))
    expect(document.documentElement.style.overflow).toBe('hidden')
    pressEscape()
    expect(document.documentElement.style.overflow).toBe('')
  })
})

describe('a dialog opened from a menu item', () => {
  it("gives the focus back to the menu's button, the item being gone", async () => {
    const { MemoryRouter } = await import('react-router-dom')
    const { default: AppMenu } = await import('../components/apps/AppMenu')
    function Host() {
      const [sharing, setSharing] = useState(false)
      const app = {
        id: 'app-1', slug: 'kanban', title: 'Kanban', scope: 'shared', position: 0, rel_path: 'workspace/apps/kanban',
        updated_at: 't', kind: 'folder', actions: [], actions_sig: 'sig', actions_approved: true, approval_stale: false,
        can_approve: true, can_manage: true, hidden_for_me: false,
      } as unknown as import('../api/apps').PinnedApp
      return (
        <MemoryRouter>
          <AppMenu app={app} onShare={() => setSharing(true)} />
          {sharing && <Dialog aria-label="Share" onClose={() => setSharing(false)}><button>Inside</button></Dialog>}
        </MemoryRouter>
      )
    }
    render(<Host />)
    const button = screen.getByRole('button', { name: 'Options for Kanban' })
    fireEvent.click(button)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Share' }))
    expect(screen.getByRole('dialog', { name: 'Share' })).toBeTruthy()
    pressEscape()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(button)
  })
})
