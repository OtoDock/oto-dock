// The list card in the chat (components/chat/ListCard): the rows come from
// the pages' own hooks with the viewer's sign-in, narrowed by the card's
// filters, each with its state badge and the actions its `can_*` flags
// allow, under the count and above the page link; every viewer of the chat
// sees their own rows, whoever asked; the agent's MCP servers show for a manager
// alone; a kind this version does not know shows one line; a list still
// loading, one that did not load and an empty one each show one line.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => {
  const query = <T,>(data: T) => ({ data, isLoading: false, isError: false })
  const mutation = () => ({ mutateAsync: vi.fn(async () => ({})), isPending: false })
  return {
    user: { current: { sub: 'u1', role: 'member', display_name: 'Dev', agent_roles: {} as Record<string, string> } },
    tasks: query([] as unknown[]),
    triggers: query([] as unknown[]),
    notifications: query([] as unknown[]),
    machines: query(null as unknown),
    mcps: query(null as unknown),
    runTask: { mutateAsync: vi.fn(async () => ({ run_id: 'run-7' })), isPending: false },
    pauseTask: mutation(), resumeTask: mutation(),
    confirm: vi.fn(async () => true),
    pauseTrigger: mutation(), resumeTrigger: mutation(),
    pauseNotification: mutation(), resumeNotification: mutation(),
    toast: vi.fn(),
    // Every list hook the card called, with its arguments.
    calls: [] as unknown[][],
  }
})

// Stable objects: a hook that returned a new function each render would re-render without end.
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: h.user.current }) }))
vi.mock('@/api/tasks', () => ({
  useTasks: (...a: unknown[]) => { h.calls.push(['useTasks', ...a]); return h.tasks }, useRunTaskNow: () => h.runTask, usePauseTask: () => h.pauseTask, useResumeTask: () => h.resumeTask,
}))
vi.mock('@/api/triggers', () => ({
  useTriggers: (...a: unknown[]) => { h.calls.push(['useTriggers', ...a]); return h.triggers }, usePauseTrigger: () => h.pauseTrigger, useResumeTrigger: () => h.resumeTrigger,
}))
vi.mock('@/api/notifications', () => ({
  useNotificationDefinitions: (...a: unknown[]) => { h.calls.push(['useNotificationDefinitions', ...a]); return h.notifications }, usePauseNotification: () => h.pauseNotification, useResumeNotification: () => h.resumeNotification,
}))
vi.mock('@/api/remoteMachines', () => ({
  useMyRemoteMachines: (...a: unknown[]) => { h.calls.push(['useMyRemoteMachines', ...a]); return h.machines },
}))
vi.mock('@/components/ui/dialogs', () => ({ useDialogs: () => ({ confirm: h.confirm }) }))
vi.mock('@/api/mcps', () => ({ useAgentMcps: (...a: unknown[]) => { h.calls.push(['useAgentMcps', ...a]); return h.mcps } }))
vi.mock('@/lib/appToasts', () => ({ emitAppToast: h.toast, onAppToast: () => () => {} }))

import ListCard from '@/components/chat/ListCard'

function card(kind: string, filters: Record<string, string> = {}, title = 'A list') {
  return render(
    <MemoryRouter>
      <ListCard kind={kind} filters={filters} title={title} agentName="alpha" />
    </MemoryRouter>,
  )
}

const rowTexts = () => screen.getAllByTestId('list-card-row').map((r) => r.textContent)
const link = () => screen.getByRole('link', { name: 'Open the page' })

function task(over: Record<string, unknown>) {
  return { id: 't', name: 'Task', enabled: true, scope: 'agent', created_by: 'u1', can_run: false, can_pause: false, can_resume: false, ...over }
}

afterEach(() => {
  h.user.current = { sub: 'u1', role: 'member', display_name: 'Dev', agent_roles: {} }
  h.tasks.data = []
  h.tasks.isLoading = false
  h.tasks.isError = false
  h.triggers.data = []
  h.notifications.data = []
  h.machines.data = null
  h.mcps.data = null
  h.runTask.mutateAsync.mockClear()
  h.confirm.mockClear()
  h.confirm.mockImplementation(async () => true)
  h.pauseTrigger.mutateAsync.mockClear()
  h.resumeNotification.mutateAsync.mockClear()
  h.calls.length = 0
})

describe('the list card', () => {
  it('shows any viewer their own rows, whoever asked for the card', () => {
    // Another person asked; this viewer's own sign-in reads their own list.
    h.user.current = { sub: 'u2', role: 'member', display_name: 'Pat', agent_roles: {} }
    h.tasks.data = [task({})]
    card('tasks', {}, 'Your scheduled tasks')
    expect(screen.getByTestId('list-card-rows')).toBeInTheDocument()
    expect(h.calls).toEqual([['useTasks', 'alpha']])
  })

  it('reads each kind through the page hook with the agent the card names', () => {
    h.machines.data = { machines: [], targets: [] }
    h.mcps.data = { mcps: [] }
    h.user.current = { sub: 'u1', role: 'member', display_name: 'Dev', agent_roles: { alpha: 'manager' } }
    for (const kind of ['tasks', 'triggers', 'notifications', 'machines', 'mcps']) {
      card(kind)
      cleanup()
    }
    expect(h.calls).toEqual([
      ['useTasks', 'alpha'], ['useTriggers', { agent: 'alpha' }], ['useNotificationDefinitions', 'alpha'],
      ['useMyRemoteMachines'], ['useAgentMcps', 'alpha'],
    ])
  })

  it('never reads the MCP servers for a person below manager', () => {
    card('mcps', {}, 'MCP servers')
    expect(h.calls).toEqual([])
  })

  it('narrows the triggers and the notifications by scope and state', () => {
    h.triggers.data = [
      { id: 'tr1', name: 'On push', enabled: true, scope: 'agent', created_by: 'u1', can_pause: true, can_resume: false },
      { id: 'tr2', name: 'Nightly', enabled: false, scope: 'user', created_by: 'u1', can_pause: false, can_resume: true },
      { id: 'tr3', name: 'Weekly', enabled: false, scope: 'agent', created_by: 'u2', can_pause: false, can_resume: false },
    ]
    let view = card('triggers', { scope: 'agent' }, 'Triggers')
    expect(rowTexts()).toEqual(['On pushActivePause', 'WeeklyPaused'])
    view.unmount()
    view = card('triggers', { scope: 'mine', status: 'paused' }, 'Your triggers')
    expect(rowTexts()).toEqual(['NightlyPausedResume'])
    view.unmount()
    h.notifications.data = [
      { id: 'n1', title: 'Standup reminder', enabled: 0, scope: 'user', created_by: 'u1', can_pause: false, can_resume: true },
      { id: 'n2', title: 'Team digest', enabled: 1, scope: 'agent', created_by: 'u1', can_pause: true, can_resume: false },
    ]
    view = card('notifications', { status: 'active' }, 'Notifications')
    expect(rowTexts()).toEqual(['Team digestActivePause'])
    view.unmount()
    card('notifications', { scope: 'agent', status: 'paused' }, 'Notifications')
    expect(screen.getByText('Nothing to show.')).toBeInTheDocument()
  })

  it('lists the tasks the filters keep, with a badge and the actions the flags allow, and runs one', () => {
    h.tasks.data = [
      task({ id: 'a', name: 'Daily digest', scope: 'user', created_by: 'u1', enabled: true, can_run: true, can_pause: true }),
      task({ id: 'b', name: 'Paused one', scope: 'user', created_by: 'u1', enabled: false, can_resume: true }),
      // An agent task the person created is the agent's, not theirs (the page's rule).
      task({ id: 'c', name: 'Not mine', scope: 'agent', created_by: 'u1', enabled: true, can_run: true }),
    ]
    card('tasks', { scope: 'mine', status: 'active' }, 'Your scheduled tasks')
    expect(screen.getByTestId('list-card-sub')).toHaveTextContent('1 task')
    expect(rowTexts()).toEqual(['Daily digestActiveRunPause'])
    const row = screen.getByTestId('list-card-row')
    expect(within(row).getByText('Active').className).toContain('text-status-success-fg')
    expect(within(row).queryByRole('button', { name: 'Resume' })).toBeNull()
    expect(link()).toHaveAttribute('href', '/agents/alpha/scheduled-tasks')
  })

  it('asks before a run with the page words, then shows a line with the run link', async () => {
    h.tasks.data = [task({ id: 'a', name: 'Daily digest', scope: 'user', can_run: true })]
    card('tasks', {}, 'Your scheduled tasks')
    h.confirm.mockImplementationOnce(async () => false)
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))
    await waitFor(() => expect(h.confirm).toHaveBeenCalledWith({ title: 'Run "Daily digest" now?', confirmLabel: 'Run now' }))
    expect(h.runTask.mutateAsync).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Run' }))
    const line = await screen.findByTestId('list-card-run')
    expect(h.runTask.mutateAsync).toHaveBeenCalledWith('a')
    expect(line).toHaveTextContent('The run of "Daily digest" started.')
    expect(within(line).getByRole('link', { name: 'Open the run' })).toHaveAttribute('href', '/runs/run-7')
  })

  it('narrows to the agent rows or to the paused rows, and counts them', () => {
    h.tasks.data = [
      task({ id: 'a', name: 'Mine', scope: 'user', created_by: 'u1' }),
      task({ id: 'b', name: 'Agent one', scope: 'agent', created_by: 'u2', enabled: false }),
      task({ id: 'c', name: 'Agent two', scope: 'agent', created_by: 'u1' }),
    ]
    const { unmount } = card('tasks', { scope: 'agent' })
    expect(screen.getByTestId('list-card-sub')).toHaveTextContent('2 tasks')
    expect(rowTexts()).toEqual(['Agent onePaused', 'Agent twoActive'])
    unmount()
    card('tasks', { status: 'paused' })
    expect(rowTexts()).toEqual(['Agent onePaused'])
  })

  it('shows twelve rows and says how many more the page holds', () => {
    h.tasks.data = Array.from({ length: 20 }, (_v, i) => task({ id: `t${i}`, name: `Task ${i}`, scope: 'user' }))
    card('tasks', {}, 'Your scheduled tasks')
    expect(screen.getAllByTestId('list-card-row')).toHaveLength(12)
    expect(screen.getByTestId('list-card-sub')).toHaveTextContent('20 tasks')
    expect(screen.getByTestId('list-card-more')).toHaveTextContent('And 8 more on the page.')
    expect(link()).toHaveAttribute('href', '/agents/alpha/scheduled-tasks')
  })

  it('lists the triggers with Pause or Resume by the flags, and pauses one', () => {
    h.triggers.data = [
      { id: 'tr1', name: 'On push', enabled: true, scope: 'agent', created_by: 'u1', can_pause: true, can_resume: false },
      { id: 'tr2', name: 'Nightly', enabled: false, scope: 'user', created_by: 'u1', can_pause: false, can_resume: true },
    ]
    card('triggers', {}, 'Triggers')
    expect(rowTexts()).toEqual(['On pushActivePause', 'NightlyPausedResume'])
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(h.pauseTrigger.mutateAsync).toHaveBeenCalledWith('tr1')
    expect(link()).toHaveAttribute('href', '/agents/alpha/triggers')
  })

  it('lists the notifications by their title, and resumes one', () => {
    h.notifications.data = [
      { id: 'n1', title: 'Standup reminder', enabled: 0, scope: 'user', created_by: 'u1', can_pause: false, can_resume: true },
      // A user notification an admin aimed at the person is theirs too.
      { id: 'n2', title: 'Rota for you', enabled: 1, scope: 'user', created_by: 'u-admin', can_pause: true, can_resume: false },
      { id: 'n3', title: 'Team digest', enabled: 1, scope: 'agent', created_by: 'u1', can_pause: false, can_resume: false },
    ]
    card('notifications', { scope: 'mine' }, 'Your notifications')
    expect(rowTexts()).toEqual(['Standup reminderPausedResume', 'Rota for youActivePause'])
    cleanup()
    h.notifications.data = h.notifications.data.slice(0, 1)
    card('notifications', {}, 'Your notifications')
    expect(rowTexts()).toEqual(['Standup reminderPausedResume'])
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(h.resumeNotification.mutateAsync).toHaveBeenCalledWith('n1')
    expect(link()).toHaveAttribute('href', '/agents/alpha/notifications')
  })

  it('lists the machines read-only with their state, and links to the user settings', () => {
    h.machines.data = { machines: [
      { id: 'm1', name: 'Studio', status: 'online' },
      { id: 'm2', name: 'Laptop', status: 'disconnected' },
    ], targets: [] }
    card('machines', {}, 'Your remote machines')
    expect(screen.getByTestId('list-card-sub')).toHaveTextContent('2 machines')
    expect(rowTexts()).toEqual(['Studioonline', 'Laptopoffline'])
    expect(screen.queryByRole('button')).toBeNull()
    expect(link()).toHaveAttribute('href', '/user-settings/remote-machines')
  })

  it('shows the MCP servers to a manager alone, read-only', () => {
    h.mcps.data = { mcps: [
      { name: 'github', label: 'GitHub', enabled: true },
      { name: 'slack', label: 'Slack', enabled: false },
    ] }
    const { unmount } = card('mcps', {}, 'MCP servers')
    expect(screen.getByText("Only managers and admins see the agent's MCP servers.")).toBeInTheDocument()
    expect(screen.queryByTestId('list-card-rows')).toBeNull()
    unmount()
    h.user.current = { sub: 'u1', role: 'member', display_name: 'Dev', agent_roles: { alpha: 'manager' } }
    card('mcps', {}, 'MCP servers')
    expect(rowTexts()).toEqual(['GitHubEnabled', 'SlackOff'])
    expect(screen.queryByRole('button')).toBeNull()
    expect(link()).toHaveAttribute('href', '/agents/alpha/mcps')
  })

  it('says so for a kind this version does not know', () => {
    card('budgets', {}, 'Budgets')
    expect(screen.getByText('This list is not in this version.')).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('shows one line while the list loads, when it did not load, and when it is empty', () => {
    h.tasks.isLoading = true
    let view = card('tasks')
    expect(screen.getByText('Reading the list.')).toBeInTheDocument()
    expect(screen.queryByTestId('list-card-sub')).toBeNull()
    view.unmount()
    h.tasks.isLoading = false
    h.tasks.isError = true
    view = card('tasks')
    expect(screen.getByText('The list did not load. Reload the page to see it.')).toBeInTheDocument()
    view.unmount()
    h.tasks.isError = false
    card('tasks')
    expect(screen.getByText('Nothing to show.')).toBeInTheDocument()
    expect(link()).toBeInTheDocument()
  })
})
