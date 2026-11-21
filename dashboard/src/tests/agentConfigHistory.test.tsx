import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  user: { role: 'admin', agent_roles: { demo: 'manager' } } as Record<string, unknown>,
  days: 0,
  update: vi.fn(async () => ({})),
}))

vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: h.user }) }))
vi.mock('../api/agents', () => ({
  useAgentInfo: () => ({ data: { history_retention_days: h.days } }),
  useUpdateAgent: () => ({ mutateAsync: h.update, isPending: false }),
}))

import { HistorySection } from '../pages/agent/AgentConfig.history'

describe('HistorySection', () => {
  beforeEach(() => {
    h.user = { role: 'admin', agent_roles: { demo: 'manager' } }
    h.days = 0
    h.update.mockClear()
  })

  it('tells a manager what the window is and lets only an admin change it', () => {
    h.user = { role: 'member', agent_roles: { demo: 'manager' } }
    h.days = 90
    render(<HistorySection name="demo" />)
    expect(screen.getByText(/older than 90 days are deleted for good/)).toBeTruthy()
    expect(screen.getByText(/An admin can change this/)).toBeTruthy()
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('says the agent keeps everything when the window is off', () => {
    h.user = { role: 'member', agent_roles: { demo: 'manager' } }
    render(<HistorySection name="demo" />)
    expect(screen.getByText(/keeps its chats and task results for ever/)).toBeTruthy()
  })

  it('shows nothing to someone who cannot manage the agent', () => {
    h.user = { role: 'member', agent_roles: { demo: 'viewer' } }
    const { container } = render(<HistorySection name="demo" />)
    expect(container.firstChild).toBeNull()
  })

  it('asks before an admin turns the window on', async () => {
    render(<HistorySection name="demo" />)
    fireEvent.click(screen.getByRole('switch', { name: 'Delete old history' }))
    expect(h.update).not.toHaveBeenCalled()
    const word = screen.getByRole('textbox')
    fireEvent.change(word, { target: { value: 'DELETE-OLD-HISTORY' } })
    fireEvent.click(screen.getByRole('button', { name: 'Delete old history' }))
    await waitFor(() => expect(h.update).toHaveBeenCalledWith({ name: 'demo', history_retention_days: 180 }))
  })

  it('asks before a shorter window deletes more, and a no leaves the window as it was', async () => {
    h.days = 90
    render(<HistorySection name="demo" />)
    const field = screen.getByLabelText(/Days to keep history/, { selector: 'input' }) as HTMLInputElement
    fireEvent.change(field, { target: { value: '7' } })
    fireEvent.blur(field)
    expect(h.update).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(h.update).not.toHaveBeenCalled()
    expect(field.value).toBe('90')

    fireEvent.change(field, { target: { value: '30' } })
    fireEvent.blur(field)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'DELETE-OLD-HISTORY' } })
    fireEvent.click(screen.getByRole('button', { name: 'Delete old history' }))
    await waitFor(() => expect(h.update).toHaveBeenCalledWith({ name: 'demo', history_retention_days: 30 }))
  })

  it('lets the server answer a number out of range and ignores an empty field', async () => {
    h.days = 90
    render(<HistorySection name="demo" />)
    const field = screen.getByLabelText(/Days to keep history/, { selector: 'input' })
    fireEvent.change(field, { target: { value: '' } })
    fireEvent.blur(field)
    expect(h.update).not.toHaveBeenCalled()
    fireEvent.change(field, { target: { value: '3' } })
    fireEvent.blur(field)
    await waitFor(() => expect(h.update).toHaveBeenCalledWith({ name: 'demo', history_retention_days: 3 }))
  })

  it('turns off with no question and saves a longer window on leaving the field', async () => {
    h.days = 90
    render(<HistorySection name="demo" />)
    const field = screen.getByLabelText(/Days to keep history/, { selector: 'input' })
    fireEvent.change(field, { target: { value: '200' } })
    fireEvent.blur(field)
    await waitFor(() => expect(h.update).toHaveBeenCalledWith({ name: 'demo', history_retention_days: 200 }))
    h.update.mockClear()
    fireEvent.click(screen.getByRole('switch', { name: 'Delete old history' }))
    await waitFor(() => expect(h.update).toHaveBeenCalledWith({ name: 'demo', history_retention_days: 0 }))
  })
})
