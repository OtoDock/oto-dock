import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { useEffect, useRef, useState } from 'react'

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

let rerender: () => void = () => {}
let isOpen = () => false

// As the chat page does: the hook's whole return handed down as `actions`,
// a new object on every render of the page.
function Page() {
  const workspace = useWorkspaceState('ag', null)
  const [, setTick] = useState(0)
  rerender = () => setTick((t) => t + 1)
  isOpen = () => workspace.state.open
  const opened = useRef(false)
  useEffect(() => {
    if (opened.current) return
    opened.current = true
    workspace.openWorkspace()
  }, [workspace])
  return <WorkspaceOverlay agent="ag" canManage canEdit state={workspace.state} actions={{ ...workspace }} />
}

describe('Escape with a toolbar menu open', () => {
  it('closes the menu, not the workspace, after the page re-rendered meanwhile', async () => {
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><Page /></MemoryRouter></QueryClientProvider>)
    await screen.findAllByText('a.md')
    fireEvent.click(screen.getByTitle('New file'))
    await screen.findByRole('menu')
    act(() => rerender()) // a streamed message re-renders the chat page
    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(isOpen()).toBe(true)
  })
})
