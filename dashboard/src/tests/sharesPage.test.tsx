import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

const state = vi.hoisted(() => ({
  shares: [] as unknown[],
  truncated: {} as Record<string, boolean>,
  patch: vi.fn(),
  filters: [] as unknown[],
  placeholder: false,
}))
vi.mock('@/api/shares', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/shares')>()),
  useAdminShares: (filters: unknown) => {
    state.filters.push(filters)
    return { data: { shares: state.shares, truncated: state.truncated }, isLoading: false, isPlaceholderData: state.placeholder, error: null }
  },
  usePatchShare: () => ({ mutate: state.patch, isPending: false }),
}))
vi.mock('@/api/agents', () => ({
  useAgents: () => ({ data: [{ name: 'dev', display_name: 'Dev Agent' }, { name: 'ops', display_name: 'Ops Desk' }] }),
}))

import SharesPage from '@/pages/admin/SharesPage'

const base = {
  target_kind: 'app', target_id: 'a', state: 'active', grantee: null, to_agent: null, to_department: null,
  role_cap: 'viewer', decision: '', decided_by: '', decided_by_name: '', placed_agent: '',
  hidden_by_grantee: false, public: false, allow_actions: false, created_by: 'u1', created_by_name: 'Nora',
  created_at: '', expires_at: null, last_access_at: null, access_count: 0, title: 'Schedule', agent: 'dev',
  standing: 'live',
}
const link = (over: Record<string, unknown>) => ({ ...base, scope: 'external', grantee_kind: '', ...over })
const internal = (over: Record<string, unknown>) => ({ ...base, scope: 'internal', ...over })

beforeEach(() => {
  state.shares = []
  state.truncated = {}
  state.filters = []
  state.placeholder = false
  state.patch.mockReset()
})

describe('Admin → Shares', () => {
  it('lists every external link with its protection and revokes one at once', () => {
    const inTenDays = new Date(Date.now() + 10 * 86_400_000 - 60_000).toISOString()
    state.shares = [
      link({ id: 'l1', public: true }),
      link({ id: 'l2', allow_actions: true, expires_at: inTenDays }),
    ]
    render(<SharesPage />)
    expect(screen.getAllByText('Schedule').length).toBe(2)
    // The agent name sits in the filter and in the rows.
    expect(screen.getAllByText('Dev Agent').length).toBe(3)
    expect(screen.getByText('public')).toBeTruthy()
    // Never opened, never expires — and an expiry is said as a time to come.
    expect(screen.getAllByText('never').length).toBe(3)
    expect(screen.getByText('expires in 10 days')).toBeTruthy()
    expect(screen.getByText('No shares to people, agents or departments.')).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: 'Revoke' })[0])
    expect(state.patch).toHaveBeenCalledWith({ id: 'l1', revoke: true }, expect.anything())
  })

  it('lists the shares to people, agents and departments with their recipient and standing', () => {
    state.shares = [
      internal({ id: 'p1', grantee_kind: 'person', grantee: { sub: 's1', name: 'Ana', username: 'ana' }, decision: 'pending', standing: 'waiting' }),
      internal({ id: 'p2', grantee_kind: 'person', grantee: { sub: 's1', name: 'Ana', username: 'ana' }, decision: 'declined', decided_by_name: 'Ana', standing: 'declined', title: 'Board' }),
      internal({ id: 'a1', grantee_kind: 'agent', to_agent: { slug: 'ops', name: 'Ops Desk' }, decision: 'accepted', role_cap: 'editor', title: 'Register' }),
      internal({ id: 'd1', grantee_kind: 'department', to_department: { id: 'dep', name: 'Engineering' }, decision: 'accepted', standing: 'paused', title: 'Roster' }),
      internal({ id: 'c1', grantee_kind: 'person', target_kind: 'chat', grantee: { sub: 's2', name: 'Ben', username: 'ben' }, decision: 'accepted', title: 'Plan chat' }),
    ]
    render(<SharesPage />)
    expect(screen.getByText('No external links.')).toBeTruthy()
    expect(screen.getByText('waiting to be accepted')).toBeTruthy()
    expect(screen.getByText('declined by Ana')).toBeTruthy()
    expect(screen.getByText('Agent: Ops Desk')).toBeTruthy()
    expect(screen.getByText('placed')).toBeTruthy()
    expect(screen.getByText('editor')).toBeTruthy()
    expect(screen.getByText('Department: Engineering')).toBeTruthy()
    expect(screen.getByText('paused while the app is unpinned')).toBeTruthy()
    expect(screen.getByText('read-only')).toBeTruthy()
    expect(screen.getByText('shared')).toBeTruthy()
    expect(screen.getByText('5 shares')).toBeTruthy()
  })

  it('a share that still gives access asks first, worded by kind; a declined one revokes at once', () => {
    state.shares = [
      internal({ id: 'p1', grantee_kind: 'person', grantee: { sub: 's1', name: 'Ana', username: 'ana' }, decision: 'accepted' }),
      internal({ id: 'a1', grantee_kind: 'agent', to_agent: { slug: 'ops', name: 'Ops Desk' }, decision: 'accepted', title: 'Register' }),
      internal({ id: 'd1', grantee_kind: 'department', to_department: { id: 'dep', name: 'Engineering' }, decision: 'accepted', title: 'Roster' }),
      internal({ id: 'c1', grantee_kind: 'person', target_kind: 'chat', grantee: { sub: 's2', name: 'Ben', username: 'ben' }, decision: 'accepted', title: 'Plan chat' }),
      internal({ id: 'x1', grantee_kind: 'person', grantee: { sub: 's1', name: 'Ana', username: 'ana' }, decision: 'declined', standing: 'declined' }),
    ]
    render(<SharesPage />)
    const revokes = () => screen.getAllByRole('button', { name: 'Revoke' })
    fireEvent.click(revokes()[0])
    expect(state.patch).not.toHaveBeenCalled()
    expect(screen.getByTestId('admin-revoke-confirm').textContent).toContain('Ana loses access to “Schedule”. Nobody is notified.')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByTestId('admin-revoke-confirm')).toBeNull()
    fireEvent.click(revokes()[1])
    expect(screen.getByTestId('admin-revoke-confirm').textContent).toContain('“Register” leaves Ops Desk\'s apps for everyone there.')
    fireEvent.click(revokes()[2])
    expect(screen.getByTestId('admin-revoke-confirm').textContent).toContain('“Roster” leaves every agent of the Engineering department.')
    fireEvent.click(screen.getByRole('button', { name: 'Revoke the share' }))
    expect(state.patch).toHaveBeenCalledWith({ id: 'd1', revoke: true }, expect.anything())
    fireEvent.click(revokes()[3])
    expect(screen.getByTestId('admin-revoke-confirm').textContent).toContain('Ben loses the shared copy of “Plan chat”.')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    state.patch.mockClear()
    fireEvent.click(revokes()[4])
    expect(screen.queryByTestId('admin-revoke-confirm')).toBeNull()
    expect(state.patch).toHaveBeenCalledWith({ id: 'x1', revoke: true }, expect.anything())
  })

  it('a waiting or paused share asks first, the paused one worded for its return; an expired one revokes at once', () => {
    state.shares = [
      internal({ id: 'w1', grantee_kind: 'person', grantee: { sub: 's1', name: 'Ana', username: 'ana' }, decision: 'pending', standing: 'waiting' }),
      internal({ id: 'p1', grantee_kind: 'agent', to_agent: { slug: 'ops', name: 'Ops Desk' }, decision: 'accepted', standing: 'paused', title: 'Register' }),
      internal({ id: 'p2', grantee_kind: 'department', to_department: { id: 'dep', name: 'Engineering' }, decision: 'accepted', standing: 'paused', title: 'Roster' }),
      internal({ id: 'p3', grantee_kind: 'person', grantee: { sub: 's1', name: 'Ana', username: 'ana' }, decision: 'accepted', standing: 'paused' }),
      internal({ id: 'e1', grantee_kind: 'person', grantee: { sub: 's1', name: 'Ana', username: 'ana' }, decision: 'accepted', standing: 'expired' }),
    ]
    render(<SharesPage />)
    const revokes = () => screen.getAllByRole('button', { name: 'Revoke' })
    expect(revokes()[4].getAttribute('aria-expanded')).toBeNull()
    expect(revokes()[0].getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(revokes()[0])
    expect(screen.getByTestId('admin-revoke-confirm').textContent).toContain('Ana loses access to “Schedule”.')
    fireEvent.click(revokes()[1])
    expect(screen.getByTestId('admin-revoke-confirm').textContent).toContain(
      '“Register” will not come back to Ops Desk\'s apps when the app is pinned again.')
    fireEvent.click(revokes()[2])
    expect(screen.getByTestId('admin-revoke-confirm').textContent).toContain(
      '“Roster” will not come back to the agents of the Engineering department when the app is pinned again.')
    fireEvent.click(revokes()[3])
    expect(screen.getByTestId('admin-revoke-confirm').textContent).toContain(
      'Ana will not get “Schedule” back when the app is pinned again.')
    expect(state.patch).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(revokes()[4])
    expect(screen.queryByTestId('admin-revoke-confirm')).toBeNull()
    expect(state.patch).toHaveBeenCalledWith({ id: 'e1', revoke: true }, expect.anything())
  })

  it('while the next filter loads, the last list stays dimmed and no empty line is claimed', () => {
    state.shares = [link({ id: 'l1' })]
    state.placeholder = true
    render(<SharesPage />)
    expect(screen.queryByText('No shares to people, agents or departments.')).toBeNull()
    expect(screen.getAllByText('Schedule').length).toBe(1)
    expect(document.querySelector('[aria-busy="true"]')).toBeTruthy()
  })

  it('the filters reach the query, and a kind hides the other table', () => {
    state.shares = [link({ id: 'l1' })]
    render(<SharesPage />)
    expect(state.filters[state.filters.length - 1]).toEqual({ kind: 'all', agent: '', standing: '' })
    fireEvent.change(screen.getByLabelText('Filter by agent'), { target: { value: 'ops' } })
    fireEvent.change(screen.getByLabelText('Filter by standing'), { target: { value: 'paused' } })
    fireEvent.change(screen.getByLabelText('Filter by kind'), { target: { value: 'external' } })
    expect(state.filters[state.filters.length - 1]).toEqual({ kind: 'external', agent: 'ops', standing: 'paused' })
    expect(screen.queryByRole('heading', { name: 'Shared inside the platform' })).toBeNull()
    expect(screen.getByRole('heading', { name: 'External links' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Filter by kind'), { target: { value: 'internal' } })
    expect(screen.queryByRole('heading', { name: 'External links' })).toBeNull()
    expect(screen.getByRole('heading', { name: 'Shared inside the platform' })).toBeTruthy()
  })

  it('says when a kind was cut at the newest 500', () => {
    state.truncated = { internal: true, external: false }
    render(<SharesPage />)
    expect(screen.getByTestId('admin-shares-cut').textContent).toContain('newest 500 of each kind')
  })

  it('says so when a revoke is refused', () => {
    state.shares = [link({ id: 'l1', state: 'suspended', public: true })]
    state.patch.mockImplementation((_v: unknown, opts: { onError?: (e: Error) => void }) => opts.onError?.(new Error('Share not found')))
    render(<SharesPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(screen.getByRole('alert').textContent).toBe('Share not found')
  })

  it('says so when there are none', () => {
    render(<SharesPage />)
    expect(screen.getByText('Every share on the platform, to people, agents and departments or by link. '
      + 'Revoke ends a share at once and tells nobody. Sharing is switched on and off under Setup → Security.')).toBeTruthy()
    expect(screen.getByText('No external links.')).toBeTruthy()
    expect(screen.getByText('No shares to people, agents or departments.')).toBeTruthy()
    expect(screen.queryByTestId('admin-shares-cut')).toBeNull()
  })
})
