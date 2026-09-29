import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The text reader must surface a refused read instead of opening an empty
// editor: a file over the inline cap answers 413 with a "download it
// instead" detail, and a save from an empty editor would truncate it.

import * as authApi from '@/api/auth'
import FileEditor from '@/components/FileEditor'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function renderEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <FileEditor agent="dev" path="workspace/big.log" />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  fetchSpy.mockReset()
})

describe('FileEditor on a refused read', () => {
  it('shows the server message and a download link, never an editor', async () => {
    fetchSpy.mockImplementation(async () => ({
      ok: false,
      status: 413,
      statusText: 'Request Entity Too Large',
      json: async () => ({ detail: 'this file is 7.2 MB, larger than the 5 MB preview limit; download it instead' }),
    }) as Response)

    renderEditor()

    await waitFor(() => {
      expect(screen.getByText(/larger than the 5 MB preview limit/)).toBeTruthy()
    })
    const link = screen.getByRole('link', { name: /download/i }) as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('/v1/agents/dev/files/workspace/big.log?download=true&fn=big.log')
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('falls back to the status text when the body carries no detail', async () => {
    fetchSpy.mockImplementation(async () => ({
      ok: false,
      status: 403,
      statusText: 'Forbidden',
      json: async () => { throw new Error('not json') },
    }) as unknown as Response)

    renderEditor()

    await waitFor(() => {
      expect(screen.getByText(/Forbidden/)).toBeTruthy()
    })
    expect(screen.queryByRole('textbox')).toBeNull()
  })
})
