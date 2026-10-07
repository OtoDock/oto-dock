import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

// The document pane, its host on the chat page, its Versions menu and the
// chat's cards: tabs newest first with closed ones gone and a dot on a
// fresh one; a version read-only with the "Not live" mark; the docked half
// sliding and the pane hidden (inert) while minimized or behind an overlay;
// Esc on the floating window only after the person touched it.

// `edited` and `changed`: the document holds unsaved edits and the file
// changed under them, so a leave is refused with its note.
const h = vi.hoisted(() => ({ edited: false, changed: false, win: null as Window | null }))
vi.mock('@/hooks/useCollaboraLiveReload', () => ({
  useCollaboraLiveReload: (args: { reload: () => void }) => ({
    iframeRef: {
      get current() { return h.win ? { contentWindow: h.win } : null },
      set current(_v: unknown) { /* the frame element itself is not needed */ },
    },
    get reloadAvailable() { return h.changed },
    doReload: () => args.reload(),
    modifiedRef: { get current() { return h.edited }, set current(v: boolean) { h.edited = v } },
  }),
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ authConfig: null }) }))

import DocumentPane from '@/components/chat/media/DocumentPane'
import DocumentVersionsMenu from '@/components/chat/media/DocumentVersionsMenu'
import ChatDocumentPane, { useDocumentChips, useDocumentPaneForm } from '@/pages/agent/chat/ChatDocumentPane'
import DocumentCard from '@/components/chat/media/DocumentCard'
import ArtifactDock from '@/components/chat/artifacts/ArtifactDock'
import { paneChat, useDocumentPaneStore, useDocumentPushStore } from '@/store/documentPaneStore'
import type { ChatDocument } from '@/api/documents'

const ORIGIN = window.location.origin
const listing: ChatDocument[] = [
  {
    file_id: 'fb', filename: 'budget.xlsx', download_url: '/v1/media/b', generation: 200,
    versions: [{ snapshot_id: 'b1', message_id: 9, version: 1, generation: 200, turn: 2, available: true }],
  },
  {
    file_id: 'fa', filename: 'report.docx', download_url: '/v1/media/a', generation: 100,
    versions: [
      { snapshot_id: 'a2', message_id: 5, version: 2, generation: 100, turn: 2, available: true },
      { snapshot_id: 'a1', message_id: 3, version: 1, generation: 50, turn: 1, available: false },
    ],
  },
]

function stubFetch(docs: ChatDocument[] = listing) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes('/v1/documents/chat-documents')) {
      return { ok: true, status: 200, json: async () => ({ documents: docs }) }
    }
    return { ok: true, status: 200, json: async () => ({ wopi_url: `${ORIGIN}/collabora/x`, access_token: 'minted' }) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>)
}

beforeEach(() => {
  h.edited = false
  h.changed = false
  h.win = null
  localStorage.clear()
  useDocumentPaneStore.setState({ owner: 'me', byChat: {} })
  useDocumentPushStore.setState({ pushed: {}, known: {}, dirty: {}, dirtyNames: {}, attended: {} })
  vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {})
})
afterEach(() => vi.unstubAllGlobals())

describe('DocumentPane', () => {
  it('shows a tab per document newest first, the active one selected, closed ones gone, a dot on fresh ones', async () => {
    stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    useDocumentPaneStore.setState((s) => ({ byChat: { c1: { ...s.byChat.c1, fresh: ['fb'] } } }))
    wrap(<DocumentPane chatId="c1" placement="docked" shown />)
    const tabs = await screen.findAllByRole('tab')
    expect(tabs.map((t) => t.getAttribute('title'))).toEqual(['budget.xlsx', 'report.docx'])
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true')
    expect(within(tabs[0]).getByLabelText('new')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close budget.xlsx' }))
    await waitFor(() => expect(screen.getAllByRole('tab')).toHaveLength(1))
    expect(paneChat('c1').closed).toEqual(['fb'])
  })

  it('a version shows read-only with the "Not live" mark; Live returns', async () => {
    const fetchMock = stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    wrap(<DocumentPane chatId="c1" placement="docked" shown />)
    await screen.findAllByRole('tab')
    fireEvent.click(screen.getByRole('button', { name: 'Versions' }))
    const items = await screen.findAllByRole('menuitemradio')
    expect(items.map((i) => i.textContent)).toEqual([
      expect.stringContaining('Live'), expect.stringContaining('Version 2'), expect.stringContaining('Version 1'),
    ])
    expect(items[0]).toHaveAttribute('aria-checked', 'true')
    expect(items[2]).toBeDisabled()
    fireEvent.click(items[1])
    await waitFor(() => expect(paneChat('c1').view).toEqual({ fa: 'a2' }))
    expect(await screen.findByText(/Not live · version 2/)).toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes('snapshot-wopi-url?chat_id=c1&snapshot_id=a2'))).toBe(true))
    expect(screen.queryByRole('link', { name: 'Download' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Versions' }))
    fireEvent.click((await screen.findAllByRole('menuitemradio'))[0])
    await waitFor(() => expect(paneChat('c1').view).toEqual({}))
  })

  it('minimize and close move the pane state', async () => {
    stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    wrap(<DocumentPane chatId="c1" placement="docked" shown />)
    await screen.findAllByRole('tab')
    fireEvent.click(screen.getByRole('button', { name: 'Minimize' }))
    expect(paneChat('c1').minimized).toBe(true)
    useDocumentPaneStore.getState().restore('c1')
    fireEvent.click(screen.getByRole('button', { name: 'Close the document pane' }))
    await waitFor(() => expect(paneChat('c1').open).toBe(false))
  })
})

describe('the Live row of the Versions menu', () => {
  const liveRow = async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Versions' }))
    return (await screen.findAllByRole('menuitemradio'))[0]
  }

  it('says "view only" when the mint gives a view token, "editable" for an edit one', async () => {
    for (const [permissions, words] of [['view', 'view only'], ['edit', 'editable']] as const) {
      vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.includes('/v1/documents/chat-documents')
        ? { ok: true, status: 200, json: async () => ({ documents: listing }) }
        : { ok: true, status: 200, json: async () => ({ wopi_url: `${ORIGIN}/collabora/x`, access_token: 't', permissions }) })))
      useDocumentPaneStore.getState().openFile('c1', 'fa')
      const { unmount } = wrap(<DocumentPane chatId="c1" placement="docked" shown />)
      await screen.findAllByRole('tab')
      await waitFor(async () => expect((await liveRow()).textContent).toContain(words))
      unmount()
    }
  })

  it("reads a pushed token's own claim, and says nothing while unknown", async () => {
    stubFetch()
    const claims = btoa(JSON.stringify({ permissions: 'view' })).replace(/=+$/, '')
    useDocumentPushStore.setState({ pushed: { c1: { fa: {
      fileId: 'fa', filename: 'report.docx', wopiUrl: `${ORIGIN}/collabora/x`, accessToken: `h.${claims}.s`,
      downloadUrl: '/d', generation: Date.now(),
    } } } })
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    const { unmount } = wrap(<DocumentPane chatId="c1" placement="docked" shown />)
    await screen.findAllByRole('tab')
    expect((await liveRow()).textContent).toContain('view only')
    unmount()
    useDocumentPushStore.setState({ pushed: {} })
    wrap(<DocumentPane chatId="c1" placement="docked" shown />)
    await screen.findAllByRole('tab')
    // stubFetch's mint names no permissions.
    const row = await liveRow()
    expect(row.textContent).not.toContain('editable')
    expect(row.textContent).not.toContain('view only')
  })
})

describe('the Versions menu by keyboard', () => {
  it('focuses the checked item, moves with the arrows, and Esc closes only the menu', async () => {
    const onPick = vi.fn()
    render(<DocumentVersionsMenu versions={listing[1].versions} current={null} onPick={onPick} />)
    fireEvent.click(screen.getByRole('button', { name: 'Versions' }))
    const items = await screen.findAllByRole('menuitemradio')
    await waitFor(() => expect(document.activeElement).toBe(items[0]))
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[1])
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[0]) // the gone version is skipped
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(onPick).not.toHaveBeenCalled()
  })
})

describe('ChatDocumentPane on the chat page', () => {
  it('docked: the right half while shown; minimized it narrows to nothing and goes inert', async () => {
    stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    const { container } = wrap(<ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />)
    const dock = container.querySelector('[data-document-pane-dock]')!
    expect(dock.className).toContain('w-1/2')
    act(() => useDocumentPaneStore.getState().minimize('c1'))
    expect(dock.className).toContain('w-0')
    expect(dock.firstElementChild).toHaveAttribute('inert')
    // The editor stays mounted while minimized.
    expect(container.querySelector('[data-document-pane]')).toBeTruthy()
  })

  it('an overlay hides the pane without closing it', async () => {
    stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    const { container } = wrap(<ChatDocumentPane chatId="c1" placement="docked" hidden userSub="me" />)
    expect(container.querySelector('[data-document-pane-dock]')!.className).toContain('w-0')
    expect(paneChat('c1').open).toBe(true)
  })

  it('floating: below the TopBar; Esc closes it only after the person touched it', async () => {
    stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    const { container } = wrap(<ChatDocumentPane chatId="c1" placement="floating" hidden={false} userSub="me" />)
    expect((container.firstElementChild as HTMLElement).className).toContain('top-12')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(paneChat('c1').open).toBe(true)
    fireEvent.pointerDown(container.querySelector('[data-document-pane]')!)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(paneChat('c1').open).toBe(false))
  })

  it("renders nothing of another person's stored pane and asks for no token", async () => {
    const fetchMock = stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    useDocumentPaneStore.setState({ owner: 'someone-else' })
    const { container } = wrap(<ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />)
    expect(container.querySelector('[data-document-pane]')).toBeNull()
    await waitFor(() => expect(useDocumentPaneStore.getState().owner).toBe('me'))
    expect(paneChat('c1').open).toBe(false)
    await new Promise((r) => setTimeout(r, 0))
    expect(container.querySelector('[data-document-pane]')).toBeNull()
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('wopi-url'))).toBe(false)
  })

  it('first sight of a chat seeds a closed pane from the listing', async () => {
    stubFetch()
    wrap(<ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />)
    await waitFor(() => expect(paneChat('c1').seen).toBe(200))
    expect(paneChat('c1').open).toBe(false)
  })
})

describe('changes made from outside the pane leave the document first', () => {
  const editedAndChanged = () => { h.edited = true; h.changed = true }
  // The pane already acted on every push in the listing.
  const openOnReport = () => {
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    useDocumentPaneStore.setState((s) => ({ byChat: { c1: { ...s.byChat.c1, seen: 300 } } }))
  }

  it('Esc on the floating window keeps a document the file changed under, with the note', async () => {
    stubFetch()
    openOnReport()
    const { container } = wrap(<ChatDocumentPane chatId="c1" placement="floating" hidden={false} userSub="me" />)
    await screen.findAllByRole('tab')
    editedAndChanged()
    fireEvent.pointerDown(container.querySelector('[data-document-pane]')!)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(await screen.findByText(/This file changed while you edited it/)).toBeInTheDocument()
    expect(paneChat('c1').open).toBe(true)
  })

  it('a card for another document leaves first; refused, the pane stays on the edited one', async () => {
    stubFetch()
    openOnReport()
    wrap(<>
      <ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />
      <DocumentCard chatId="c1" fileId="fb" filename="budget.xlsx" downloadUrl="/v1/media/b" snapshotId="b1" />
    </>)
    await screen.findAllByRole('tab')
    editedAndChanged()
    fireEvent.click(await screen.findByTitle('Open the document'))
    expect(await screen.findByText(/This file changed while you edited it/)).toBeInTheDocument()
    expect(paneChat('c1').active).toBe('fa')
    // Resolved in the editor, then Reload: the next card goes through.
    h.changed = false
    h.edited = false
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    fireEvent.click(screen.getByTitle('Open the document'))
    await waitFor(() => expect(paneChat('c1').active).toBe('fb'))
  })

  it('a chip X on the edited document restores the pane instead of closing it', async () => {
    stubFetch()
    openOnReport()
    editedAndChanged()
    let chips: ReturnType<typeof useDocumentChips> = null
    function Chips() { chips = useDocumentChips('c1', false); return null }
    wrap(<><ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" /><Chips /></>)
    await screen.findAllByRole('tab')
    act(() => useDocumentPaneStore.getState().minimize('c1'))
    await waitFor(() => expect(chips).not.toBeNull())
    act(() => chips!.onClose('fa'))
    await waitFor(() => expect(paneChat('c1').minimized).toBe(false))
    expect(paneChat('c1').closed).toEqual([])
  })
})

describe('the pane form', () => {
  it('holds while the document has unsaved edits, then follows the window', async () => {
    const width = window.innerWidth
    const setWidth = (w: number) => { Object.defineProperty(window, 'innerWidth', { value: w, configurable: true }) }
    setWidth(1280)
    useDocumentPushStore.getState().setDirty('c1', 'fa')
    const { result } = renderHook(() => useDocumentPaneForm(false, 'c1'))
    expect(result.current).toBe('docked')
    setWidth(800)
    act(() => { window.dispatchEvent(new Event('resize')) })
    await new Promise((r) => setTimeout(r, 150))
    expect(result.current).toBe('docked')
    act(() => useDocumentPushStore.getState().setDirty('c1', null))
    await waitFor(() => expect(result.current).toBe('floating'))
    setWidth(width)
  })
})

describe('a version whose copy is gone', () => {
  it('falls back to Live with the note', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/v1/documents/chat-documents')) return { ok: true, status: 200, json: async () => ({ documents: listing }) }
      if (url.includes('snapshot-wopi-url')) return { ok: false, status: 404, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => ({ wopi_url: `${ORIGIN}/collabora/x`, access_token: 'minted' }) }
    })
    vi.stubGlobal('fetch', fetchMock)
    useDocumentPaneStore.getState().openFile('c1', 'fa', 'a2')
    wrap(<DocumentPane chatId="c1" placement="docked" shown />)
    expect(await screen.findByText(/That version is no longer available/)).toBeInTheDocument()
    expect(paneChat('c1').view).toEqual({})
  })
})

describe('a pane put away saves its unsaved edits', () => {
  const saves = (post: { mock: { calls: unknown[][] } }) => post.mock.calls
    .filter(([m]) => String(m).includes('"Action_Save"')).length

  it('Minimize asks the editor to save, and the editor stays mounted', async () => {
    stubFetch()
    h.edited = true
    h.win = window
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    const { container } = wrap(<ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />)
    await screen.findAllByRole('tab')
    expect(saves(post)).toBe(0)
    fireEvent.click(screen.getByRole('button', { name: 'Minimize' }))
    await waitFor(() => expect(saves(post)).toBe(1))
    expect(container.querySelector('iframe')).toBeTruthy()
    post.mockRestore()
  })

  it('an overlay over the pane asks for a save too, a clean document asks nothing', async () => {
    stubFetch()
    h.win = window
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = render(<QueryClientProvider client={qc}>
      <ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />
    </QueryClientProvider>)
    await screen.findAllByRole('tab')
    view.rerender(<QueryClientProvider client={qc}>
      <ChatDocumentPane chatId="c1" placement="docked" hidden userSub="me" />
    </QueryClientProvider>)
    expect(saves(post)).toBe(0)
    h.edited = true
    view.rerender(<QueryClientProvider client={qc}>
      <ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />
    </QueryClientProvider>)
    view.rerender(<QueryClientProvider client={qc}>
      <ChatDocumentPane chatId="c1" placement="docked" hidden userSub="me" />
    </QueryClientProvider>)
    await waitFor(() => expect(saves(post)).toBe(1))
    post.mockRestore()
  })
})

describe('Fullscreen shows the pane\'s own editor', () => {
  // jsdom has no popover API: a stub that tracks the open state.
  function withPopover() {
    const show = vi.fn(function (this: HTMLElement) { this.setAttribute('data-open', '') })
    const hide = vi.fn(function (this: HTMLElement) { this.removeAttribute('data-open') })
    const matches = HTMLElement.prototype.matches
    Object.assign(HTMLElement.prototype, { showPopover: show, hidePopover: hide })
    const spy = vi.spyOn(HTMLElement.prototype, 'matches').mockImplementation(function (this: HTMLElement, sel: string) {
      return sel === ':popover-open' ? this.hasAttribute('data-open') : matches.call(this, sel)
    })
    return {
      show, hide,
      restore: () => {
        delete (HTMLElement.prototype as Partial<HTMLElement>).showPopover
        delete (HTMLElement.prototype as Partial<HTMLElement>).hidePopover
        spy.mockRestore()
      },
    }
  }

  it('one frame, never reloaded: the box goes to the top layer over the portal and back', async () => {
    const pop = withPopover()
    try {
      stubFetch()
      useDocumentPaneStore.getState().openFile('c1', 'fa')
      wrap(<DocumentPane chatId="c1" placement="floating" shown />)
      await waitFor(() => expect(document.querySelectorAll('iframe')).toHaveLength(1))
      const frame = document.querySelector('iframe')
      const posts = vi.mocked(HTMLFormElement.prototype.submit).mock.calls.length
      const box = document.querySelector('[data-document-frame-box]') as HTMLElement
      expect(box.getAttribute('popover')).toBe('manual')
      expect(box.style.position).toBe('relative')
      fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }))
      const slot = document.querySelector('[data-preview-slot]')
      expect(slot).toBeTruthy()
      expect(slot!.childElementCount).toBe(0)
      await waitFor(() => expect(pop.show).toHaveBeenCalledTimes(1))
      expect(box.style.position).toBe('fixed')
      expect(document.querySelectorAll('iframe')).toHaveLength(1)
      expect(document.querySelector('iframe')).toBe(frame)
      // The portal's Close (the pane's own Close shares the title).
      fireEvent.click(screen.getAllByTitle('Close').slice(-1)[0])
      await waitFor(() => expect(pop.hide).toHaveBeenCalledTimes(1))
      expect(document.querySelector('[data-preview-slot]')).toBeNull()
      expect(box.style.position).toBe('relative')
      expect(document.querySelector('iframe')).toBe(frame)
      expect(vi.mocked(HTMLFormElement.prototype.submit).mock.calls.length).toBe(posts)
    } finally {
      pop.restore()
    }
  })

  it('a pane put away leaves Fullscreen', async () => {
    const pop = withPopover()
    try {
      stubFetch()
      useDocumentPaneStore.getState().openFile('c1', 'fa')
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const view = render(<QueryClientProvider client={qc}><DocumentPane chatId="c1" placement="docked" shown /></QueryClientProvider>)
      await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())
      fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }))
      await waitFor(() => expect(pop.show).toHaveBeenCalledTimes(1))
      view.rerender(<QueryClientProvider client={qc}><DocumentPane chatId="c1" placement="docked" shown={false} /></QueryClientProvider>)
      await waitFor(() => expect(pop.hide).toHaveBeenCalledTimes(1))
      expect(document.querySelector('[data-preview-slot]')).toBeNull()
    } finally {
      pop.restore()
    }
  })

  it('without the popover API there is no Fullscreen', async () => {
    stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    wrap(<DocumentPane chatId="c1" placement="floating" shown />)
    await screen.findAllByRole('tab')
    expect(screen.queryByRole('button', { name: 'Fullscreen' })).toBeNull()
  })
})

describe('the listing catches up with the pushes', () => {
  const listings = (f: ReturnType<typeof stubFetch>) =>
    f.mock.calls.filter(([u]) => String(u).includes('/v1/documents/chat-documents')).length

  it('refetches for a push the listing has no version of, a push with no copy included', async () => {
    const fetchMock = stubFetch()
    useDocumentPushStore.setState({ pushed: { c1: { fa: {
      fileId: 'fa', filename: 'report.docx', wopiUrl: '', downloadUrl: '', generation: 300, snapshotId: undefined,
    } } } })
    wrap(<ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />)
    await waitFor(() => expect(listings(fetchMock)).toBe(1))
    await waitFor(() => expect(listings(fetchMock)).toBeGreaterThanOrEqual(2), { timeout: 2500 })
  })

  it('a push the listing already holds asks for nothing more', async () => {
    const fetchMock = stubFetch()
    useDocumentPushStore.setState({ pushed: { c1: { fa: {
      fileId: 'fa', filename: 'report.docx', wopiUrl: '', downloadUrl: '', generation: 100, snapshotId: 'a2',
    } } } })
    wrap(<ChatDocumentPane chatId="c1" placement="docked" hidden={false} userSub="me" />)
    await waitFor(() => expect(listings(fetchMock)).toBe(1))
    await new Promise((r) => setTimeout(r, 700))
    expect(listings(fetchMock)).toBe(1)
  })
})

describe('the Versions menu and scrolling', () => {
  it('its own list scrolling keeps it open; a scroll that moves the button closes it', async () => {
    render(<DocumentVersionsMenu versions={listing[1].versions} current={null} onPick={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Versions' }))
    const menu = await screen.findByRole('menu')
    fireEvent.scroll(menu)
    expect(screen.queryByRole('menu')).toBeInTheDocument()
    fireEvent.scroll(document)
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })
})

describe('the minimized pane in the left stack', () => {
  it('a chip restores the pane as the person\'s own open (Esc closes it then)', async () => {
    stubFetch()
    useDocumentPaneStore.getState().openFile('c1', 'fa')
    useDocumentPaneStore.getState().minimize('c1')
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useDocumentChips('c1', false), {
      wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
    })
    await waitFor(() => expect(result.current?.chips.length).toBeGreaterThan(0))
    act(() => result.current!.onRestore('fa'))
    expect(paneChat('c1').minimized).toBe(false)
    expect(useDocumentPushStore.getState().attended.c1).toBe(true)
  })

  it('shows four chips then one for the rest; a chip restores, its X closes that file', () => {
    const onRestore = vi.fn()
    const onClose = vi.fn()
    const chips = ['a', 'b', 'c', 'd'].map((id) => ({ fileId: id, title: `${id}.docx` }))
    render(<ArtifactDock windows={[]} minimized={new Set()} onRestore={() => {}} onClose={() => {}}
      documents={{ chips, more: 2, onRestore, onClose }} />)
    fireEvent.click(screen.getByTitle('Open: b.docx'))
    expect(onRestore).toHaveBeenCalledWith('b')
    fireEvent.click(screen.getByTitle('Open: 2 more documents'))
    expect(onRestore).toHaveBeenLastCalledWith()
    fireEvent.click(screen.getAllByTitle('Close')[0])
    expect(onClose).toHaveBeenCalledWith('a')
  })
})
