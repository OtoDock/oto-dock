import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

// ─── The editor frame takes its WOPI token by a form post, never on its URL
//     (proxy F57): the workspace preview and the frame component. ──────────────

vi.mock('@/hooks/useCollaboraLiveReload', () => ({
  useCollaboraLiveReload: () => ({ iframeRef: { current: null }, reloadAvailable: false, doReload: () => {} }),
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

describe('the workspace document preview', () => {
  it('posts the minted token and posts it again on reload', async () => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ wopi_url: '/collabora/cool.html?WOPISrc=w', access_token: 'ws-tok', access_token_ttl: 1 }),
    })
    render(
      <FilePreviewBody
        agent="pa"
        node={{ name: 'a.docx', path: 'workspace/a.docx', type: 'file' } as never}
        canWrite
        onClose={() => {}}
      />,
    )
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1))
    expect(String(apiFetchMock.mock.calls[0][0])).toBe('/v1/documents/wopi-url')
    expect((document.querySelector('input[name="access_token"]') as HTMLInputElement).value).toBe('ws-tok')
    expect(document.querySelector('iframe')!.getAttribute('src')).toBeNull()
    fireEvent.click(screen.getByTitle('Reload'))
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(2))
  })
})
