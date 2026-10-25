// A batch delete in the workspace refreshes the listing, the open file
// contents and the recover bin once, after every delete settled, not once
// per item (each refresh refetches the whole tree).

import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { useEffect } from 'react'
import { personClick, renderWithDialogs } from './dialogsHarness'

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
import RecoverBinModal from '@/components/workspace/RecoverBinModal'
import { useWorkspaceState } from '@/hooks/useWorkspaceState'
import { onAppToast, type AppToast } from '@/lib/appToasts'

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

const refusal = (status: number, detail: string) =>
  Promise.resolve(new Response(JSON.stringify({ detail }), { status }))

/** The toasts raised while `run` runs. */
async function toastsOf(run: () => Promise<void>): Promise<AppToast[]> {
  const seen: AppToast[] = []
  const stop = onAppToast((t) => seen.push(t))
  try { await run() } finally { stop() }
  return seen
}

function renderWorkspace() {
  renderWithDialogs(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><Workspace /></MemoryRouter>
    </QueryClientProvider>,
  )
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
    personClick(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull())
    const keys = invalidate.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
    expect(keys.filter((k) => k === '["agent-files","ag"]')).toHaveLength(1)
    expect(keys.filter((k) => k === '["agent-file-content","ag"]')).toHaveLength(1)
    expect(keys.filter((k) => k === '["agent-recover-bin","ag"]')).toHaveLength(1)
  })
})

describe('a refused workspace action says so', () => {
  it('a delete refused for every item shows one toast with the reason', async () => {
    fetchMock.mockImplementation(() => refusal(403, 'You cannot delete files here.'))
    renderWorkspace()
    await screen.findAllByText('a.md')
    const toasts = await toastsOf(async () => {
      await act(async () => { fireEvent.keyDown(document, { key: 'Delete' }) })
      personClick(await screen.findByRole('button', { name: 'Delete' }))
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull())
    })
    expect(toasts).toEqual([{ title: 'The items were not deleted', body: 'You cannot delete files here.', severity: 'warning' }])
  })

  it('a delete refused for one of two names how many', async () => {
    let n = 0
    fetchMock.mockImplementation(() => (++n === 1
      ? Promise.resolve(new Response('{}', { status: 200 }))
      : refusal(409, 'The folder is not empty.')))
    renderWorkspace()
    await screen.findAllByText('a.md')
    const toasts = await toastsOf(async () => {
      await act(async () => { fireEvent.keyDown(document, { key: 'Delete' }) })
      personClick(await screen.findByRole('button', { name: 'Delete' }))
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull())
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([['1 of 2 items was not deleted', 'The folder is not empty.']])
  })

  it('a new folder that is refused says why', async () => {
    fetchMock.mockImplementation(() => refusal(409, 'Something with that name already exists.'))
    renderWorkspace()
    await screen.findAllByText('a.md')
    const toasts = await toastsOf(async () => {
      personClick(screen.getByTitle('New folder'))
      fireEvent.change(await screen.findByLabelText('Folder name'), { target: { value: 'notes' } })
      personClick(screen.getByRole('button', { name: 'Create' }))
      await waitFor(() => expect(fetchMock).toHaveBeenCalled())
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Create' })).toBeNull())
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([['The folder was not created', 'Something with that name already exists.']])
  })

  it('a new file that is refused says why', async () => {
    fetchMock.mockImplementation(() => refusal(403, 'You cannot write here.'))
    renderWorkspace()
    await screen.findAllByText('a.md')
    const toasts = await toastsOf(async () => {
      personClick(screen.getByTitle('New file'))
      personClick((await screen.findAllByRole('menuitem'))[0])
      fireEvent.change(await screen.findByRole('textbox', { name: /Name of the new/ }), { target: { value: 'todo' } })
      personClick(screen.getByRole('button', { name: 'Create' }))
      await waitFor(() => expect(fetchMock).toHaveBeenCalled())
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Create' })).toBeNull())
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([['The file was not created', 'You cannot write here.']])
  })

  it('a download that is refused says why', async () => {
    fetchMock.mockImplementation(() => refusal(507, 'The server’s disk is full.'))
    renderWorkspace()
    const [row] = await screen.findAllByText('b.md')
    const toasts = await toastsOf(async () => {
      fireEvent.contextMenu(row)
      // The menu is measured before it shows (visibility hidden in jsdom).
      personClick((await screen.findByText(/Download 2 items/)).closest('button')!)
      await waitFor(() => expect(fetchMock).toHaveBeenCalled())
      await new Promise((r) => setTimeout(r, 50))
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([['The download was not prepared', 'The server’s disk is full.']])
  })
})

describe('the recover bin says when it was refused', () => {
  const entries = [{ entry_id: 'e1', rel_path: 'users/u/workspace/a.md', original_name: 'a.md',
    reason: 'deleted' as const, scope: 'user' as const, size: 3, binned_at: '2026-10-18T00:00:00Z' }]

  function renderBin(onClose = vi.fn()) {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <RecoverBinModal agent="ag" entries={entries} onClose={onClose} />
      </QueryClientProvider>,
    )
    return onClose
  }

  it('a restore that is refused shows its reason and keeps the list', async () => {
    fetchMock.mockImplementation(() => refusal(403, 'Only a manager can restore these.'))
    renderBin()
    const toasts = await toastsOf(async () => {
      personClick(screen.getByRole('button', { name: 'Restore 1 selected' }))
      await waitFor(() => expect(fetchMock).toHaveBeenCalled())
      await waitFor(() => expect(screen.getByRole('button', { name: 'Restore 1 selected' })).toBeEnabled())
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([['The items were not restored', 'Only a manager can restore these.']])
  })

  it('a discard that is refused shows its reason and stays open', async () => {
    fetchMock.mockImplementation(() => refusal(403, 'Only a manager can discard these.'))
    const onClose = renderBin()
    const toasts = await toastsOf(async () => {
      personClick(screen.getByRole('button', { name: 'Discard' }))
      personClick(await screen.findByRole('button', { name: 'Discard 1 permanently?' }))
      await waitFor(() => expect(fetchMock).toHaveBeenCalled())
      await waitFor(() => expect(screen.getByRole('button', { name: /Discard/ })).toBeEnabled())
    })
    expect(toasts.map((t) => [t.title, t.body])).toEqual([['The items were not discarded', 'Only a manager can discard these.']])
    expect(onClose).not.toHaveBeenCalled()
  })
})
