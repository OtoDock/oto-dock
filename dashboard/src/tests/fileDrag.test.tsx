// A file card dragged to another chat's message box (lib/chat/fileDrag): the
// card's reference, the drop's checks (the drop is untrusted), the fetch by
// the person's own sign-in under the upload cap, and the composer's intake.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, renderHook } from '@testing-library/react'

const q = vi.hoisted(() => ({ enqueue: vi.fn() }))
vi.mock('../lib/chatUploadQueue', async (orig) => ({
  ...(await orig<typeof import('../lib/chatUploadQueue')>()),
  enqueueChatUpload: q.enqueue,
}))
vi.mock('../hooks/useFlag', () => ({ useFlag: () => 1000 }))

import {
  FILE_DRAG_TYPE, REFUSED_NO_ANSWER, REFUSED_NOT_YOURS, REFUSED_OTHER_INSTALL, REFUSED_SHAPE,
  dispositionName, fetchDroppedFile, fileDragRef, keepsDownloadUrl, pickName, readFileDrag, writeFileDrag,
  type FileDragRef,
} from '../lib/chat/fileDrag'
import DisplayFile from '../components/chat/media/DisplayFile'
import { useChatAttachments } from '../pages/agent/chat/useChatAttachments'
import { useChatStore } from '../store/chatStore'

const TOKEN = 'A'.repeat(20) + 'b_-'.repeat(7) + 'z'.repeat(2)

function fakeTransfer(data: Record<string, string> = {}) {
  const store: Record<string, string> = { ...data }
  return {
    get types() { return Object.keys(store) },
    files: [] as File[],
    items: [] as DataTransferItem[],
    effectAllowed: 'all',
    dropEffect: 'none',
    getData: (t: string) => store[t] ?? '',
    setData: (t: string, v: string) => { store[t] = v },
    clearData: () => { for (const k of Object.keys(store)) delete store[k] },
    store,
  }
}

function carrying(ref: unknown) {
  return fakeTransfer({ [FILE_DRAG_TYPE]: typeof ref === 'string' ? ref : JSON.stringify(ref) }) as unknown as DataTransfer
}

const good = (): FileDragRef => ({ v: 1, origin: window.location.origin, token: TOKEN, name: 'report.pdf' })

// The user agents of the browsers the drag was measured in (Chrome 150, Firefox 151),
// and Edge's and Safari's, as they report themselves.
const UA = {
  chrome: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
  edge: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
  firefox: 'Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0',
  safari: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15',
  unknown: 'Mozilla/5.0 (X11; Linux x86_64) SomeBrowser/1.0',
}
const CHROMIUM_HINTS = { brands: [{ brand: 'Chromium', version: '150' }, { brand: 'Google Chrome', version: '150' }] }

function asBrowser(ua: string, hints?: { brands: Array<{ brand: string; version: string }> }) {
  vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(ua)
  if (hints) Object.defineProperty(window.navigator, 'userAgentData', { value: hints, configurable: true })
}

afterEach(() => {
  vi.restoreAllMocks()
  delete (window.navigator as { userAgentData?: unknown }).userAgentData
})

describe('the desktop download line, by browser', () => {
  const write = () => {
    const dt = fakeTransfer()
    writeFileDrag(dt as unknown as DataTransfer, good(), `/v1/media/${TOKEN}?download=1&fn=report.pdf`)
    return dt
  }

  it('is written in Chrome and Edge, which keep it from the page', () => {
    asBrowser(UA.chrome, CHROMIUM_HINTS)
    expect(write().types.sort()).toEqual(['DownloadURL', FILE_DRAG_TYPE].sort())
    vi.restoreAllMocks()
    delete (window.navigator as { userAgentData?: unknown }).userAgentData
    asBrowser(UA.edge)
    expect(write().types.sort()).toEqual(['DownloadURL', FILE_DRAG_TYPE].sort())
  })

  it('is written in Chrome on a plain http page, where the client hints are missing', () => {
    asBrowser(UA.chrome)
    expect(write().types).toContain('DownloadURL')
  })

  it('is left out in Firefox, which hands it to the page, and the drag holds no address', () => {
    asBrowser(UA.firefox)
    const dt = write()
    expect(dt.types).toEqual([FILE_DRAG_TYPE])
    for (const v of Object.values(dt.store)) expect(v).not.toContain('/v1/media/')
  })

  it('is left out in Safari and in a browser it cannot tell', () => {
    asBrowser(UA.safari)
    expect(write().types).toEqual([FILE_DRAG_TYPE])
    vi.restoreAllMocks()
    asBrowser(UA.unknown)
    expect(write().types).toEqual([FILE_DRAG_TYPE])
  })
})

describe('keepsDownloadUrl', () => {
  it('answers from the client hints when the browser gives them', () => {
    asBrowser(UA.firefox, CHROMIUM_HINTS)
    expect(keepsDownloadUrl()).toBe(true)
    Object.defineProperty(window.navigator, 'userAgentData', { value: { brands: [{ brand: 'Other', version: '1' }] }, configurable: true })
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(UA.chrome)
    expect(keepsDownloadUrl()).toBe(false)
  })

  it('reads the user agent otherwise, and fails closed', () => {
    for (const [ua, want] of [[UA.chrome, true], [UA.edge, true], [UA.firefox, false], [UA.safari, false], [UA.unknown, false], ['', false]] as const) {
      vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(ua)
      expect(keepsDownloadUrl(), ua).toBe(want)
    }
  })
})

describe('the card reference', () => {
  it('reads a media download, and no other address', () => {
    expect(TOKEN).toHaveLength(43)
    expect(fileDragRef(`/v1/media/${TOKEN}?download=1`, 'report.pdf')).toEqual(good())
    expect(fileDragRef(`/v1/shares/s1/media/${TOKEN}`, 'report.pdf')).toBeNull()
    expect(fileDragRef('', 'report.pdf')).toBeNull()
    expect(fileDragRef(`/v1/media/${TOKEN}x?download=1`, 'report.pdf')).toBeNull()
  })

  it('writes our type and the desktop download, and drops the link text', () => {
    asBrowser(UA.chrome, CHROMIUM_HINTS)
    const dt = fakeTransfer({ 'text/uri-list': 'http://x/', 'text/plain': 'http://x/' })
    writeFileDrag(dt as unknown as DataTransfer, good(), `/v1/media/${TOKEN}?download=1&fn=a%3Ab.pdf`)
    expect(dt.types.sort()).toEqual(['DownloadURL', FILE_DRAG_TYPE].sort())
    expect(JSON.parse(dt.store[FILE_DRAG_TYPE])).toEqual(good())
    expect(dt.store.DownloadURL).toBe(
      `application/octet-stream:report.pdf:${window.location.origin}/v1/media/${TOKEN}?download=1&fn=a%3Ab.pdf`)
    expect(dt.effectAllowed).toBe('copy')
  })

  it('keeps a colon in a name out of the download field', () => {
    asBrowser(UA.chrome, CHROMIUM_HINTS)
    const dt = fakeTransfer()
    writeFileDrag(dt as unknown as DataTransfer, { ...good(), name: 'a:b.pdf' }, `/v1/media/${TOKEN}?download=1`)
    expect(dt.store.DownloadURL.split(':')[1]).toBe('a_b.pdf')
  })
})

describe('the drop checks', () => {
  it('passes a drag that carries no card', () => {
    expect(readFileDrag(fakeTransfer({ 'text/plain': 'hi' }) as unknown as DataTransfer)).toBeNull()
    expect(readFileDrag(null)).toBeNull()
  })

  it('takes a reference in its one shape', () => {
    expect(readFileDrag(carrying(good()))).toEqual(good())
  })

  it('refuses a card from another install', () => {
    expect(readFileDrag(carrying({ ...good(), origin: 'https://other.example' }))).toEqual({ refused: REFUSED_OTHER_INSTALL })
  })

  it('refuses every other shape', () => {
    const bad: unknown[] = [
      'not json', '[1]', 'null', JSON.stringify({ ...good(), url: 'https://evil.example/x' }),
      { ...good(), v: 2 }, { ...good(), token: 'short' }, { ...good(), token: `${TOKEN.slice(1)}/` },
      { ...good(), name: '../etc/passwd' }, { ...good(), name: 'a\\b' }, { ...good(), name: '..' },
      { ...good(), name: '' }, { ...good(), name: 'x'.repeat(256) }, { ...good(), name: 'a\u0007b' },
      { ...good(), origin: 5 }, { origin: window.location.origin, token: TOKEN, name: 'a' },
      'x'.repeat(3000),
    ]
    for (const b of bad) expect(readFileDrag(carrying(b)), JSON.stringify(b).slice(0, 60)).toEqual({ refused: REFUSED_SHAPE })
  })
})

describe('the name', () => {
  it('reads both forms of the header', () => {
    expect(dispositionName('attachment; filename="plain name.pdf"')).toBe('plain name.pdf')
    expect(dispositionName("attachment; filename*=utf-8''%CE%B1%CE%BD%CE%B1%CF%86%CE%BF%CF%81%CE%AC.pdf")).toBe('αναφορά.pdf')
    expect(dispositionName('attachment; filename=bare.txt')).toBe('bare.txt')
    expect(dispositionName(null)).toBe('')
  })

  it("keeps the card's name only while it keeps the file's type", () => {
    expect(pickName('Q3 report.pdf', 'out-1.pdf')).toBe('Q3 report.pdf')
    expect(pickName('harmless.txt', 'secret.xlsx')).toBe('secret.xlsx')
    expect(pickName('report.pdf', '')).toBe('report.pdf')
  })
})

describe('the fetch', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => { fetchSpy = vi.spyOn(globalThis, 'fetch') })
  afterEach(() => { fetchSpy.mockRestore() })
  const over = (cap: number) => `Over the ${cap} B upload limit`

  it('builds the address itself and fetches with the sign-in', async () => {
    fetchSpy.mockResolvedValue(new Response(new Uint8Array(10), {
      headers: { 'content-type': 'application/pdf', 'content-length': '10',
                 'content-disposition': 'attachment; filename="out.pdf"' },
    }))
    const file = await fetchDroppedFile(good(), 1000, new AbortController().signal, over)
    expect(fetchSpy.mock.calls[0][0]).toBe(`/v1/media/${TOKEN}?download=1`)
    expect((fetchSpy.mock.calls[0][1] as RequestInit).credentials).toBe('same-origin')
    expect(file.name).toBe('report.pdf')
    expect(file.type).toBe('application/pdf')
    expect(file.size).toBe(10)
  })

  it('refuses a file the person may not read, and says when nothing answered', async () => {
    fetchSpy.mockResolvedValueOnce(new Response('{}', { status: 404 }))
    await expect(fetchDroppedFile(good(), 1000, new AbortController().signal, over)).rejects.toThrow(REFUSED_NOT_YOURS)
    fetchSpy.mockResolvedValueOnce(new Response('{}', { status: 503 }))
    await expect(fetchDroppedFile(good(), 1000, new AbortController().signal, over)).rejects.toThrow(REFUSED_NO_ANSWER)
    fetchSpy.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(fetchDroppedFile(good(), 1000, new AbortController().signal, over)).rejects.toThrow(REFUSED_NO_ANSWER)
  })

  it('stops at the upload cap, with or without a length', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(new Uint8Array(10), { headers: { 'content-length': '5000' } }))
    await expect(fetchDroppedFile(good(), 1000, new AbortController().signal, over)).rejects.toThrow('Over the 1000 B upload limit')
    const body = new ReadableStream({
      start(c) { for (let i = 0; i < 5; i++) c.enqueue(new Uint8Array(400)); c.close() },
    })
    fetchSpy.mockResolvedValueOnce(new Response(body))
    await expect(fetchDroppedFile(good(), 1000, new AbortController().signal, over)).rejects.toThrow('Over the 1000 B upload limit')
  })
})

describe('the card', () => {
  it('starts a drag with its reference', () => {
    const { container } = render(<DisplayFile filename="report.pdf" downloadUrl={`/v1/media/${TOKEN}?download=1`} />)
    const a = container.querySelector('a')!
    expect(a.getAttribute('draggable')).toBe('true')
    expect(a.getAttribute('href')).toBe(`/v1/media/${TOKEN}?download=1&fn=report.pdf`)
    const dt = fakeTransfer()
    fireEvent.dragStart(a, { dataTransfer: dt })
    expect(JSON.parse(dt.store[FILE_DRAG_TYPE])).toEqual(good())
  })

  it('is no drag source for another address', () => {
    const { container } = render(<DisplayFile filename="report.pdf" downloadUrl="/v1/shares/s1/media/abc" />)
    expect(container.querySelector('a')!.hasAttribute('draggable')).toBe(false)
  })
})

describe('the intake', () => {
  beforeEach(() => {
    q.enqueue.mockReset()
    useChatStore.setState({ byChat: {} } as never)
  })

  function files(key: string) {
    return useChatStore.getState().byChat[key]?.pendingFiles ?? []
  }

  it('shows a waiting chip at once, then uploads the fetched file', async () => {
    let resolve: (f: File) => void = () => {}
    const { result } = renderHook(() => useChatAttachments({ agentName: 'ops', draftKey: 'chat-1' }))
    act(() => { result.current.handleAddFetched('report.pdf', () => new Promise((r) => { resolve = r })) })
    expect(files('chat-1')).toHaveLength(1)
    expect(files('chat-1')[0]).toMatchObject({ name: 'report.pdf', uploading: true, queued: true })
    await act(async () => { resolve(new File([new Uint8Array(7)], 'report.pdf', { type: 'application/pdf' })) })
    expect(files('chat-1')[0]).toMatchObject({ size: 7 })
    expect(q.enqueue).toHaveBeenCalledTimes(1)
    expect(q.enqueue.mock.calls[0][0]).toMatchObject({ agent: 'ops', fileId: files('chat-1')[0].id })
  })

  it("puts a refusal on the chip and uploads nothing", async () => {
    const { result } = renderHook(() => useChatAttachments({ agentName: 'ops', draftKey: 'chat-1' }))
    await act(async () => { result.current.handleAddFetched('report.pdf', () => Promise.reject(new Error(REFUSED_NOT_YOURS))) })
    expect(files('chat-1')[0]).toMatchObject({ error: REFUSED_NOT_YOURS, uploading: false })
    expect(q.enqueue).not.toHaveBeenCalled()
  })

  it('ends the fetch when the chip is removed', async () => {
    let seen: AbortSignal | null = null
    const { result } = renderHook(() => useChatAttachments({ agentName: 'ops', draftKey: 'chat-1' }))
    act(() => { result.current.handleAddFetched('report.pdf', (signal) => { seen = signal; return new Promise(() => {}) }) })
    act(() => { result.current.handleRemoveFile(files('chat-1')[0].id) })
    expect(seen!.aborted).toBe(true)
    expect(files('chat-1')).toHaveLength(0)
  })
})
