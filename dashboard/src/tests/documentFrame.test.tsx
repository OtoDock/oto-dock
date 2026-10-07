import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { createRef, type ReactElement } from 'react'

// The document pane's editor (DocumentFrame): where each load gets its
// token (a pushed one under 15 minutes old, else a mint; never persisted),
// what it says when nothing can open, the editor-origin notice, the save a
// leave waits for, the focus a booting editor hands back, and (with the
// real reload hook) what a change of the file does to the document on show.

const h = vi.hoisted(() => ({
  authConfig: null as null | { collabora_origin?: string },
  modified: false,
  reloadAvailable: false,
  frameWindow: null as Window | null,
  /** The real useCollaboraLiveReload for this test. */
  realHook: false,
}))

vi.mock('@/hooks/useCollaboraLiveReload', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/hooks/useCollaboraLiveReload')>()
  return {
    useCollaboraLiveReload: (args: { fileId?: string; reload: () => void }) => (h.realHook
      ? real.useCollaboraLiveReload(args)
      : fakeReloadHook(args)),
  }
})

function fakeReloadHook(args: { reload: () => void }) {
  return {
    iframeRef: {
      el: null as unknown,
      get current() { return h.frameWindow ? { contentWindow: h.frameWindow } : this.el },
      set current(v: unknown) { this.el = v },
    },
    get reloadAvailable() { return h.reloadAvailable },
    doReload: () => args.reload(),
    modifiedRef: { get current() { return h.modified }, set current(v: boolean) { h.modified = v } },
  }
}
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ authConfig: h.authConfig }) }))

import DocumentFrame, {
  FOCUS_AFTER_LOAD_MS, LEAVE_SAVE_TIMEOUT_MS, RELOAD_DEDUPE_MS, SEND_SAVE_TIMEOUT_MS, type DocumentFrameHandle,
} from '@/components/chat/media/DocumentFrame'
import type { PushedDocument } from '@/store/documentPaneStore'
import { emitFileUpdate } from '@/lib/fileUpdates'

const ORIGIN = window.location.origin
const pushedDoc = (over: Partial<PushedDocument> = {}): PushedDocument => ({
  fileId: 'f1', filename: 'report.docx', wopiUrl: `${ORIGIN}/collabora/browser/dist/cool.html?WOPISrc=x`,
  accessToken: 'push-tok', accessTokenTtl: 9, downloadUrl: '/v1/media/f1', generation: Date.now(), ...over,
})

function renderFrame(props: Partial<Parameters<typeof DocumentFrame>[0]> = {}) {
  const ref = createRef<DocumentFrameHandle>()
  const utils = render(
    <DocumentFrame ref={ref} chatId="c1" fileId="f1" filename="report.docx" snapshotId={null} shown {...props} />,
  )
  return { ...utils, ref }
}

const fetchOk = (body: object) => vi.fn(async (_url: string) => ({ ok: true, status: 200, json: async () => body }))
const fetchStatus = (status: number) => vi.fn(async () => ({ ok: false, status, json: async () => ({}) }))
const posted = () => (document.querySelector('form input[name="access_token"]') as HTMLInputElement | null)?.value

beforeEach(() => {
  h.authConfig = null
  h.modified = false
  h.reloadAvailable = false
  h.frameWindow = null
  h.realHook = false
  vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => {})
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('DocumentFrame: the token of each load', () => {
  it('a push under 15 minutes old posts its own token, no mint', () => {
    const fetchMock = fetchOk({})
    vi.stubGlobal('fetch', fetchMock)
    renderFrame({ pushed: pushedDoc() })
    expect(fetchMock).not.toHaveBeenCalled()
    expect(posted()).toBe('push-tok')
  })

  it('an older push, or none, mints through the chat-scoped route', async () => {
    const fetchMock = fetchOk({ wopi_url: `${ORIGIN}/collabora/x`, access_token: 'minted' })
    vi.stubGlobal('fetch', fetchMock)
    renderFrame({ pushed: pushedDoc({ generation: Date.now() - 16 * 60 * 1000 }) })
    await vi.waitFor(() => expect(posted()).toBe('minted'))
    expect(String(fetchMock.mock.calls[0][0])).toContain('/v1/documents/preview-wopi-url?chat_id=c1&file_id=f1')
  })

  it('a version mints a read-only snapshot token', async () => {
    const fetchMock = fetchOk({ wopi_url: `${ORIGIN}/collabora/x`, access_token: 'view' })
    vi.stubGlobal('fetch', fetchMock)
    renderFrame({ snapshotId: 's9', pushed: pushedDoc() })
    await vi.waitFor(() => expect(posted()).toBe('view'))
    expect(String(fetchMock.mock.calls[0][0])).toContain('/v1/documents/snapshot-wopi-url?chat_id=c1&snapshot_id=s9')
  })

  it('a failed mint falls back to a pushed token, else says so with Retry', async () => {
    vi.stubGlobal('fetch', fetchStatus(500))
    const old = pushedDoc({ generation: Date.now() - 60 * 60 * 1000 })
    const { unmount } = renderFrame({ pushed: old })
    await vi.waitFor(() => expect(posted()).toBe('push-tok'))
    unmount()
    renderFrame()
    await vi.waitFor(() => expect(screen.getByText('The document could not be opened.')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('a Live 404 and a version 404 report the file or the copy gone', async () => {
    vi.stubGlobal('fetch', fetchStatus(404))
    const onLiveGone = vi.fn()
    const { unmount } = renderFrame({ onLiveGone })
    await vi.waitFor(() => expect(onLiveGone).toHaveBeenCalled())
    expect(screen.getByText('This file is no longer there.')).toBeInTheDocument()
    unmount()
    const onVersionGone = vi.fn()
    renderFrame({ snapshotId: 's1', onVersionGone })
    await vi.waitFor(() => expect(onVersionGone).toHaveBeenCalled())
  })

  it('a refresh loads again and re-decides the source', async () => {
    const fetchMock = fetchOk({ wopi_url: `${ORIGIN}/collabora/x`, access_token: 'minted' })
    vi.stubGlobal('fetch', fetchMock)
    const { ref } = renderFrame()
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    act(() => ref.current!.refresh())
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  })
})

describe('DocumentFrame: the editor origin', () => {
  it('a foreign-origin URL renders the fix-it notice, not an iframe', () => {
    h.authConfig = { collabora_origin: '' }
    renderFrame({ pushed: pushedDoc({ wopiUrl: 'http://localhost:8410/collabora/browser/dist/cool.html?WOPISrc=x' }) })
    expect(screen.getByText(/can't load from this address/)).toBeInTheDocument()
    expect(screen.getByText(new RegExp(`DASHBOARD_PUBLIC_URL=${ORIGIN}`))).toBeInTheDocument()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('a same-origin URL renders the iframe; no verdict before the server answers', () => {
    renderFrame({ pushed: pushedDoc() })
    expect(document.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByText(/can't load from this address/)).toBeNull()
  })

  it('an editor on its own host names its configured address', () => {
    h.authConfig = { collabora_origin: 'https://collabora.example.com' }
    renderFrame({ pushed: pushedDoc({ wopiUrl: 'https://old-collabora.example.com/browser/dist/cool.html?WOPISrc=x' }) })
    expect(screen.getByText('https://collabora.example.com')).toBeInTheDocument()
    expect(screen.getByText(/Reload the page/)).toBeInTheDocument()
    expect(screen.queryByText(/DASHBOARD_PUBLIC_URL/)).toBeNull()
  })
})

describe('DocumentFrame: leaving a document with unsaved edits', () => {
  const answer = (success: boolean) => act(() => {
    window.dispatchEvent(new MessageEvent('message', {
      data: JSON.stringify({ MessageId: 'Action_Save_Resp', Values: { success } }), source: window,
    }))
  })

  it('a clean document leaves at once', async () => {
    const { ref } = renderFrame({ pushed: pushedDoc() })
    await expect(ref.current!.leave()).resolves.toBe(true)
  })

  it('a dirty one asks Collabora to save and leaves when it confirms', async () => {
    h.modified = true
    h.frameWindow = window
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ pushed: pushedDoc() })
    const leaving = ref.current!.leave()
    expect(JSON.parse(String(post.mock.calls[0][0]))).toMatchObject({
      MessageId: 'Action_Save', Values: { Notify: true, DontTerminateEdit: true },
    })
    answer(true)
    await expect(leaving).resolves.toBe(true)
    post.mockRestore()
  })

  it('no answer in time keeps the document on show with a note', async () => {
    vi.useFakeTimers()
    h.modified = true
    h.frameWindow = window
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ pushed: pushedDoc() })
    let result: boolean | undefined
    const leaving = ref.current!.leave().then((v) => { result = v })
    await act(async () => {
      vi.advanceTimersByTime(LEAVE_SAVE_TIMEOUT_MS + 10)
      await leaving
    })
    expect(result).toBe(false)
    expect(screen.getByText(/Not saved yet: the editor has not confirmed the save/)).toBeInTheDocument()
    post.mockRestore()
  })
})

describe('DocumentFrame: saving without leaving (Minimize, an overlay, a send)', () => {
  const answer = (success: boolean) => act(() => {
    window.dispatchEvent(new MessageEvent('message', {
      data: JSON.stringify({ MessageId: 'Action_Save_Resp', Values: { success } }), source: window,
    }))
  })

  it('a clean document has nothing to save', async () => {
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ pushed: pushedDoc() })
    await expect(ref.current!.save()).resolves.toBe(true)
    expect(post).not.toHaveBeenCalled()
    post.mockRestore()
  })

  it('posts Action_Save, keeps editing, and resolves on the answer with the dirty flag cleared', async () => {
    h.modified = true
    h.frameWindow = window
    const onDirtyChange = vi.fn()
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ pushed: pushedDoc(), onDirtyChange })
    const saving = ref.current!.save()
    expect(JSON.parse(String(post.mock.calls[0][0]))).toEqual({
      MessageId: 'Action_Save',
      Values: { DontTerminateEdit: true, DontSaveIfUnmodified: true, Notify: true },
    })
    answer(true)
    await expect(saving).resolves.toBe(true)
    expect(onDirtyChange).toHaveBeenCalledWith(false)
    expect(screen.queryByText(/Not saved yet/)).toBeNull()
    post.mockRestore()
  })

  it('no answer in time resolves false and leaves no note in the pane', async () => {
    vi.useFakeTimers()
    h.modified = true
    h.frameWindow = window
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ pushed: pushedDoc() })
    let result: boolean | undefined
    const saving = ref.current!.save().then((v) => { result = v })
    await act(async () => {
      vi.advanceTimersByTime(LEAVE_SAVE_TIMEOUT_MS + 10)
      await saving
    })
    expect(result).toBe(false)
    expect(screen.queryByText(/Not saved yet/)).toBeNull()
    post.mockRestore()
  })

  it("a send's save gives up sooner, at its own wait", async () => {
    vi.useFakeTimers()
    h.modified = true
    h.frameWindow = window
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ pushed: pushedDoc() })
    let result: boolean | undefined
    const saving = ref.current!.save(SEND_SAVE_TIMEOUT_MS).then((v) => { result = v })
    await act(async () => { vi.advanceTimersByTime(SEND_SAVE_TIMEOUT_MS - 10) })
    expect(result).toBeUndefined()
    await act(async () => {
      vi.advanceTimersByTime(20)
      await saving
    })
    expect(result).toBe(false)
    expect(SEND_SAVE_TIMEOUT_MS).toBeLessThan(LEAVE_SAVE_TIMEOUT_MS)
    post.mockRestore()
  })

  it('a file changed under the edits is not saved (the server would refuse it)', async () => {
    h.modified = true
    h.reloadAvailable = true
    h.frameWindow = window
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ pushed: pushedDoc() })
    await expect(ref.current!.save()).resolves.toBe(false)
    expect(post).not.toHaveBeenCalled()
    post.mockRestore()
  })

  it('a version has nothing to save', async () => {
    h.modified = true
    h.frameWindow = window
    vi.stubGlobal('fetch', fetchOk({ wopi_url: `${ORIGIN}/collabora/x`, access_token: 'view' }))
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ snapshotId: 's1' })
    await expect(ref.current!.save()).resolves.toBe(true)
    expect(post).not.toHaveBeenCalled()
    post.mockRestore()
  })
})

describe('DocumentFrame: leaving a document the file changed under', () => {
  it('holds the leave without a save: Collabora would confirm a save the server refuses', async () => {
    h.modified = true
    h.reloadAvailable = true
    h.frameWindow = window
    const post = vi.spyOn(window, 'postMessage').mockImplementation(() => {})
    const { ref } = renderFrame({ pushed: pushedDoc() })
    let result: boolean | undefined
    await act(async () => { result = await ref.current!.leave() })
    expect(result).toBe(false)
    expect(post).not.toHaveBeenCalled()
    expect(screen.getByText(/This file changed while you edited it/)).toBeInTheDocument()
    expect(screen.getByText(/This document changed. Reload/)).toBeInTheDocument()
    post.mockRestore()
  })

  it('still holds it once a refused save has cleared the dirty flag', async () => {
    // Collabora reports the document unmodified after its own save, whatever
    // the upload answered: the banner is what tells.
    h.modified = false
    h.reloadAvailable = true
    const { ref } = renderFrame({ pushed: pushedDoc() })
    let result: boolean | undefined
    await act(async () => { result = await ref.current!.leave() })
    expect(result).toBe(false)
    expect(screen.getByText(/Choose in the editor \(discard or overwrite\), then Reload/)).toBeInTheDocument()
  })
})

describe('DocumentFrame: a booting editor does not take the keyboard', () => {
  it('hands focus back to the field that held it while the document loads', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    renderFrame({ pushed: pushedDoc() })
    const back = vi.spyOn(composer, 'focus')
    const iframe = document.querySelector('iframe')!
    act(() => { iframe.focus() })
    expect(back).toHaveBeenCalled()
    expect(document.activeElement).toBe(composer)
    composer.remove()
  })

  it('also when the frame takes focus with no focusin, only a window blur (Chrome)', async () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    renderFrame({ pushed: pushedDoc() })
    const back = vi.spyOn(composer, 'focus')
    const iframe = document.querySelector('iframe')!
    const active = vi.spyOn(document, 'activeElement', 'get').mockReturnValue(iframe)
    await act(async () => {
      window.dispatchEvent(new Event('blur'))
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(back).toHaveBeenCalledTimes(1)
    active.mockRestore()
    composer.remove()
  })

  it('the editor focusing itself again just after "document loaded" is handed back too', () => {
    vi.useFakeTimers()
    h.frameWindow = window
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    renderFrame({ pushed: pushedDoc() })
    const back = vi.spyOn(composer, 'focus')
    const iframe = document.querySelector('iframe')!
    act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        data: JSON.stringify({ MessageId: 'App_LoadingStatus', Values: { Status: 'Document_Loaded' } }), source: window,
      }))
    })
    act(() => { vi.advanceTimersByTime(500) })
    act(() => { iframe.focus() })
    expect(back).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(FOCUS_AFTER_LOAD_MS) })
    act(() => { iframe.focus() })
    expect(back).toHaveBeenCalledTimes(1)
    composer.remove()
  })

  it('a pane sliding under a resting pointer keeps the guard; a real move ends it', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    const { container } = renderFrame({ pushed: pushedDoc() })
    const host = container.querySelector('[data-collabora-host]')!
    const move = (dx: number) => {
      const e = new Event('pointermove', { bubbles: true })
      Object.defineProperty(e, 'movementX', { value: dx })
      Object.defineProperty(e, 'movementY', { value: 0 })
      host.dispatchEvent(e)
    }
    const back = vi.spyOn(composer, 'focus')
    const iframe = document.querySelector('iframe')!
    act(() => { move(0) })
    act(() => { iframe.focus() })
    expect(back).toHaveBeenCalledTimes(1)
    act(() => { move(4) })
    act(() => { iframe.focus() })
    expect(back).toHaveBeenCalledTimes(1)
    composer.remove()
  })

  it('a pointer moving straight into the editor ends it', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    renderFrame({ pushed: pushedDoc() })
    const iframe = document.querySelector('iframe')!
    const over = new Event('pointerover', { bubbles: true })
    Object.defineProperty(over, 'movementX', { value: 3 })
    Object.defineProperty(over, 'movementY', { value: 0 })
    act(() => { iframe.dispatchEvent(over) })
    const back = vi.spyOn(composer, 'focus')
    act(() => { iframe.focus() })
    expect(back).not.toHaveBeenCalled()
    composer.remove()
  })

  it('on a touch screen hands the focus back once, then lets the editor keep it', () => {
    const had = Object.getOwnPropertyDescriptor(window, 'matchMedia')
    Object.defineProperty(window, 'matchMedia', {
      configurable: true, writable: true,
      value: (q: string) => ({ matches: q === '(pointer: coarse)', media: q, addEventListener() {}, removeEventListener() {} }),
    })
    try {
      const composer = document.createElement('textarea')
      document.body.appendChild(composer)
      composer.focus()
      renderFrame({ pushed: pushedDoc() })
      const back = vi.spyOn(composer, 'focus')
      const iframe = document.querySelector('iframe')!
      act(() => { iframe.focus() })
      expect(back).toHaveBeenCalledTimes(1)
      act(() => { iframe.focus() })
      expect(back).toHaveBeenCalledTimes(1)
      composer.remove()
    } finally {
      if (had) Object.defineProperty(window, 'matchMedia', had)
      else delete (window as Partial<Window>).matchMedia
    }
  })

  it('a press on the pane first lets the editor keep it', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    const { container } = renderFrame({ pushed: pushedDoc() })
    const host = container.querySelector('[data-collabora-host]')!
    act(() => { host.dispatchEvent(new Event('pointerdown', { bubbles: true })) })
    const blur = vi.spyOn(composer, 'focus')
    const iframe = document.querySelector('iframe')!
    act(() => { iframe.focus() })
    expect(blur).not.toHaveBeenCalled()
    composer.remove()
  })
})

describe('DocumentFrame: a change of the file on show (the real reload hook)', () => {
  // The hidden form's posts: one per load of the editor.
  let base = 0
  const loads = () => vi.mocked(HTMLFormElement.prototype.submit).mock.calls.length - base
  beforeEach(() => { base = vi.mocked(HTMLFormElement.prototype.submit).mock.calls.length })
  const editInFrame = () => act(() => {
    const win = document.querySelector('iframe')!.contentWindow!
    window.dispatchEvent(new MessageEvent('message', {
      data: JSON.stringify({ MessageId: 'Doc_ModifiedStatus', Values: { Modified: true } }), source: win,
    }))
  })

  it('a new push of the file under unsaved edits offers Reload and keeps the edits', () => {
    h.realHook = true
    const first = pushedDoc()
    const { rerender } = renderFrame({ pushed: first })
    expect(loads()).toBe(1)
    editInFrame()
    rerender(<DocumentFrame chatId="c1" fileId="f1" filename="report.docx" snapshotId={null} shown
      pushed={pushedDoc({ generation: first.generation + 1000, accessToken: 'push-tok-2', wopiUrl: `${ORIGIN}/collabora/browser/dist/cool.html?WOPISrc=y` })} />)
    expect(screen.getByText(/This document changed. Reload/)).toBeInTheDocument()
    expect(loads()).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(loads()).toBeGreaterThan(1)
    // Reload opens the push it was offered for.
    expect(posted()).toBe('push-tok-2')
    expect(screen.queryByText(/This document changed/)).toBeNull()
  })

  const src = (gen: number) => `${ORIGIN}/collabora/browser/dist/cool.html?WOPISrc=${encodeURIComponent(`https://w/wopi/files/f1-${gen}`)}`
  const disk = () => act(() => emitFileUpdate({ agent_slug: 'a', rel_path: 'w/r.docx', file_id: 'f1', source: 'disk' }))
  const show = (pushed: PushedDocument, rerender: (ui: ReactElement) => void) =>
    rerender(<DocumentFrame chatId="c1" fileId="f1" filename="report.docx" snapshotId={null} shown pushed={pushed} />)

  it('in a terminal chat the push comes first: its write\'s file_updated does not load again', async () => {
    h.realHook = true
    const fetchMock = fetchOk({ wopi_url: src(9), access_token: 'minted' })
    vi.stubGlobal('fetch', fetchMock)
    const first = pushedDoc({ wopiUrl: src(1) })
    const { rerender } = renderFrame({ pushed: first })
    expect(loads()).toBe(1)
    show(pushedDoc({ wopiUrl: src(2), generation: first.generation + 1, accessToken: 'push-tok-2' }), rerender)
    expect(loads()).toBe(2)
    expect(posted()).toBe('push-tok-2')
    disk()
    expect(loads()).toBe(2)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('in a headless turn the write comes first: it mints the newest document, the push then loads nothing', async () => {
    h.realHook = true
    const fetchMock = fetchOk({ wopi_url: src(2), access_token: 'minted' })
    vi.stubGlobal('fetch', fetchMock)
    const first = pushedDoc({ wopiUrl: src(1) })
    const { rerender } = renderFrame({ pushed: first })
    expect(loads()).toBe(1)
    // Mid-turn, a while after the pane opened: the pushed frame in memory is
    // the previous push's, so the reload mints instead of reopening that
    // push's document.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(Date.now() + RELOAD_DEDUPE_MS + 10)
    disk()
    await vi.waitFor(() => expect(loads()).toBe(2))
    expect(posted()).toBe('minted')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    // The turn's flush: the push of the document already open.
    show(pushedDoc({ wopiUrl: src(2), generation: first.generation + 1, accessToken: 'push-tok-2' }), rerender)
    expect(loads()).toBe(2)
    // A later push of another document loads it.
    show(pushedDoc({ wopiUrl: src(3), generation: first.generation + 2, accessToken: 'push-tok-3' }), rerender)
    expect(loads()).toBe(3)
    expect(posted()).toBe('push-tok-3')
  })

  it('the person\'s Refresh loads the newest document right after a load', async () => {
    h.realHook = true
    const fetchMock = fetchOk({ wopi_url: src(5), access_token: 'minted' })
    vi.stubGlobal('fetch', fetchMock)
    const { ref } = renderFrame({ pushed: pushedDoc({ wopiUrl: src(5) }) })
    expect(loads()).toBe(1)
    act(() => ref.current!.refresh())
    await vi.waitFor(() => expect(loads()).toBe(2))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('a change that arrives while the pane is hidden waits until it shows', async () => {
    h.realHook = true
    vi.stubGlobal('fetch', fetchOk({ wopi_url: src(4), access_token: 'minted' }))
    const doc = pushedDoc()
    const { rerender } = renderFrame({ pushed: doc, shown: false })
    expect(loads()).toBe(1)
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(Date.now() + RELOAD_DEDUPE_MS + 10)
    disk()
    expect(loads()).toBe(1)
    expect(screen.queryByText(/This document changed/)).toBeNull()
    show(doc, rerender)
    await vi.waitFor(() => expect(loads()).toBe(2))
  })
})
