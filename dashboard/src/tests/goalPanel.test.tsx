import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import GoalPanel from '@/components/chat/plan/GoalPanel'
import type { ThreadGoal } from '@/hooks/useDashboardWs.types'

const GOAL: ThreadGoal = {
  objective: 'Ship the release',
  status: 'active',
  token_budget: 500000,
  tokens_used: 250000,
  time_used_seconds: 187,
}

describe('GoalPanel', () => {
  it('renders nothing without a goal', () => {
    const { container } = render(<GoalPanel goal={null} />)
    expect(container.innerHTML).toBe('')
  })

  it('shows the budget-% badge; expanding reveals objective, bar and time', () => {
    render(<GoalPanel goal={GOAL} />)
    fireEvent.click(screen.getByTitle('Goal: Ship the release'))
    expect(screen.getByText('Ship the release')).toBeTruthy()
    expect(screen.getByText('50%')).toBeTruthy()
    expect(screen.getByText('250k / 500k')).toBeTruthy()
    expect(screen.getByText('3m')).toBeTruthy()
  })

  it('hides a COMPLETE goal — a model "mark complete" arrives as a status update', () => {
    const { container } = render(<GoalPanel goal={{ ...GOAL, status: 'complete' }} />)
    expect(container.innerHTML).toBe('')
  })

  it('keeps a stuck goal visible with a status label', () => {
    render(<GoalPanel goal={{ ...GOAL, status: 'budgetLimited', tokens_used: 600000 }} />)
    fireEvent.click(screen.getByTitle('Goal: Ship the release'))
    expect(screen.getByText('(budget limited)')).toBeTruthy()
    expect(screen.getByText('100%')).toBeTruthy() // over-budget capped
  })

  it('no budget → no bar, tokens-used line instead; missing status = active', () => {
    render(<GoalPanel goal={{ objective: 'No budget', token_budget: null, tokens_used: 24800, time_used_seconds: 30 }} />)
    fireEvent.click(screen.getByTitle('Goal: No budget'))
    expect(screen.getByText('Tokens used')).toBeTruthy()
    expect(screen.getByText('24.8k')).toBeTruthy()
    expect(screen.queryByText('Token budget')).toBeNull()
  })
})
