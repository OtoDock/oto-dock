// The transfers popup over the workspace toolbar closes for good when its
// transfers drain (and on an agent change): the next transfer lights the
// icon again but the popup waits for a click.
import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { useEffect, useRef } from 'react'

vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true, writable: true,
    value: (query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }),
  })
})
vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'u', feature_flags: {} } }),
}))
const file = (path: string) => ({ name: path.split('/').pop()!, type: 'file' as const, path, size: 3, modified: '' })
const tree = [{
  name: 'users', type: 'dir' as const, path: 'users', size: 0, modified: '',
  children: [{ name: 'u', type: 'dir' as const, path: 'users/u', size: 0, modified: '',
    children: [{ name: 'workspace', type: 'dir' as const, path: 'users/u/workspace', size: 0, modified: '',
      children: [file('users/u/workspace/a.md')] }] }],
}]
vi.mock('@/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/agents')>()),
  useAgentFiles: () => ({ data: tree }),
  useRecoverBin: () => ({ data: [] }),
}))

import WorkspaceOverlay from '@/components/workspace/WorkspaceOverlay'
import { useWorkspaceState } from '@/hooks/useWorkspaceState'
import { useTransferStore } from '@/store/transferStore'

function Page() {
  const workspace = useWorkspaceState('ag', null)
  const opened = useRef(false)
  useEffect(() => {
    if (opened.current) return
    opened.current = true
    workspace.openWorkspace()
  }, [workspace])
  return <WorkspaceOverlay agent="ag" canManage canEdit state={workspace.state} actions={{ ...workspace }} />
}

const upload = (clientId: string, filename: string) => useTransferStore.getState().beginLocalUpload({
  clientId, agent: 'ag', targetDir: 'users/u/workspace', filename, bytesTotal: 3,
})

describe('the transfers popup', () => {
  it('stays closed once its transfers drained, until the next click', async () => {
    useTransferStore.setState({ byId: {} })
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><Page /></MemoryRouter></QueryClientProvider>)
    await screen.findAllByText('a.md')
    act(() => upload('l1', 'up.md'))
    fireEvent.click(await screen.findByRole('button', { name: 'Transfers: 1 in progress' }))
    expect(screen.getByText(/Uploading…/)).toBeInTheDocument()
    // The upload finished and its row pruned away: the popup hides.
    act(() => useTransferStore.setState({ byId: {} }))
    expect(screen.queryByText(/Uploading…/)).toBeNull()
    // The next upload lights the icon; the popup waits for a click.
    act(() => upload('l2', 'again.md'))
    expect(screen.getByRole('button', { name: 'Transfers: 1 in progress' })).toBeInTheDocument()
    expect(screen.queryByText(/Uploading…/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Transfers: 1 in progress' }))
    expect(screen.getByText(/Uploading…/)).toBeInTheDocument()
    useTransferStore.setState({ byId: {} })
  })
})
