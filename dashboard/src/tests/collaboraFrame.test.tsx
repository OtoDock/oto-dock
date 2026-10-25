import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor, fireEvent } from '@testing-library/react'

// ─── The editor frame takes its WOPI token by a form post, never on its URL
//     (proxy F57): the workspace preview and the frame component. ──────────────

const live = vi.hoisted(() => ({
  reload: null as null | (() => void), banner: false, ref: { current: null as unknown },
}))
vi.mock('@/hooks/useCollaboraLiveReload', () => ({
  useCollaboraLiveReload: ({ reload }: { reload: () => void }) => {
    live.reload = reload
    return { iframeRef: live.ref, reloadAvailable: live.banner, doReload: reload, modifiedRef: { current: false } }
  },
}))
vi.mock('@/components/workspace/FilePreviewPortal', () => ({
  default: ({ children, onReload }: { children: React.ReactNode; onReload?: () => void }) => (
    <div>
      <button title="Reload" onClick={onReload} />
      {children}
    </div>
  ),
}))

const apiFetchMock = vi.fn()
vi.mock('@/api/auth', async (orig) => ({
  ...(await orig<typeof import('@/api/auth')>()),
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

import CollaboraFrame from '@/components/chat/media/CollaboraFrame'
import FilePreviewBody from '@/components/workspace/FilePreviewBody'

const submitMock = vi.fn()

beforeEach(() => {
  live.banner = false
  live.ref.current = null
  apiFetchMock.mockReset()
  submitMock.mockReset()
  vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(submitMock)
})

describe('CollaboraFrame', () => {
  it('posts the token into a blank, named frame', () => {
    render(<CollaboraFrame url="/collabora/cool.html?WOPISrc=x" accessToken="tok" accessTokenTtl={5} />)
    const form = document.querySelector('form')!
    const frame = document.querySelector('iframe')!
    expect(form.getAttribute('method')).toBe('post')
    expect(form.getAttribute('action')).toBe('/collabora/cool.html?WOPISrc=x')
    expect(form.getAttribute('target')).toBe(frame.getAttribute('name'))
    expect((form.querySelector('input[name="access_token"]') as HTMLInputElement).value).toBe('tok')
    expect((form.querySelector('input[name="access_token_ttl"]') as HTMLInputElement).value).toBe('5')
    expect(frame.getAttribute('src')).toBeNull()
    expect(submitMock).toHaveBeenCalledTimes(1)
  })

  it('a URL that still carries its token loads as it is', () => {
    render(<CollaboraFrame url="/collabora/cool.html?WOPISrc=x&access_token=old" />)
    expect(document.querySelector('form')).toBeNull()
    expect(document.querySelector('iframe')!.getAttribute('src')).toContain('access_token=old')
    expect(submitMock).not.toHaveBeenCalled()
  })
})

const header = () => (document.querySelector('input[name="access_token"]') as HTMLInputElement | null)?.value

const doc = { name: 'a.docx', path: 'workspace/a.docx', type: 'file' } as never
const minted = (tok: string) => ({
    ok: true,
  json: async () => ({ wopi_url: `/collabora/cool.html?WOPISrc=${tok}`, access_token: tok, access_token_ttl: 1 }),
})

describe('the workspace document preview', () => {
  it('mints on each load: the first, Reload, a file update', async () => {
    apiFetchMock.mockResolvedValueOnce(minted('ws-1')).mockResolvedValueOnce(minted('ws-2'))
      .mockResolvedValueOnce(minted('ws-3'))
    render(<FilePreviewBody agent="pa" node={doc} canWrite onClose={() => {}} />)
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1))
    expect(String(apiFetchMock.mock.calls[0][0])).toBe('/v1/documents/wopi-url')
    expect(header()).toBe('ws-1')
    expect(document.querySelector('iframe')!.getAttribute('src')).toBeNull()
    fireEvent.click(screen.getByTitle('Reload'))
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(2))
    expect(header()).toBe('ws-2')
    act(() => live.reload!())
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(3))
    expect(header()).toBe('ws-3')
    expect(apiFetchMock).toHaveBeenCalledTimes(3)
  })

  it('a reload posts once, when its mint lands, never the previous token first', async () => {
    let land: (v: unknown) => void = () => {}
    apiFetchMock.mockResolvedValueOnce(minted('ws-1'))
      .mockReturnValueOnce(new Promise((r) => { land = r }))
    render(<FilePreviewBody agent="pa" node={doc} canWrite onClose={() => {}} />)
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByTitle('Reload'))
    await act(async () => { await Promise.resolve() })
    expect(submitMock).toHaveBeenCalledTimes(1)          // the old frame stays, nothing posted
    expect(header()).toBe('ws-1')
    await act(async () => { land(minted('ws-2')) })
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(2))
    expect(header()).toBe('ws-2')
  })

  it('a failed re-mint shows the error with Retry, not a token that may be past its end', async () => {
    apiFetchMock.mockResolvedValueOnce(minted('ws-1'))
      .mockResolvedValueOnce({ ok: false, status: 404, text: async () => JSON.stringify({ detail: 'Preview not found' }) })
      .mockResolvedValueOnce(minted('ws-3'))
    render(<FilePreviewBody agent="pa" node={doc} canWrite onClose={() => {}} />)
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByTitle('Reload'))
    await waitFor(() => expect(screen.getByText('Preview not found')).toBeInTheDocument())
    expect(document.querySelector('iframe')).toBeNull()
    fireEvent.click(screen.getByText('Retry'))
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(2))
    expect(header()).toBe('ws-3')
  })

  it("recovers from Collabora's server-side idle end by minting again, unless its banner is up", async () => {
    apiFetchMock.mockResolvedValueOnce(minted('ws-1')).mockResolvedValueOnce(minted('ws-2'))
    const { unmount } = render(<FilePreviewBody agent="pa" node={doc} canWrite onClose={() => {}} />)
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1))
    const closed = () => act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        data: JSON.stringify({ MessageId: 'Session_Closed', Values: { Reason: 'DocumentIdle' } }),
        source: document.querySelector('iframe')!.contentWindow,
      }))
    })
    const back = () => act(() => {
      document.querySelector('iframe')!.dispatchEvent(new Event('pointerover', { bubbles: true }))
    })
    closed()
    back()
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(2))
    expect(header()).toBe('ws-2')
    unmount()

    live.banner = true
    submitMock.mockReset()
    apiFetchMock.mockReset()
    apiFetchMock.mockResolvedValue(minted('ws-3'))
    render(<FilePreviewBody agent="pa" node={doc} canWrite onClose={() => {}} />)
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1))
    closed()
    back()
    await act(async () => { await Promise.resolve() })
    expect(apiFetchMock).toHaveBeenCalledTimes(1)
  })
})

