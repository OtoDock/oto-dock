import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

// ─── The model chip on both schedules pages ─────────────────────────────────
// Display only: agents set the pin through the schedules MCP. The chip's job
// is to make "this task runs on something else" visible at a glance, and to
// never leave a reader guessing whether a blank cell means default or unknown.

import { TaskModelChip } from '@/components/common/TaskModelChip'
import type { Task } from '@/api/tasks'

function task(over: Partial<Task> = {}): Task {
  return {
    id: 'dyn-1', name: 'Daily briefing', agent: 'briefer',
    schedule: '0 7 * * *', run_at: null, delay_seconds: null,
    interval_seconds: null, llm_mode: 'cli', prompt: 'brief me',
    enabled: true, timeout_seconds: 600, next_run_time: null,
    scope: 'user', created_by: 'user-1',
    notification_mode: 'none', notify_severity: 'info',
    override_model: null, override_execution_path: null,
    effective_model: 'claude-sonnet-5', effective_execution_path: 'claude-code-cli',
    can_run: true, can_delete: true, can_pause: true, can_resume: false,
    ...over,
  }
}

describe('TaskModelChip', () => {
  it('renders the agent default muted, with no engine suffix', () => {
    render(<TaskModelChip task={task()} />)
    const chip = screen.getByTitle('Agent default: claude-sonnet-5')
    expect(chip.textContent).toBe('claude-sonnet-5')
    expect(chip.className).not.toContain('text-brand')
  })

  it('brands the chip when the task pins its own model', () => {
    render(<TaskModelChip task={task({
      override_model: 'claude-opus-5', effective_model: 'claude-opus-5',
    })} />)
    const chip = screen.getByTitle(/^Pinned to claude-opus-5/)
    expect(chip.textContent).toContain('claude-opus-5')
    expect(chip.className).toContain('text-brand')
  })

  it('appends the engine only when the task pins the layer too', () => {
    render(<TaskModelChip task={task({
      override_model: 'hosted-mini', effective_model: 'hosted-mini',
      override_execution_path: 'direct-llm',
      effective_execution_path: 'direct-llm',
    })} />)
    expect(screen.getByTitle(/on direct-llm/).textContent).toContain('direct-llm')
  })

  it('renders nothing when the install could not resolve a model', () => {
    const { container } = render(<TaskModelChip task={task({ effective_model: '' })} />)
    expect(container.firstChild).toBeNull()
  })
})
