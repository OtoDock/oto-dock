import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const h = vi.hoisted(() => ({
  update: vi.fn(async () => ({})),
  confirm: vi.fn(async (_options: unknown) => true),
}))

vi.mock('../api/agents', () => ({
  useUpdateAgent: () => ({ mutateAsync: h.update, isPending: false }),
}))
vi.mock('../components/ui/dialogs', () => ({ useDialogs: () => ({ confirm: h.confirm }) }))

import { TeamAccountSwitch, TEAM_ACCOUNT_LABEL } from '../pages/agent/AgentConfig.teamAccount'
import type { AgentInfo } from '../api/agents'

const info = (over: Partial<AgentInfo> = {}) => ({ name: 'demo', team_account: false, ...over }) as AgentInfo

describe('TeamAccountSwitch', () => {
  beforeEach(() => {
    h.update.mockClear()
    h.confirm.mockClear()
    h.confirm.mockResolvedValue(true)
  })

  it('is shown for the two collaborative modes only', () => {
    const { container, rerender } = render(
      <TeamAccountSwitch name="demo" info={info()} mode="shared_only" canManage />)
    expect(container.firstChild).toBeNull()
    rerender(<TeamAccountSwitch name="demo" info={info()} mode="personal_only" canManage />)
    expect(container.firstChild).toBeNull()
    rerender(<TeamAccountSwitch name="demo" info={info()} mode="shared_personal" canManage />)
    expect(screen.getByRole('switch', { name: TEAM_ACCOUNT_LABEL })).toBeTruthy()
  })

  it('asks before turning it on, and a no leaves it off', async () => {
    h.confirm.mockResolvedValue(false)
    render(<TeamAccountSwitch name="demo" info={info()} mode="personal_shared" canManage />)
    fireEvent.click(screen.getByRole('switch', { name: TEAM_ACCOUNT_LABEL }))
    await waitFor(() => expect(h.confirm).toHaveBeenCalledTimes(1))
    expect(h.confirm.mock.calls[0][0]).toMatchObject({
      title: "Use the agent's own accounts?",
      body: "Every chat of this agent then uses the agent's own accounts, not each person's. "
        + "People below the Editor role then get no account for these tools.",
    })
    expect(h.update).not.toHaveBeenCalled()
    expect(screen.getByRole('switch', { name: TEAM_ACCOUNT_LABEL }).getAttribute('aria-checked')).toBe('false')
  })

  it('a yes saves the switch, then the floor select appears and saves on change', async () => {
    render(<TeamAccountSwitch name="demo" info={info()} mode="personal_shared" canManage />)
    fireEvent.click(screen.getByRole('switch', { name: TEAM_ACCOUNT_LABEL }))
    await waitFor(() => expect(h.update).toHaveBeenCalledWith({ name: 'demo', team_account: true }))
    const select = screen.getByLabelText('From this role') as HTMLSelectElement
    expect(select.value).toBe('editor')
    expect(screen.getByText("People below this role get no account for the agent's tools in their chats.")).toBeTruthy()
    fireEvent.change(select, { target: { value: 'viewer' } })
    await waitFor(() => expect(h.update).toHaveBeenCalledWith({ name: 'demo', team_account_floor: 'viewer' }))
  })

  it('turns off without a question', async () => {
    render(<TeamAccountSwitch name="demo" info={info({ team_account: true, team_account_floor: 'manager' })}
      mode="personal_shared" canManage />)
    expect((screen.getByLabelText('From this role') as HTMLSelectElement).value).toBe('manager')
    fireEvent.click(screen.getByRole('switch', { name: TEAM_ACCOUNT_LABEL }))
    await waitFor(() => expect(h.update).toHaveBeenCalledWith({ name: 'demo', team_account: false }))
    expect(h.confirm).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('From this role')).toBeNull()
  })

  it('reads as a sentence to someone who cannot manage the agent', () => {
    const { rerender } = render(
      <TeamAccountSwitch name="demo" info={info({ team_account: true, team_account_floor: 'editor' })}
        mode="personal_shared" canManage={false} />)
    expect(screen.getByText("Every chat of this agent uses the agent's own accounts, from the editor role up.")).toBeTruthy()
    expect(screen.queryByRole('switch')).toBeNull()
    rerender(<TeamAccountSwitch name="demo" info={info()} mode="personal_shared" canManage={false} />)
    expect(screen.getByText("People's chats use their own accounts.")).toBeTruthy()
  })
})
