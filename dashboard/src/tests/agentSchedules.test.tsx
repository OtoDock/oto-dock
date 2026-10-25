import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// ─── The Scheduled Tasks tab: the schedule in words names the task's zone
//     when the browser sits in another one, and the phone card's class
//     contract (jsdom does no layout — the width check is the browser) ───

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'admin', display_name: 'Dev Admin', agent_roles: {} } }),
}))

const TASKS = [
  {
    id: 'task-hosting-admin-daily-health-hosting-admin-google-oauth2|100000000000000000001',
    name: 'Daily health', agent: 'hosting-admin', schedule: '0 8 * * *', run_at: null, delay_seconds: null,
    interval_seconds: null, llm_mode: 'cli', prompt: 'check', enabled: true, timeout_seconds: 600,
    next_run_time: '2026-09-20T05:00:00+00:00', scope: 'agent', created_by: 'u1', task_type: 'scheduled',
    notification_mode: 'manual', notify_severity: 'info', override_model: null, override_execution_path: null,
    effective_model: 'claude-sonnet-5', effective_execution_path: 'claude-code-cli',
    user_tz: null, effective_tz: 'Europe/Athens', schedule_text: 'Daily at 08:00 (Europe/Athens)',
    can_run: true, can_delete: true, can_pause: true, can_resume: false,
    transferred_from: 'local:pat', transferred_at: '2026-09-27T07:51:00+00:00',
    transferred_from_name: 'Pat Person',
    target_machine: 'm-drill', target_machine_label: 'drill-sat', target_machine_state: 'disconnected',
  },
  {
    id: 'dyn-2', name: 'Here', agent: 'hosting-admin', schedule: '0 9 * * 1-5', run_at: null, delay_seconds: null,
    interval_seconds: null, llm_mode: 'cli', prompt: 'x', enabled: false, timeout_seconds: 600, next_run_time: null,
    scope: 'user', created_by: 'u1', task_type: 'scheduled', notification_mode: 'none', notify_severity: 'info',
    override_model: null, override_execution_path: null, effective_model: '', effective_execution_path: '',
    user_tz: 'Pacific/Kiritimati', effective_tz: 'Pacific/Kiritimati', schedule_text: 'Weekdays at 09:00 (Pacific/Kiritimati)',
    can_run: true, can_delete: true, can_pause: false, can_resume: true,
  },
]

// A task nobody here may change: no Settings on its row.
const READ_ONLY = {
  ...TASKS[1], id: 'dyn-3', name: 'Theirs', scope: 'user', created_by: 'u2',
  can_run: false, can_delete: false, can_pause: false, can_resume: false,
}

const h = vi.hoisted(() => ({
  tasks: null as unknown as unknown[],
  editMachine: vi.fn(),
  editMode: vi.fn(),
  runTargets: vi.fn(),
}))

vi.mock('@/api/tasks', () => ({
  TASK_MACHINE_DEFAULT: 'default',
  useTasks: () => ({ data: h.tasks, isLoading: false }),
  useRunTaskNow: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
  usePauseTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useResumeTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useEditTaskMachine: () => ({ mutateAsync: h.editMachine, isPending: false }),
  useEditTaskMode: () => ({ mutateAsync: h.editMode, isPending: false }),
}))

// The places the task dialog offers for the task's scope.
vi.mock('@/api/remoteMachines', () => ({
  useAgentRunTargets: (agent: string, scope?: string) => h.runTargets(agent, scope),
}))

const RUN_TARGETS = {
  default_target: 'local',
  targets: [
    { target: 'local', label: 'OtoDock platform', pairing: '', state: '', reachable: true,
      is_default: true, pickable: true, why_not: '', allowed: true },
    { target: 'm-attic', label: 'Attic PC', pairing: 'admin', state: 'online', reachable: true,
      is_default: false, pickable: true, why_not: '', allowed: true },
    { target: 'm-drill', label: 'drill-sat', pairing: 'admin', state: 'disconnected', reachable: false,
      is_default: false, pickable: false, why_not: 'drill-sat is offline.', allowed: true },
  ],
}

beforeEach(() => {
  h.tasks = TASKS
  h.editMachine.mockReset()
  h.editMachine.mockResolvedValue(undefined)
  h.editMode.mockReset()
  h.editMode.mockResolvedValue(undefined)
  h.runTargets.mockReset()
  h.runTargets.mockReturnValue({ data: RUN_TARGETS, isError: false })
})

import AgentSchedules from '@/pages/agent/AgentSchedules'
import { TaskSettingsModal } from '@/pages/agent/AgentSchedules.modals'
import type { Task } from '@/api/tasks'

function mount() {
  return render(
    <MemoryRouter initialEntries={['/agents/hosting-admin/scheduled-tasks']}>
      <Routes>
        <Route path="/agents/:name/scheduled-tasks" element={<AgentSchedules />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('AgentSchedules', () => {
  afterEach(() => vi.restoreAllMocks())

  it("names a task's zone when the browser sits elsewhere, and the phone card can shrink", () => {
    // The browser on Kiritimati: the Athens task says so, the Kiritimati one does not.
    vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions')
      .mockReturnValue({ timeZone: 'Pacific/Kiritimati' } as Intl.ResolvedDateTimeFormatOptions)
    mount()
    const cards = screen.getAllByTestId('task-card-main')
    expect(cards).toHaveLength(2)
    const athens = cards[0].parentElement!.parentElement!
    expect(within(athens).getByText(/Daily at 08:00 \(Europe\/Athens\)/)).toBeInTheDocument()
    const local = cards[1].parentElement!.parentElement!
    expect(within(local).getByText(/Weekdays at 09:00/).textContent).not.toContain('Pacific')
    // The class contract of the phone card: the left column may shrink and
    // its 80-character id breaks anywhere; the status pill never shrinks.
    expect(cards[0].className).toContain('min-w-0')
    expect(cards[0].className).toContain('flex-1')
    expect(within(cards[0]).getByText(TASKS[0].id).className).toContain('break-all')
    expect(screen.getAllByTestId('task-card-status')[0].className).toContain('shrink-0')
    // The model chip truncates its id in its own span, inside a bounded box.
    const chip = within(cards[0]).getByTitle(/Agent default: claude-sonnet-5/)
    expect(chip.className).toContain('max-w-full')
    expect(within(chip).getByText('claude-sonnet-5').className).toContain('truncate')
  })

  it('names who a moved task was transferred from, on the table and the card', () => {
    mount()
    const lines = screen.getAllByTestId('transferred-from')
    // One for the desktop row and one for the phone card of the moved task;
    // the task that never changed hands shows none.
    expect(lines).toHaveLength(2)
    for (const l of lines) {
      expect(l.textContent).toBe('Transferred from Pat Person')
      expect(l.getAttribute('title')).toMatch(/^Moved to its current owner on /)
    }
  })

  it("offers Settings on the rows the person may change, a paused one included, on the table and the card", () => {
    h.tasks = [...TASKS, READ_ONLY]
    mount()
    // Two editable tasks (one active, one paused) in two layouts.
    expect(screen.getAllByRole('button', { name: 'Settings' })).toHaveLength(4)
  })

  it("names the task's own machine beside its model chip, offline said", () => {
    mount()
    const lines = screen.getAllByTestId('task-machine')
    expect(lines).toHaveLength(2)
    for (const l of lines) expect(l.textContent).toBe('on drill-sat, offline')
  })

  it('names an Auto task with a chip on the table and the card, and a Don\'t Ask task with none', () => {
    h.tasks = [{ ...TASKS[0], permission_mode: 'autoReview' }, { ...TASKS[1], permission_mode: 'dontAsk' }]
    mount()
    const chips = screen.getAllByText('Auto')
    expect(chips).toHaveLength(2)
    for (const c of chips) expect(c).toHaveAttribute('title', expect.stringContaining("engine's own reviewer"))
  })

  it("the dialog's mode is saved with the machine left alone", async () => {
    h.tasks = [{ ...TASKS[0], permission_mode: 'autoReview' }]
    mount()
    fireEvent.click(screen.getAllByRole('button', { name: 'Settings' })[0])
    expect(screen.getByTestId('task-mode')).toHaveValue('autoReview')
    fireEvent.change(screen.getByTestId('task-mode'), { target: { value: 'dontAsk' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(h.editMode).toHaveBeenCalledWith({ taskId: TASKS[0].id, mode: 'dontAsk' }))
    expect(h.editMachine).not.toHaveBeenCalled()
  })

  it('the dialog offers the default and the places for the task scope, an offline one not pickable', () => {
    mount()
    fireEvent.click(screen.getAllByRole('button', { name: 'Settings' })[0])
    expect(screen.getByText('Task settings: Daily health')).toBeInTheDocument()
    expect(h.runTargets).toHaveBeenCalledWith('hosting-admin', 'agent')
    const select = screen.getByTestId('task-runs-on') as HTMLSelectElement
    const options = Array.from(select.options).map((o) => [o.value, o.textContent, o.disabled])
    expect(options).toEqual([
      ['default', "The agent's default: OtoDock platform", false],
      ['local', 'OtoDock platform', false],
      ['m-attic', 'Attic PC', false],
      ['m-drill', 'drill-sat, offline', true],
    ])
    // The task's own machine is the current pick.
    expect(select.value).toBe('m-drill')
  })

  it('Save sends only the machine, the default as its word', async () => {
    mount()
    fireEvent.click(screen.getAllByRole('button', { name: 'Settings' })[0])
    fireEvent.change(screen.getByTestId('task-runs-on'), { target: { value: 'default' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(h.editMachine).toHaveBeenCalledWith({ taskId: TASKS[0].id, machine: 'default' }))
    await waitFor(() => expect(screen.queryByText('Task settings: Daily health')).toBeNull())
  })

  it("an unchanged pick closes without a call, and a refusal shows inline", async () => {
    mount()
    fireEvent.click(screen.getAllByRole('button', { name: 'Settings' })[0])
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByText('Task settings: Daily health')).toBeNull())
    expect(h.editMachine).not.toHaveBeenCalled()

    h.editMachine.mockRejectedValueOnce(new Error("You can't run this task there. Pick one of: Attic PC."))
    fireEvent.click(screen.getAllByRole('button', { name: 'Settings' })[0])
    fireEvent.change(screen.getByTestId('task-runs-on'), { target: { value: 'm-attic' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("You can't run this task there. Pick one of: Attic PC.")
    expect(h.editMachine).toHaveBeenCalledWith({ taskId: TASKS[0].id, machine: 'm-attic' })
    expect(screen.getByText('Task settings: Daily health')).toBeInTheDocument()
  })

  it('renders the extra field under "Runs on"', () => {
    render(
      <TaskSettingsModal agent="hosting-admin" task={TASKS[1] as unknown as Task} onClose={() => {}}
        extraFields={<p>Mode field</p>} />,
    )
    const select = screen.getByTestId('task-runs-on') as HTMLSelectElement
    // A task on the default: the default is the pick.
    expect(select.value).toBe('default')
    expect(select.compareDocumentPosition(screen.getByText('Mode field')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
