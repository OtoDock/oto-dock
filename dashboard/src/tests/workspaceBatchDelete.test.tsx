// A batch delete in the workspace refreshes the listing, the open file
// contents and the recover bin once, after every delete settled, not once
// per item (each refresh refetches the whole tree).

import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { useEffect } from 'react'

// WorkspaceOverlay reads matchMedia once at import; jsdom has none.
vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true, writable: true,
    value: (query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }),
  })
})

// jsdom has no ResizeObserver (the scope chips' fade edges).
vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })

const fetchMock = vi.hoisted(() => vi.fn())
vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  apiFetch: (...args: unknown[]) => fetchMock(...args),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'u', feature_flags: {} } }),
}))

const file = (path: string) => ({
  name: path.split('/').pop()!, type: 'file' as const, path, size: 3, modified: '',
})
const tree = [{
  name: 'users', type: 'dir' as const, path: 'users', size: 0, modified: '',
  children: [{
    name: 'u', type: 'dir' as const, path: 'users/u', size: 0, modified: '',
    children: [{
      name: 'workspace', type: 'dir' as const, path: 'users/u/workspace', size: 0, modified: '',
      children: ['a.md', 'b.md', 'c.md'].map((n) => file(`users/u/workspace/${n}`)),
    }],
  }],
}]
vi.mock('@/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/agents')>()),
  useAgentFiles: () => ({ data: tree }),
  useRecoverBin: () => ({ data: [] }),
}))

import WorkspaceOverlay from '@/components/workspace/WorkspaceOverlay'
import { useWorkspaceState } from '@/hooks/useWorkspaceState'

function Workspace() {
  const { state, ...actions } = useWorkspaceState('ag', null)
  const { openWorkspace, setSelection } = actions
  useEffect(() => {
    openWorkspace()
  }, [openWorkspace])
  useEffect(() => {
    if (state.open && state.selected.length === 0) {
      setSelection(['users/u/workspace/a.md', 'users/u/workspace/b.md'], 'users/u/workspace/b.md')
    }
  }, [state.open, state.selected.length, setSelection])
  return <WorkspaceOverlay agent="ag" canManage canEdit state={state} actions={actions} />
}

describe('the workspace batch delete', () => {
  it('deletes each item without a refresh, then refreshes once', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(new Response('{}', { status: 200 })))
    const qc = new QueryClient()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><Workspace /></MemoryRouter>
      </QueryClientProvider>,
    )
    await screen.findAllByText('a.md')
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Delete' })
    })
    invalidate.mockClear()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull())
    const keys = invalidate.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
    expect(keys.filter((k) => k === '["agent-files","ag"]')).toHaveLength(1)
    expect(keys.filter((k) => k === '["agent-file-content","ag"]')).toHaveLength(1)
    expect(keys.filter((k) => k === '["agent-recover-bin","ag"]')).toHaveLength(1)
  })
})
