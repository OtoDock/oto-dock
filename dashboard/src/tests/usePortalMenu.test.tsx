// usePortalMenu and the workspace toolbar menus built on it: the first item
// takes the focus once placed, arrows/Home/End move it, Tab and Escape close
// and give it back to the button, and Escape closes the menu alone (the
// workspace overlay's handler below it on the escape stack stays put).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createPortal } from 'react-dom'

import WorkspaceToolbar from '@/components/workspace/WorkspaceToolbar'
import { usePortalMenu } from '@/hooks/usePortalMenu'
import { pushEscHandler } from '@/lib/escStack'
import Dialog from '@/components/ui/Dialog'

function Harness() {
  const m = usePortalMenu()
  return (
    <>
      <button ref={m.buttonRef} onClick={m.toggle} aria-haspopup="menu" aria-expanded={m.open}>Open</button>
      {m.open && createPortal(
        <div ref={m.panelRef} role="menu" onKeyDown={m.onKeyDown} style={m.style}>
          <button role="menuitem">One</button>
          <button role="menuitem" disabled>Off</button>
          <button role="menuitem">Two</button>
          <button role="menuitem">Three</button>
        </div>,
        m.portalRoot,
      )}
    </>
  )
}

function RadioHarness({ closeOnMove = false }: { closeOnMove?: boolean }) {
  const m = usePortalMenu({ items: '[role="menuitemradio"]', focus: '[aria-checked="true"]', closeOnMove })
  return (
    <div style={{ overflow: 'auto' }} data-testid="scroller">
      <button ref={m.buttonRef} onClick={m.toggle} aria-haspopup="menu" aria-expanded={m.open}>Open</button>
      {m.open && createPortal(
        <div ref={m.panelRef} role="menu" onKeyDown={m.onKeyDown} style={m.style} data-testid="panel">
          <button role="menuitemradio" aria-checked="false">Live</button>
          <button role="menuitemradio" aria-checked="true">Version 2</button>
          <button role="menuitemradio" aria-checked="false" disabled>Version 1</button>
        </div>,
        m.portalRoot,
      )}
    </div>
  )
}

const focused = () => (document.activeElement as HTMLElement | null)?.textContent

let popWorkspace: () => void = () => {}
const closeWorkspace = vi.fn()
beforeEach(() => {
  closeWorkspace.mockReset()
  // The workspace overlay's own Escape, below any menu it opens.
  popWorkspace = pushEscHandler(closeWorkspace)
})
afterEach(() => popWorkspace())

describe('usePortalMenu', () => {
  it('focuses the first item once open and moves with the arrows, Home and End', async () => {
    render(<Harness />)
    const button = screen.getByRole('button', { name: 'Open' })
    fireEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(focused()).toBe('One'))
    const menu = screen.getByRole('menu')
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(focused()).toBe('Two') // the disabled item is skipped
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(focused()).toBe('One') // wraps
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(focused()).toBe('Three')
    fireEvent.keyDown(menu, { key: 'Home' })
    expect(focused()).toBe('One')
    fireEvent.keyDown(menu, { key: 'End' })
    expect(focused()).toBe('Three')
  })

  it('Escape closes the menu alone and gives the focus back; the next Escape is the workspace', async () => {
    render(<Harness />)
    const button = screen.getByRole('button', { name: 'Open' })
    fireEvent.click(button)
    await waitFor(() => expect(focused()).toBe('One'))
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(document.activeElement).toBe(button)
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(closeWorkspace).not.toHaveBeenCalled()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(closeWorkspace).toHaveBeenCalledTimes(1)
  })

  it('Tab closes and returns the focus to the button; a press outside closes', async () => {
    render(<Harness />)
    const button = screen.getByRole('button', { name: 'Open' })
    fireEvent.click(button)
    await waitFor(() => expect(focused()).toBe('One'))
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Tab' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(document.activeElement).toBe(button)
    fireEvent.click(button)
    expect(screen.getByRole('menu')).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })
})

describe('usePortalMenu options', () => {
  it('moves through the items it names and focuses the chosen one on open', async () => {
    render(<RadioHarness />)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(focused()).toBe('Version 2'))
    const menu = screen.getByRole('menu')
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(focused()).toBe('Live') // the disabled radio is skipped, then it wraps
    act(() => { (document.activeElement as HTMLElement).blur() })
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(focused()).toBe('Version 2') // nothing focused: ArrowUp is the last item
  })

  it('closeOnMove closes on the window losing the focus, a resize and a scroll that moves the button', async () => {
    render(<RadioHarness closeOnMove />)
    const button = screen.getByRole('button', { name: 'Open' })
    for (const move of [
      () => window.dispatchEvent(new Event('blur')),
      () => window.dispatchEvent(new Event('resize')),
      () => fireEvent.scroll(screen.getByTestId('scroller')),
    ]) {
      fireEvent.click(button)
      await screen.findByRole('menu')
      act(move)
      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    }
    fireEvent.click(button)
    await screen.findByRole('menu')
    fireEvent.scroll(screen.getByTestId('panel'))
    expect(screen.getByRole('menu')).toBeInTheDocument() // the menu's own scroll keeps it
  })

  it('without closeOnMove a blur, a resize or a scroll keeps the menu open (the toolbar and RunsOnChip)', async () => {
    render(<RadioHarness />)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    await screen.findByRole('menu')
    act(() => {
      window.dispatchEvent(new Event('blur'))
      window.dispatchEvent(new Event('resize'))
      fireEvent.scroll(screen.getByTestId('scroller'))
    })
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })
})

describe('the workspace toolbar menus by keyboard', () => {
  const real = Object.getOwnPropertyDescriptor(window, 'matchMedia')
  const media = (desktop: boolean) => Object.defineProperty(window, 'matchMedia', {
    configurable: true, writable: true,
    value: (query: string) => ({ matches: desktop, media: query, addEventListener() {}, removeEventListener() {} }),
  })
  afterEach(() => {
    if (real) Object.defineProperty(window, 'matchMedia', real)
    else delete (window as { matchMedia?: unknown }).matchMedia
  })

  const toolbar = (props: Partial<Parameters<typeof WorkspaceToolbar>[0]> = {}) => render(
    <WorkspaceToolbar
      canWrite view="grid" onChangeView={() => {}} onNewFile={() => {}} onNewFolder={() => {}}
      onUpload={() => {}} onUploadFolder={() => {}} {...props}
    />,
  )

  it('opens the Upload menu, walks it, picks Folder, and Escape leaves the workspace open', async () => {
    media(true)
    const { container } = toolbar()
    const upload = screen.getByRole('button', { name: 'Upload' })
    expect(upload).toHaveAttribute('aria-haspopup', 'menu')
    expect(upload).toHaveAttribute('aria-expanded', 'false')
    const folderInput = container.querySelector<HTMLInputElement>('input[webkitdirectory]')!
    const pickFolder = vi.spyOn(folderInput, 'click').mockImplementation(() => {})

    fireEvent.click(upload) // Enter or Space on the button
    await waitFor(() => expect(focused()).toBe('Files'))
    fireEvent.keyDown(screen.getByRole('menu', { name: 'Upload' }), { key: 'ArrowDown' })
    expect(focused()).toBe('Folder')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(closeWorkspace).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(upload)

    fireEvent.click(upload)
    await waitFor(() => expect(focused()).toBe('Files'))
    fireEvent.keyDown(screen.getByRole('menu', { name: 'Upload' }), { key: 'End' })
    fireEvent.click(document.activeElement!) // Enter on the item
    expect(pickFolder).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('a click opens either menu with no row painted by its focus: only a keyboard focus paints', async () => {
    media(true)
    toolbar()
    for (const [button, name] of [['Upload', 'Upload'], ['File', 'New file']] as const) {
      fireEvent.click(screen.getByRole('button', { name: button }))
      const menu = screen.getByRole('menu', { name })
      for (const row of within(menu).getAllByRole('menuitem')) {
        expect(row.className).not.toMatch(/(^|\s)focus:bg-/)
        expect(row.className).toContain('focus-visible:bg-surface-hover')
      }
      fireEvent.keyDown(document, { key: 'Escape' })
      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    }
  })

  it('opens the file picker straight away where there is no folder picker', () => {
    media(false)
    const { container } = toolbar()
    const upload = screen.getByRole('button', { name: 'Upload' })
    expect(upload).not.toHaveAttribute('aria-haspopup')
    expect(container.querySelector('input[webkitdirectory]')).toBeNull()
    const files = container.querySelector<HTMLInputElement>('input[type="file"]')!
    const pick = vi.spyOn(files, 'click').mockImplementation(() => {})
    fireEvent.click(upload)
    expect(pick).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('the New File menu is a keyboard menu too', async () => {
    media(true)
    const onNewFile = vi.fn()
    toolbar({ onNewFile })
    const button = screen.getByRole('button', { name: 'File' })
    expect(button).toHaveAttribute('aria-haspopup', 'menu')
    fireEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(focused()).toContain('Markdown'))
    fireEvent.keyDown(screen.getByRole('menu', { name: 'New file' }), { key: 'ArrowDown' })
    expect(focused()).toContain('Text File')
    fireEvent.click(document.activeElement!)
    expect(onNewFile).toHaveBeenCalledWith('.txt')
    expect(screen.queryByRole('menu')).toBeNull()
  })
})

describe('usePortalMenu inside a dialog', () => {
  it('mounts its panel in the dialog, takes Escape before it, and a backdrop press closes only the menu', async () => {
    const onClose = vi.fn()
    render(<Dialog aria-label="Edit" onClose={onClose}><Harness /></Dialog>)
    const dialog = screen.getByRole('dialog', { name: 'Edit' })
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    const menu = screen.getByRole('menu')
    expect(dialog.contains(menu)).toBe(true)
    await waitFor(() => expect(focused()).toBe('One'))
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    expect(closeWorkspace).not.toHaveBeenCalled()
    // Open again; a press on the backdrop closes the menu, not the dialog.
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    fireEvent.pointerDown(dialog)
    fireEvent.mouseDown(dialog)
    fireEvent.click(dialog, { detail: 1 })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(closeWorkspace).not.toHaveBeenCalled()
  })
})
