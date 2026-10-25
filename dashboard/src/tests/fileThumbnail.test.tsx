// The thumbnail URL of a workspace tile: a mounted tile's URL stays valid
// while a sibling tile of the same file unmounts, every URL made is revoked
// once the last tile is gone, and StrictMode's double mount never hands a
// tile a revoked URL.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode, type ReactNode } from 'react'
import { useFileThumbnail } from '@/hooks/useFileThumbnail'
import * as authApi from '@/api/auth'

const made: string[] = []
const revoked: string[] = []

beforeEach(() => {
  made.length = 0
  revoked.length = 0
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: vi.fn(() => { const u = `blob:${made.length + 1}`; made.push(u); return u }),
    revokeObjectURL: vi.fn((u: string) => { revoked.push(u) }),
  }))
  vi.spyOn(authApi, 'apiFetch').mockImplementation(async () =>
    ({ ok: true, blob: async () => new Blob(['x']) }) as Response)
})
afterEach(() => { vi.restoreAllMocks() })

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

describe('useFileThumbnail', () => {
  it('keeps a sibling usable and revokes everything after the last tile leaves', async () => {
    const qc = client()
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const a = renderHook(() => useFileThumbnail('agent', 'pics/a.png', true), { wrapper })
    const b = renderHook(() => useFileThumbnail('agent', 'pics/a.png', true), { wrapper })
    await waitFor(() => expect(a.result.current.src).toMatch(/^blob:/))
    await waitFor(() => expect(b.result.current.src).toMatch(/^blob:/))
    a.unmount()
    expect(revoked).not.toContain(b.result.current.src)
    b.unmount()
    expect(new Set(revoked)).toEqual(new Set(made))
  })

  it('never hands a revoked URL to a tile under StrictMode', async () => {
    const qc = client()
    const wrapper = ({ children }: { children: ReactNode }) => (
      <StrictMode><QueryClientProvider client={qc}>{children}</QueryClientProvider></StrictMode>
    )
    const h = renderHook(() => useFileThumbnail('agent', 'pics/a.png', true), { wrapper })
    await waitFor(() => expect(h.result.current.src).toMatch(/^blob:/))
    await new Promise((r) => setTimeout(r, 0))
    expect(revoked).not.toContain(h.result.current.src)
  })
})
