import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// ─── The admin Scheduled Tasks page: a one-shot whose time passed with
//     nothing left to run it is never shown as active ───

const h = vi.hoisted(() => ({ tasks: [] as unknown[] }))

vi.mock('@/api/tasks', () => ({
  useTasks: () => ({ data: h.tasks, isLoading: false }),
  useRunTaskNow: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
  usePauseTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useResumeTask: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))
vi.mock('@/api/agents', () => ({ useAgents: () => ({ data: [] }) }))
vi.mock('@/api/runs', () => ({ useAdminUsers: () => ({ data: [] }) }))

import Schedules from '@/pages/Schedules'

const ROW = {
  id: 'dyn-dead0001', name: 'Gate wake', agent: 'otodock-developer', schedule: '', run_at: null,
  delay_seconds: 2280, interval_seconds: null, llm_mode: 'cli', prompt: 'check the gate', enabled: true,
  timeout_seconds: 600, next_run_time: null, scope: 'agent', created_by: 'u1', task_type: 'continuation',
  notification_mode: 'none', notify_severity: 'info', override_model: null, override_execution_path: null,
  effective_model: '', effective_execution_path: '', can_run: true, can_delete: true, can_pause: true,
  can_resume: false,
}

describe('Schedules', () => {
  it('shows a one-shot that will not run as such, on the table and the card', () => {
    h.tasks = [{ ...ROW, will_not_fire: true }, { ...ROW, id: 'dyn-live0001', name: 'Watchdog' }]
    render(<MemoryRouter><Schedules /></MemoryRouter>)
    expect(screen.getAllByText('Will not run')).toHaveLength(2)
    expect(screen.getAllByText('Active')).toHaveLength(2)
  })
})
