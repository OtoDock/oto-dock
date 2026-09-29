import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

const state = vi.hoisted(() => ({ shares: [] as unknown[], patch: vi.fn() }))
vi.mock('@/api/shares', () => ({
  useAdminShares: () => ({ data: state.shares, isLoading: false, error: null }),
  usePatchShare: () => ({ mutate: state.patch, isPending: false }),
}))
vi.mock('@/api/agents', () => ({
  useAgents: () => ({ data: [{ name: 'dev', display_name: 'Dev Agent' }] }),
}))

import SharesPage from '@/pages/admin/SharesPage'

describe('Admin → Shares', () => {
  it('lists every external link with its protection and revokes one', () => {
    const inTenDays = new Date(Date.now() + 10 * 86_400_000 - 60_000).toISOString()
    state.shares = [
      { id: 'l1', target_kind: 'app', target_id: 'a', scope: 'external', state: 'active', grantee: null,
        hidden_by_grantee: false, public: true, allow_actions: false, created_by: 'u1', created_by_name: 'Nora',
        created_at: '', expires_at: null, last_access_at: null, access_count: 0, title: 'Schedule', agent: 'dev' },
      { id: 'l2', target_kind: 'app', target_id: 'a', scope: 'external', state: 'active', grantee: null,
        hidden_by_grantee: false, public: false, allow_actions: true, created_by: 'u1', created_by_name: 'Nora',
        created_at: '', expires_at: inTenDays, last_access_at: null, access_count: 0, title: 'Schedule', agent: 'dev' },
    ]
    render(<SharesPage />)
    expect(screen.getAllByText('Schedule').length).toBe(2)
    // The agent name sits in the filter and in the rows.
    expect(screen.getAllByText('Dev Agent').length).toBe(3)
    expect(screen.getByText('public')).toBeTruthy()
    // Never opened, never expires — and an expiry is said as a time to come.
    expect(screen.getAllByText('never').length).toBe(3)
    expect(screen.getByText('expires in 10 days')).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: 'Revoke' })[0])
    expect(state.patch).toHaveBeenCalledWith({ id: 'l1', revoke: true }, expect.anything())
  })

  it('says so when a revoke is refused', () => {
    state.shares = [
      { id: 'l1', target_kind: 'app', target_id: 'a', scope: 'external', state: 'suspended', grantee: null,
        hidden_by_grantee: false, public: true, allow_actions: false, created_by: 'u1', created_by_name: 'Nora',
        created_at: '', expires_at: null, last_access_at: null, access_count: 0, title: 'Schedule', agent: 'dev' },
    ]
    state.patch.mockImplementation((_v: unknown, opts: { onError?: (e: Error) => void }) => opts.onError?.(new Error('Share not found')))
    render(<SharesPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(screen.getByRole('alert').textContent).toBe('Share not found')
    state.patch.mockReset()
  })

  it('says so when there are none', () => {
    state.shares = []
    render(<SharesPage />)
    expect(screen.getByText('No external links.')).toBeTruthy()
  })
})
