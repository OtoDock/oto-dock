/**
 * useSnapshotHeal (components/agents-map/useSnapshotHeal.ts): an admin's
 * map grays by the mount-time auth snapshot; a slug the polled agents list
 * carries that the snapshot lacks refetches the snapshot once, latched per
 * slug — so an agent created by another agent through the Agent Creator
 * (its creator granted manager in the same request) stops showing gray
 * without a hard reload, and a genuine non-member never refires.
 */
import { describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useSnapshotHeal } from '../components/agents-map/useSnapshotHeal'
import type { AgentSummary } from '../api/agents'

const agent = (name: string): AgentSummary => ({
  name, display_name: name, admin_only: false,
  execution_path: 'claude-code-cli', execution_paths: ['claude-code-cli'],
  execution_target: 'local', collaborative: true, default_model: '',
  default_scope: 'user', color: '', description: '', mcp_count: 0,
  mcp_names: [], schedule_count: 0, trigger_count: 0, has_workspace: false,
  department_id: '', department_level_id: '',
} as unknown as AgentSummary)

const admin = (agents: string[]) => ({ role: 'admin', agents })

describe('useSnapshotHeal', () => {
  it('refetches once for a slug the snapshot lacks and latches it', () => {
    const refreshUser = vi.fn(() => Promise.resolve())
    const { rerender } = renderHook(
      (p: { agents: AgentSummary[]; user: { role: string; agents: string[] } }) =>
        useSnapshotHeal({ ...p, refreshUser, settled: true }),
      { initialProps: { agents: [agent('mine'), agent('fresh')], user: admin(['mine']) } },
    )
    expect(refreshUser).toHaveBeenCalledTimes(1)
    // The snapshot still lacks it (a genuine non-member, or the refresh
    // raced): no second call for the same slug.
    rerender({ agents: [agent('mine'), agent('fresh')], user: admin(['mine']) })
    expect(refreshUser).toHaveBeenCalledTimes(1)
  })

  it('refires when a NEW slug appears later', () => {
    const refreshUser = vi.fn(() => Promise.resolve())
    const { rerender } = renderHook(
      (p: { agents: AgentSummary[] }) =>
        useSnapshotHeal({ ...p, user: admin(['mine']), refreshUser, settled: true }),
      { initialProps: { agents: [agent('mine')] } },
    )
    expect(refreshUser).not.toHaveBeenCalled()
    rerender({ agents: [agent('mine'), agent('created-by-an-agent')] })
    expect(refreshUser).toHaveBeenCalledTimes(1)
  })

  it('does nothing for non-admins or unsettled data', () => {
    const refreshUser = vi.fn(() => Promise.resolve())
    renderHook(() => useSnapshotHeal({
      agents: [agent('other')], user: { role: 'member', agents: [] },
      refreshUser, settled: true,
    }))
    renderHook(() => useSnapshotHeal({
      agents: [agent('other')], user: admin([]), refreshUser, settled: false,
    }))
    expect(refreshUser).not.toHaveBeenCalled()
  })

  it('un-latches the round when the refresh fails, so the next poll retries', async () => {
    const refreshUser = vi.fn()
      .mockImplementationOnce(() => Promise.reject(new Error('offline')))
      .mockImplementation(() => Promise.resolve())
    const { rerender } = renderHook(
      (p: { agents: AgentSummary[] }) =>
        useSnapshotHeal({ ...p, user: admin(['mine']), refreshUser, settled: true }),
      { initialProps: { agents: [agent('mine'), agent('fresh')] } },
    )
    expect(refreshUser).toHaveBeenCalledTimes(1)
    await Promise.resolve()
    await Promise.resolve()
    rerender({ agents: [agent('mine'), agent('fresh'), agent('another')] })
    expect(refreshUser).toHaveBeenCalledTimes(2)
  })
})
