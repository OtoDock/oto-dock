import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

// ─── Frozen "previous version" preview blocks: view-only render of the
//     block's OWN push-time snapshot, minted at swap time; any failure
//     degrades to the chip (never a broken iframe); dismissal is scoped
//     to the one instance. ──────────────────────────────────────────────────

vi.mock('@/hooks/useCollaboraLiveReload', () => ({
  useCollaboraLiveReload: () => ({
    iframeRef: { current: null },
    reloadAvailable: false,
    doReload: () => {},
    modifiedRef: { current: false },
  }),
}))

import DocumentPreview from '@/components/chat/media/DocumentPreview'

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})

function renderPreview(props: Partial<Parameters<typeof DocumentPreview>[0]> = {}) {
  return render(
    <DocumentPreview
      wopiUrl={`${window.location.origin}/collabora/browser/dist/cool.html?WOPISrc=x&_t=1`}
      filename="report.xlsx"
      fileId="f1"
      downloadUrl="/v1/media/tok"
      chatId="chat-1"
      {...props}
    />,
  )
}

describe('DocumentPreview — chain modes', () => {
  it('chip mode renders the reference chip, no iframe', () => {
    renderPreview({ mode: 'chip' })
    expect(screen.getByText(/preview moved to latest turn/)).toBeInTheDocument()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('frozen without a snapshot degrades to the chip', () => {
    renderPreview({ mode: 'frozen' })
    expect(screen.getByText(/preview moved to latest turn/)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('frozen mints a view URL for ITS OWN snapshot and renders it', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ wopi_url: `${window.location.origin}/collabora/cool.html?snap=own` }),
    })
    renderPreview({ mode: 'frozen', snapshotId: 'snap-own' })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())
    expect(fetchMock.mock.calls[0][0]).toContain('/v1/documents/snapshot-wopi-url')
    expect(fetchMock.mock.calls[0][0]).toContain('snapshot_id=snap-own')
    expect(document.querySelector('iframe')!.getAttribute('src')).toContain('snap=own')
    expect(screen.getByText('Previous version')).toBeInTheDocument()
    // View-only affordances: no refresh, no download on a pinned old version.
    expect(screen.queryByTitle('Refresh preview')).toBeNull()
    expect(screen.queryByTitle('Download')).toBeNull()
    expect(screen.getByTitle('Go to the latest preview')).toBeInTheDocument()
  })

  it('a pruned snapshot (404) degrades to the chip at runtime', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 404 })
    renderPreview({ mode: 'frozen', snapshotId: 'snap-gone' })
    await waitFor(() =>
      expect(screen.getByText(/preview moved to latest turn/)).toBeInTheDocument(),
    )
    expect(document.querySelector('iframe')).toBeNull()
  })
})

describe('DocumentPreview — stale live re-mint', () => {
  // Persisted preview URLs carry a 4h token; blocks rendered from history
  // mint a fresh URL at mount instead of loading a token Collabora rejects
  // as "session expired". A fresh push keeps its streamed URL untouched.

  it('a fresh block (recent generation) renders the persisted URL, no mint', () => {
    renderPreview({ mode: 'live', generation: Date.now() - 5_000 })
    expect(fetchMock).not.toHaveBeenCalled()
    expect(document.querySelector('iframe')!.getAttribute('src')).toContain('WOPISrc=x')
  })

  it('a stale block mints through preview-wopi-url and renders the fresh URL', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ wopi_url: `${window.location.origin}/collabora/cool.html?fresh=1` }),
    })
    renderPreview({ mode: 'live', generation: Date.now() - 60 * 60 * 1000 })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())
    expect(fetchMock.mock.calls[0][0]).toContain('/v1/documents/preview-wopi-url')
    expect(fetchMock.mock.calls[0][0]).toContain('chat_id=chat-1')
    expect(fetchMock.mock.calls[0][0]).toContain('file_id=f1')
    expect(document.querySelector('iframe')!.getAttribute('src')).toContain('fresh=1')
  })

  it('a block with no generation (older history) also mints fresh', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ wopi_url: `${window.location.origin}/collabora/cool.html?fresh=2` }),
    })
    renderPreview({ mode: 'live' })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())
    expect(document.querySelector('iframe')!.getAttribute('src')).toContain('fresh=2')
  })

  it('a failed mint falls back to the persisted URL (never a blank block)', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 404 })
    renderPreview({ mode: 'live', generation: Date.now() - 60 * 60 * 1000 })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())
    expect(document.querySelector('iframe')!.getAttribute('src')).toContain('WOPISrc=x')
  })
})

describe('DocumentPreview — dismissal scoping', () => {
  it('the live block dismisses the whole file trail (no scope params)', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) })
    const onDismiss = vi.fn()
    renderPreview({ mode: 'live', snapshotId: 's-live', onDismiss })
    fireEvent.click(screen.getByTitle('Close preview'))
    await waitFor(() => expect(onDismiss).toHaveBeenCalledWith('file'))
    const patchCall = fetchMock.mock.calls.find(c => c[1]?.method === 'PATCH')!
    expect(patchCall[0]).toBe('/v1/chats/chat-1/dismiss-preview/f1')
  })

  it('a frozen block dismisses only itself (snapshot-scoped)', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ wopi_url: `${window.location.origin}/collabora/cool.html?snap=own` }),
    })
    const onDismiss = vi.fn()
    renderPreview({ mode: 'frozen', snapshotId: 's-old', onDismiss })
    await waitFor(() => expect(document.querySelector('iframe')).toBeTruthy())
    fireEvent.click(screen.getByTitle('Close this previous version'))
    await waitFor(() => expect(onDismiss).toHaveBeenCalledWith('instance'))
    const patchCall = fetchMock.mock.calls.find(c => c[1]?.method === 'PATCH')!
    expect(patchCall[0]).toBe('/v1/chats/chat-1/dismiss-preview/f1?snapshot_id=s-old')
  })
})
