import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import QuestionDialog from '@/components/chat/QuestionDialog'
import PlanView from '@/components/chat/plan/PlanView'

// ─── QuestionDialog on an interactive chat (readOnly) ────────────────────────
// A live terminal owns the dialog: the card shows WHAT is asked and points at
// the terminal — no option buttons, no text input, no submit (a card answer
// would only be held until the parked turn ends).

const toolInput = {
  questions: [
    {
      header: 'Next lane',
      question: 'Which lane do you want next?',
      options: [
        { label: 'Community profile', description: 'Issue templates, PR template.' },
        { label: 'Internal deploy', description: 'Deploy the tip first.' },
      ],
    },
  ],
}

describe('QuestionDialog readOnly', () => {
  it('renders the question and options without controls', () => {
    const onAnswer = vi.fn()
    render(<QuestionDialog toolInput={toolInput} onAnswer={onAnswer} readOnly />)
    expect(screen.getByTestId('question-readonly')).toBeTruthy()
    expect(screen.getByText('Which lane do you want next?')).toBeTruthy()
    expect(screen.getByText('Community profile')).toBeTruthy()
    expect(screen.getByText(/Answer in the terminal/)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(onAnswer).not.toHaveBeenCalled()
  })

  it('an answered question stays the answered chip even when readOnly', () => {
    render(<QuestionDialog toolInput={toolInput} onAnswer={() => {}} readOnly answered />)
    expect(screen.getByText('Questions answered')).toBeTruthy()
    expect(screen.queryByTestId('question-readonly')).toBeNull()
  })

  it('the headless card keeps its controls without the flag', () => {
    render(<QuestionDialog toolInput={toolInput} onAnswer={() => {}} />)
    expect(screen.getByRole('button', { name: /Submit/ })).toBeTruthy()
    expect(screen.queryByTestId('question-readonly')).toBeNull()
  })
})

describe('PlanView readOnly', () => {
  const plan = { plan: '# Plan\n\n1. add README', planFilePath: '/x/.claude/plans/p.md' }

  it('shows the plan with no action buttons and the terminal hint', () => {
    render(
      <PlanView action="exit" toolInput={plan} readOnly
        onImplement={() => {}} onSendMessage={() => {}} />,
    )
    expect(screen.getByTestId('plan-readonly')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Start Implementation|Edit Plan|Reject|Full Permissions/ })).toBeNull()
  })

  it('the headless card keeps its buttons without the flag', () => {
    render(<PlanView action="exit" toolInput={plan} onImplement={() => {}} onSendMessage={() => {}} />)
    expect(screen.getByRole('button', { name: /Start Implementation/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Edit Plan/ })).toBeTruthy()
    expect(screen.queryByTestId('plan-readonly')).toBeNull()
  })
})
