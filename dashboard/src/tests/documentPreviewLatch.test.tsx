/**
 * Lazy-mount latch: a preview mounts its Collabora iframe (and fires its
 * WOPI-token mints) only once it nears the viewport. Embedded (PiP) hosts
 * latch immediately; fullscreen and refresh force-latch; a not-yet-latched
 * block fires NEITHER the stale-live mint nor the frozen-snapshot mint.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'

vi.mock('@/hooks/useCollaboraLiveReload', () => ({
  useCollaboraLiveReload: () => ({
    iframeRef: { current: null },
    reloadAvailable: false,
    doReload: () => {},
    modifiedRef: { current: false },
  }),
}))

import DocumentPreview from '@/components/chat/media/DocumentPreview'

// Controllable IntersectionObserver — tests trigger intersection by hand.
const observers: FakeIO[] = []
class FakeIO {
  cb: IntersectionObserverCallback
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb
    observers.push(this)
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  intersect() {
    this.cb(
      [{ isIntersecting: true, intersectionRatio: 1 } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    )
  }
}

const fetchMock = vi.fn()
const STALE_GENERATION = Date.now() - 20 * 60 * 1000

beforeEach(() => {
  observers.length = 0
  fetchMock.mockReset()
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({ wopi_url: `${window.location.origin}/collabora/minted?WOPISrc=x` }),
  })
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('IntersectionObserver', FakeIO)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderPreview(props: Partial<Parameters<typeof DocumentPreview>[0]> = {}) {
  return render(
    <DocumentPreview
      wopiUrl={`${window.location.origin}/collabora/browser/dist/cool.html?WOPISrc=x&_t=1`}
      filename="report.xlsx"
      fileId="f1"
      downloadUrl="/v1/media/tok"
      chatId="chat-1"
      generation={Date.now()}
      {...props}
    />,
  )
}

function intersectAll() {
  act(() => {
    observers.forEach((o) => o.intersect())
  })
}

describe('DocumentPreview — lazy-mount latch', () => {
  it('mounts the iframe only after the latch observer fires', () => {
    renderPreview()
    expect(document.querySelector('iframe')).toBeNull()
    intersectAll()
    expect(document.querySelector('iframe')).not.toBeNull()
  })

  it('a stale live block does not mint until latch', async () => {
    renderPreview({ generation: STALE_GENERATION })
    expect(fetchMock).not.toHaveBeenCalled()
    expect(document.querySelector('iframe')).toBeNull()
    intersectAll()
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(String(fetchMock.mock.calls[0][0])).toContain('preview-wopi-url')
    await waitFor(() =>
      expect(document.querySelector('iframe')?.getAttribute('src')).toContain('minted'),
    )
  })

  it('a frozen block does not mint its snapshot until latch', async () => {
    renderPreview({ mode: 'frozen', snapshotId: 'snap-1', generation: STALE_GENERATION })
    expect(fetchMock).not.toHaveBeenCalled()
    intersectAll()
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const urls = fetchMock.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('snapshot-wopi-url'))).toBe(true)
  })

  it('embedded (PiP) hosts latch immediately with no observer', () => {
    renderPreview({ embedded: true })
    expect(observers.length).toBe(0)
    expect(document.querySelector('iframe')).not.toBeNull()
  })

  it('the fullscreen button force-latches', () => {
    renderPreview()
    expect(document.querySelector('iframe')).toBeNull()
    fireEvent.click(screen.getByTitle('Fullscreen'))
    expect(document.querySelectorAll('iframe').length).toBeGreaterThan(0)
  })

  it('the refresh button force-latches', () => {
    renderPreview()
    expect(document.querySelector('iframe')).toBeNull()
    fireEvent.click(screen.getByTitle('Refresh preview'))
    expect(document.querySelector('iframe')).not.toBeNull()
  })
})
