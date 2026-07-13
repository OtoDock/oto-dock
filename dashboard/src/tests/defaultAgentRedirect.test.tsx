import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// ─── "/" redirect resolution: favorite → first VISIBLE agent (any role) →
//     /agents. A member with agents but no favorite used to bounce back to
//     /agents — the dead Back-to-Chat button (operator repro 2026-07-13). ───

const authState = { user: null as unknown }
const agentsState = { data: undefined as unknown, isLoading: false }

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => authState }))
vi.mock('@/api/agents', () => ({ useAgents: () => agentsState }))

import DefaultAgentRedirect from '@/components/DefaultAgentRedirect'

function renderAt() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<DefaultAgentRedirect />} />
        <Route path="/chat/:name" element={<ChatMarker />} />
        <Route path="/agents" element={<div>AGENTS PAGE</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

import { useParams } from 'react-router-dom'
function ChatMarker() {
  const { name } = useParams()
  return <div>CHAT:{name}</div>
}

function setState(user: object, agents: { name: string }[] | undefined, isLoading = false) {
  authState.user = user
  agentsState.data = agents
  agentsState.isLoading = isLoading
}

describe('DefaultAgentRedirect', () => {
  afterEach(() => setState({}, undefined))

  it('member with agents but NO favorite lands on the first visible agent', () => {
    setState({ role: 'member', default_agent: '', agents: [] },
      [{ name: 'alpha' }, { name: 'beta' }, { name: 'gamma' }])
    renderAt()
    expect(screen.getByText('CHAT:alpha')).toBeInTheDocument()
  })

  it('an explicit favorite that still exists wins', () => {
    setState({ role: 'member', default_agent: 'beta', agents: ['beta'] },
      [{ name: 'alpha' }, { name: 'beta' }])
    renderAt()
    expect(screen.getByText('CHAT:beta')).toBeInTheDocument()
  })

  it('a favorite pointing at a deleted agent falls through to the first visible', () => {
    setState({ role: 'member', default_agent: 'gone', agents: ['gone'] },
      [{ name: 'alpha' }])
    renderAt()
    expect(screen.getByText('CHAT:alpha')).toBeInTheDocument()
  })

  it('no agents at all → the agents page', () => {
    setState({ role: 'member', default_agent: '', agents: [] }, [])
    renderAt()
    expect(screen.getByText('AGENTS PAGE')).toBeInTheDocument()
  })
})
