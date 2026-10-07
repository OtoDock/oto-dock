import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

import type { PinnedApp } from '@/api/apps'

// The page composes header + menu + frame around one row; the frame, the
// socket and the auth context are stubbed.
const frameProps = vi.hoisted(() => ({ current: null as any }))
vi.mock('@/components/apps/AppFrame', () => ({
  default: (props: any) => {
    frameProps.current = props
    return <div data-testid="app-frame">{props.app.slug}</div>
  },
}))
// The page owns its socket: it must connect on mount and disconnect on
// unmount (the hook never connects by itself).
const wsSpy = vi.hoisted(() => ({ connect: vi.fn(), disconnect: vi.fn() }))
vi.mock('@/hooks/useDashboardWs', () => ({ useDashboardWs: () => wsSpy }))
const appRow = vi.hoisted(() => ({ current: null as any, loading: false }))
const rollbackMutate = vi.hoisted(() => vi.fn())
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  useApp: () => ({ data: appRow.current, isLoading: appRow.loading }),
  useRollbackApp: () => ({ mutate: rollbackMutate, isPending: false }),
}))
const authUser = vi.hoisted(() => ({ current: { role: 'member', agents: ['dev'] } as any }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: authUser.current }) }))
const hideShare = vi.hoisted(() => vi.fn())
const patchShare = vi.hoisted(() => vi.fn())
vi.mock('@/api/shares', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/shares')>()),
  useHideShare: () => ({ mutate: hideShare }),
  usePatchShare: () => ({ mutate: patchShare }),
}))

import AppPage from '@/pages/apps/AppPage'

function mkApp(over: Partial<PinnedApp> = {}): PinnedApp & { agent: string; chat_id: string } {
  return {
    id: 'app-1', slug: 'brief', title: 'Morning brief', scope: 'shared', position: 0,
    rel_path: 'workspace/apps/brief.html', updated_at: '', actions: [],
    actions_sig: '', actions_approved: true, approval_stale: false,
    can_approve: true, can_manage: true, hidden_for_me: false,
    agent: 'dev', chat_id: '', ...over,
  }
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname}|{JSON.stringify(loc.state)}</div>
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/app-1']}>
        <Routes>
          <Route path="/apps/:appId" element={<><AppPage /><LocationProbe /></>} />
          <Route path="/chat/:name" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AppPage', () => {
  it('a render names the copy to load and opens no socket; ?preview=1 shows the working copy to a manager', () => {
    wsSpy.connect.mockClear()
    const sha = 'a'.repeat(64)
    appRow.current = mkApp({ kind: 'folder', release_sha: 'b'.repeat(64) })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/apps/app-1?render=${sha}`]}>
          <Routes><Route path="/apps/:appId" element={<AppPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(frameProps.current.renderSha).toBe(sha)
    expect(wsSpy.connect).not.toHaveBeenCalled()
    unmount()
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/app-1?preview=1']}>
          <Routes><Route path="/apps/:appId" element={<AppPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(frameProps.current.preview).toBe(true)
    expect(frameProps.current.renderSha).toBe('')
    expect(wsSpy.connect).toHaveBeenCalled()
  })

  it('Roll back asks first on the page too, and the notice names the outcome', () => {
    rollbackMutate.mockClear()
    appRow.current = mkApp({ kind: 'folder', release: 2, has_previous_release: true })
    renderPage()
    fireEvent.click(screen.getByLabelText('Options for Morning brief'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Roll back' }))
    expect(rollbackMutate).not.toHaveBeenCalled()
    expect(screen.getByTestId('app-rollback-confirm')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Roll back' }))
    expect(rollbackMutate).toHaveBeenCalledTimes(1)
    act(() => { rollbackMutate.mock.calls[0][1].onSuccess({ release: 1, screens: 1, db_restored: false }) })
    expect(screen.getByTestId('app-rollback-notice').textContent)
      .toContain('No copy from before release 2 exists')
  })

  it('renders the app full screen with its title, back link and menu', () => {
    appRow.current = mkApp()
    renderPage()
    expect(screen.getByRole('heading', { name: 'Morning brief' })).toBeTruthy()
    expect(screen.getByTestId('app-frame').textContent).toBe('brief')
    fireEvent.click(screen.getByLabelText('Options for Morning brief'))
    // Already full screen: no entry for it; unpin and hide are offered.
    expect(screen.queryByRole('menuitem', { name: 'Open full screen' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Hide for me' })).toBeTruthy()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unpin for everyone' }))
    expect(screen.getByRole('button', { name: 'Unpin for everyone' })).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Back to the agent'))
    expect(screen.getByTestId('loc').textContent).toContain('/chat/dev')
  })

  it('a shared app that runs a server says Stop app for everyone, and the confirm says what is kept', () => {
    authUser.current = { role: 'member', agents: ['dev'] }
    appRow.current = mkApp({ kind: 'folder', has_server: true })
    renderPage()
    fireEvent.click(screen.getByLabelText('Options for Morning brief'))
    expect(screen.queryByRole('menuitem', { name: /Unpin/ })).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Stop app for everyone' }))
    expect(screen.getByTestId('app-unpin-confirm').textContent).toContain(
      'Stop “Morning brief” for everyone? Stops the app and keeps its data. Pin it again to start it.')
    expect(screen.getByRole('button', { name: 'Stop app for everyone' })).toBeTruthy()
  })

  it('a missing or denied app is the same absence', () => {
    appRow.current = null
    renderPage()
    expect(screen.getByText('This app is not available.')).toBeTruthy()
    expect(screen.queryByTestId('app-frame')).toBeNull()
  })

  it('a send_prompt button hands the action to the agent home as a new chat', async () => {
    appRow.current = mkApp()
    renderPage()
    let r: { status: string } = { status: '' }
    await act(async () => {
      r = await frameProps.current.onSendPrompt(
        appRow.current, { id: 'ask', label: 'Ask', prompt: 'hi' }, { m: 1 })
    })
    expect(r.status).toBe('sent')
    const text = screen.getByTestId('loc').textContent!
    expect(text.startsWith('/chat/dev|')).toBe(true)
    expect(JSON.parse(text.split('|')[1]).pendingAppAction.action.id).toBe('ask')
  })

  it('non-members get no send_prompt route (acks unavailable in the frame)', () => {
    appRow.current = mkApp({ agent: 'ceo' })
    authUser.current = { role: 'member', agents: ['dev'] }
    renderPage()
    expect(frameProps.current.onSendPrompt).toBeUndefined()
  })
})

describe('AppPage socket', () => {
  it('connects its own dashboard socket on mount and disconnects on unmount', () => {
    wsSpy.connect.mockClear()
    wsSpy.disconnect.mockClear()
    appRow.current = mkApp()
    const { unmount } = renderPage()
    expect(wsSpy.connect).toHaveBeenCalledTimes(1)
    expect(wsSpy.disconnect).not.toHaveBeenCalled()
    unmount()
    expect(wsSpy.disconnect).toHaveBeenCalledTimes(1)
  })
})

describe('AppPage — an app a share placed for this viewer (SHARING.md)', () => {
  it('a person\'s own placement goes back to its agent and offers Remove for me, which revokes their share behind a confirm', () => {
    hideShare.mockClear()
    patchShare.mockClear()
    authUser.current = { role: 'member', agents: ['lite'] }
    appRow.current = mkApp({
      agent: 'ops', can_manage: false, can_approve: false,
      placement: { kind: 'person', share_id: 'sh-9', from_agent: 'ops', from_agent_name: 'Ops Desk', agent: 'lite', role_cap: 'viewer', shared_by: 'u-nora', shared_by_name: 'Nora', can_remove: false },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/app-1']}>
          <Routes>
            <Route path="/apps/:appId" element={<><AppPage /><LocationProbe /></>} />
            <Route path="/chat/:name" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByText('shared with you')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Options for Morning brief'))
    expect(screen.queryByRole('menuitem', { name: 'Hide for me' })).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove for me' }))
    expect(patchShare).not.toHaveBeenCalled()
    expect(screen.getByTestId('app-remove-confirm').textContent).toContain(
      'Remove “Morning brief” from your apps? Nora can share it again.')
    fireEvent.click(screen.getByRole('button', { name: 'Remove for me' }))
    expect(patchShare).toHaveBeenCalledWith({ id: 'sh-9', revoke: true })
    expect(hideShare).not.toHaveBeenCalled()
    expect(screen.getByTestId('loc').textContent).toContain('/chat/lite')
  })

  it('the page\'s Remove for me reads whole without the sharer\'s name', () => {
    authUser.current = { role: 'member', agents: ['lite'] }
    appRow.current = mkApp({
      agent: 'ops', can_manage: false, can_approve: false,
      placement: { kind: 'person', share_id: 'sh-9', from_agent: 'ops', from_agent_name: 'Ops Desk', agent: 'lite', role_cap: 'viewer', shared_by: '', shared_by_name: '', can_remove: false },
    })
    renderPage()
    fireEvent.click(screen.getByLabelText('Options for Morning brief'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove for me' }))
    expect(screen.getByTestId('app-remove-confirm').textContent).toContain(
      'Remove “Morning brief” from your apps? The person who shared it can share it again.')
  })

  it('goes back to the agent it sits in for a non-member, labels it, and hides a team placement through the share', () => {
    hideShare.mockClear()
    authUser.current = { role: 'member', agents: ['lite'] }
    appRow.current = mkApp({
      agent: 'ops', can_manage: false, can_approve: false,
      placement: { kind: 'agent', share_id: 'sh-9', from_agent: 'ops', from_agent_name: 'Ops Desk', agent: 'lite', role_cap: 'viewer', shared_by: '', shared_by_name: '', can_remove: false },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/app-1']}>
          <Routes>
            <Route path="/apps/:appId" element={<><AppPage /><LocationProbe /></>} />
            <Route path="/chat/:name" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByText('shared with you')).toBeTruthy()
    expect(frameProps.current.onSendPrompt).toBeUndefined()
    fireEvent.click(screen.getByLabelText('Options for Morning brief'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Hide for me' }))
    expect(hideShare).toHaveBeenCalledWith({ id: 'sh-9', agent: 'lite' })
    expect(screen.getByTestId('loc').textContent).toContain('/chat/lite')
  })

  it('a pending person share hides through its own share id and goes home', () => {
    hideShare.mockClear()
    authUser.current = { role: 'member', agents: [] }
    appRow.current = mkApp({ agent: 'ops', can_manage: false, granted: true, share_id: 'sh-3' })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/app-1']}>
          <Routes>
            <Route path="/apps/:appId" element={<><AppPage /><LocationProbe /></>} />
            <Route path="/" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByLabelText('Back to the agent'))
    expect(screen.getByTestId('loc').textContent?.startsWith('/|')).toBe(true)
  })
})
