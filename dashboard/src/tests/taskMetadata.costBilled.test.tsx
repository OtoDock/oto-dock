/**
 * The task-run popup's Cost block is the chat's own total (chats.total_cost),
 * pinned in the same viewport as the gauge — so it follows the same rule:
 * hidden when the run's chat runs on a subscription or a local model
 * (`costBilled` false), shown otherwise and when the flag is unknown.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import TaskMetadata from '@/components/chat/TaskMetadata'
import type { Run } from '@/api/runs'

const run: Run = {
  id: 'run-1', task_id: 'task-1', agent: 'dev', trigger_type: 'schedule', trigger_source: null,
  status: 'completed', started_at: '2026-09-08T00:00:00+00:00', completed_at: '2026-09-08T00:01:00+00:00',
  duration_ms: 60000, prompt_preview: 'do it', prompt_text: 'do it', output_text: 'done',
  error_message: null, session_id: 's1', task_type: null, cost_usd: 0.5, chat_id: 'task-run-1',
  session_cost_usd: 1.2, session_turn_count: 3,
}

function open(costBilled?: boolean) {
  render(<TaskMetadata run={run} costBilled={costBilled} />)
  fireEvent.click(screen.getByTitle('Task run: completed'))
}

describe('TaskMetadata cost block', () => {
  it('is hidden on a subscription / local-model chat', () => {
    open(false)
    expect(screen.queryByText('Cost')).toBeNull()
    expect(screen.queryByText(/\$1\.2000/)).toBeNull()
    expect(screen.getByText('Duration')).toBeInTheDocument()  // the rest of the panel stays
  })

  it('is shown on an API-key / relay chat and when the flag is unknown', () => {
    open(true)
    expect(screen.getByText(/\$1\.2000/)).toBeInTheDocument()
    expect(screen.getByText(/This turn: \$0\.5000/)).toBeInTheDocument()
  })

  it('is shown when the flag is unknown', () => {
    open(undefined)
    expect(screen.getByText(/\$1\.2000/)).toBeInTheDocument()
  })
})
