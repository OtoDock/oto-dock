import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import QuestionDialog from '@/components/chat/QuestionDialog'

// Codex request_user_input question set (verbatim ids the answer MUST key on).
const CODEX_INPUT = {
  questions: [
    {
      id: 'color_theme',
      header: 'Theme',
      question: 'Which theme?',
      isOther: true,
      options: [
        { label: 'Dark', description: 'dark ui' },
        { label: 'Light', description: 'light ui' },
      ],
    },
  ],
}

describe('QuestionDialog — codex structured answer', () => {
  it('keys the answer map by the VERBATIM question id and calls onAnswerStructured', () => {
    const onAnswerStructured = vi.fn()
    const onAnswer = vi.fn()
    render(
      <QuestionDialog
        toolInput={CODEX_INPUT}
        requestId="req-1"
        onAnswer={onAnswer}
        onAnswerStructured={onAnswerStructured}
      />,
    )
    fireEvent.click(screen.getByText('Dark'))
    fireEvent.click(screen.getByText('Submit'))
    // Structured path — NOT the Claude string path.
    expect(onAnswer).not.toHaveBeenCalled()
    expect(onAnswerStructured).toHaveBeenCalledWith('req-1', {
      color_theme: { answers: ['Dark'] },
    })
  })

  it('includes free-text alongside the selected label (isOther)', () => {
    const onAnswerStructured = vi.fn()
    render(
      <QuestionDialog
        toolInput={CODEX_INPUT}
        requestId="req-2"
        onAnswer={vi.fn()}
        onAnswerStructured={onAnswerStructured}
      />,
    )
    fireEvent.click(screen.getByText('Dark'))
    fireEvent.change(screen.getByPlaceholderText(/custom response/i), {
      target: { value: 'high contrast please' },
    })
    fireEvent.click(screen.getByText('Submit'))
    expect(onAnswerStructured).toHaveBeenCalledWith('req-2', {
      color_theme: { answers: ['Dark', 'high contrast please'] },
    })
  })

  it('falls back to the Claude string path when there is no requestId', () => {
    const onAnswer = vi.fn()
    const onAnswerStructured = vi.fn()
    render(
      <QuestionDialog
        toolInput={CODEX_INPUT}
        onAnswer={onAnswer}
        onAnswerStructured={onAnswerStructured}
      />,
    )
    fireEvent.click(screen.getByText('Light'))
    fireEvent.click(screen.getByText('Submit'))
    expect(onAnswerStructured).not.toHaveBeenCalled()
    expect(onAnswer).toHaveBeenCalledTimes(1)
    expect(String(onAnswer.mock.calls[0][0])).toContain('Light')
  })
})
