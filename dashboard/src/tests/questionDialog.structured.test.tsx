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

// The 2026-09-02 bug: long option content (options[].preview — e.g. four full
// CTA versions to choose between) was silently dropped — the card rendered
// only label + description, and the user had to re-ask in plain text.
describe('QuestionDialog — option preview content', () => {
  const LONG_PREVIEW =
    'Version 2 — full text:\n"If you want this for your own company, ' +
    'the code is available on GitHub — link in the description."\n' +
    'Line three of the long preview stays intact.'

  const CLAUDE_INPUT = {
    questions: [
      {
        header: 'CTA',
        question: 'Which version?',
        options: [
          { label: 'Version 1', description: 'short cut' },
          { label: 'Version 2', description: 'your framing', preview: LONG_PREVIEW },
        ],
      },
    ],
  }

  it('renders the full preview content for Claude questions', () => {
    render(<QuestionDialog toolInput={CLAUDE_INPUT} onAnswer={vi.fn()} />)
    expect(screen.getByText(/Line three of the long preview stays intact/)).toBeInTheDocument()
    expect(screen.getByText(/available on GitHub/)).toBeInTheDocument()
    // label + description still render alongside the preview
    expect(screen.getByText('Version 2')).toBeInTheDocument()
    expect(screen.getByText('your framing')).toBeInTheDocument()
  })

  it('renders previews on the codex structured path too', () => {
    const input = {
      questions: [{
        id: 'q1', header: 'Pick', question: 'Choose one',
        options: [{ label: 'A', description: 'a', preview: 'FULL BODY A' }],
      }],
    }
    render(
      <QuestionDialog
        toolInput={input}
        requestId="req-9"
        onAnswer={vi.fn()}
        onAnswerStructured={vi.fn()}
      />,
    )
    expect(screen.getByText('FULL BODY A')).toBeInTheDocument()
  })

  it('answer still round-trips the label when a preview was shown', () => {
    const onAnswer = vi.fn()
    render(<QuestionDialog toolInput={CLAUDE_INPUT} onAnswer={onAnswer} />)
    fireEvent.click(screen.getByText('Version 2'))
    fireEvent.click(screen.getByText('Submit'))
    expect(onAnswer).toHaveBeenCalledWith('Version 2')
  })
})
