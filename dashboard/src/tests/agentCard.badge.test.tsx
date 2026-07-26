import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

vi.mock('@/api/remoteMachines', () => ({ useRemoteMachines: () => ({ data: [] }) }))

import AgentCard from '@/components/AgentCard'
import type { AgentSummary } from '@/api/agents'

function makeAgent(collaborative: boolean, default_scope: 'user' | 'agent'): AgentSummary {
  return {
    name: 'demo',
    display_name: 'Demo',
    description: '',
    color: '#336699',
    collaborative,
    default_scope,
    execution_target: 'local',
    mcp_count: 0,
    schedule_count: 0,
    trigger_count: 0,
  } as unknown as AgentSummary
}

function renderCard(agent: AgentSummary) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AgentCard agent={agent} isDefault={false} onSetDefault={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// Every mode gets a badge — including the default Personal + shared, which
// used to be deliberately unbadged and left collaborative agents unlabelled
// on the grid.
describe('AgentCard mode badge', () => {
  it.each([
    [true, 'user', 'Personal + shared'],
    [true, 'agent', 'Shared + personal'],
    [false, 'user', 'Personal only'],
    [false, 'agent', 'Shared only'],
  ] as const)('collaborative=%s scope=%s shows "%s"', (collab, scope, label) => {
    const { unmount } = renderCard(makeAgent(collab, scope))
    expect(screen.getByText(label)).toBeInTheDocument()
    unmount()
  })
})
