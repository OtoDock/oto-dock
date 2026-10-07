import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
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

vi.mock('@/api/tasks', () => ({
  useTasks: () => ({ data: TASKS, isLoading: false }),
  useRunTaskNow: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
  usePauseTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useResumeTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))

import AgentSchedules from '@/pages/agent/AgentSchedules'

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
      expect(l.textContent).toBe('transferred from Pat Person')
      expect(l.getAttribute('title')).toMatch(/^Moved to its current owner on /)
    }
  })
})
