/**
 * The cost above the composer is the money the chat has cost (an API key's
 * or the relay's turns and every tool fee; the caller passes that sum, never
 * a subscription's activity estimate): a round dollar icon that opens the
 * amount, hidden when nothing is billed and in interactive mode.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import ChatStatusBar from '@/components/chat/ChatStatusBar'

function renderBar(over: Partial<Parameters<typeof ChatStatusBar>[0]> = {}) {
  return render(
    <ChatStatusBar
      streaming={false}
      warming={false}
      startTime={null}
      thinkingActive={false}
      compressingActive={false}
      activeAgents={[]}
      mode="ask"
      model="claude-sonnet-5"
      costUsd={1.23}
      contextUsed={0}
      contextMax={0}
      onModeChange={() => {}}
      onModelChange={() => {}}
      {...over}
    />,
  )
}

describe('ChatStatusBar cost', () => {
  it('is hidden when nothing is billed', () => {
    renderBar({ costUsd: 0, costBilled: false })
    expect(screen.queryByRole('button', { name: /^Cost/ })).toBeNull()
  })

  it('is a round icon named with the amount, and opens the amount on a click', () => {
    renderBar({ costBilled: true })
    const button = screen.getByRole('button', { name: 'Cost $1.23' })
    expect(button.className).toContain('rounded-full')
    expect(screen.queryByText('$1.23')).toBeNull()
    fireEvent.click(button)
    expect(screen.getByText('$1.23')).toBeInTheDocument()
    expect(screen.queryByText('Tools')).toBeNull()
  })

  it('splits the model and the tools, and says so when only tool fees are billed', () => {
    renderBar({ costUsd: 0.24, toolCostUsd: 0.04, costBilled: true })
    fireEvent.click(screen.getByRole('button', { name: 'Cost $0.24' }))
    expect(screen.getByText('Model').nextSibling).toHaveTextContent('$0.20')
    expect(screen.getByText('Tools').nextSibling).toHaveTextContent('$0.04')
    expect(screen.queryByText(/runs on a subscription/)).toBeNull()
  })

  it('a subscription chat with a tool fee shows the fee and the note', () => {
    renderBar({ costUsd: 0.04, toolCostUsd: 0.04, costBilled: false })
    fireEvent.click(screen.getByRole('button', { name: 'Cost $0.04' }))
    // The headline and the Tools row carry the same figure.
    expect(screen.getAllByText('$0.04')).toHaveLength(2)
    expect(screen.queryByText('Model')).toBeNull()
    expect(screen.getByText('Tools').nextSibling).toHaveTextContent('$0.04')
    expect(screen.getByText(/runs on a subscription/)).toBeInTheDocument()
  })

  it('stays hidden in interactive mode', () => {
    renderBar({ costBilled: true, interactiveActive: true })
    expect(screen.queryByRole('button', { name: /^Cost/ })).toBeNull()
  })
})
