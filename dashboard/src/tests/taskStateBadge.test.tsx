import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TaskStateBadge } from '../components/common/TaskStateBadge'
import { taskState } from '../components/common/TaskStateBadge.helpers'

// ─── A task row's state: a one-shot whose time passed with nothing left to
//     run it is never shown as active ───

describe('TaskStateBadge', () => {
  it('names a row that will not run, with what to do about it', () => {
    render(<TaskStateBadge task={{ enabled: true, will_not_fire: true }} />)
    const badge = screen.getByText('Will not run')
    expect(badge.getAttribute('title')).toBe('Its time has passed and nothing will run it. Run it now, or delete it.')
    expect(badge.className).toContain('text-status-warning-fg')
  })

  it('keeps active and paused as they were, an older proxy included', () => {
    expect(taskState({ enabled: true, will_not_fire: false }).label).toBe('Active')
    expect(taskState({ enabled: true }).label).toBe('Active')
    expect(taskState({ enabled: false, will_not_fire: true }).label).toBe('Paused')
    render(<TaskStateBadge task={{ enabled: true }} />)
    expect(screen.getByText('Active').getAttribute('title')).toBeNull()
  })
})
