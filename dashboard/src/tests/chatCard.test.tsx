// The frame every card in the chat shares (components/ui/ChatCard): the
// title, a small line under it, the body, the state line in the controls'
// place and the footer row, each drawn only when given, inside the one
// shell the test id names.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import ChatCard from '@/components/ui/ChatCard'

describe('ChatCard', () => {
  it('draws the title alone in the card shell', () => {
    render(<ChatCard title="Wake word" testId="demo-card" />)
    const card = screen.getByTestId('demo-card')
    expect(card.className).toContain('max-w-md')
    expect(card.className).toContain('rounded-card')
    expect(screen.getByText('Wake word').className).toContain('font-medium')
    expect(screen.queryByTestId('demo-card-sub')).toBeNull()
    expect(screen.queryByTestId('demo-card-line')).toBeNull()
    expect(card.children).toHaveLength(1)
  })

  it('fills the slots in their order: sub under the title, body, line, footer', () => {
    render(
      <ChatCard title="Your scheduled tasks" sub="3 tasks" line="Reading the list." testId="demo-card"
        footer={<a href="/agents/alpha/scheduled-tasks">Open the page</a>}>
        <p>Body</p>
      </ChatCard>,
    )
    const card = screen.getByTestId('demo-card')
    const texts = Array.from(card.querySelectorAll('p, a')).map((n) => n.textContent)
    expect(texts).toEqual(['Your scheduled tasks', '3 tasks', 'Body', 'Reading the list.', 'Open the page'])
    expect(screen.getByTestId('demo-card-sub').className).toContain('text-tertiary')
    expect(screen.getByTestId('demo-card-line').className).toContain('text-secondary')
    expect(screen.getByText('Open the page').parentElement?.className).toContain('flex-wrap')
  })

  it('takes a node as the title, so a card keeps its info button beside the label', () => {
    render(<ChatCard title={<><span>Theme</span><button type="button" aria-label="About Theme" /></>} testId="demo-card" />)
    expect(screen.getByRole('button', { name: 'About Theme' }).parentElement?.textContent).toBe('Theme')
  })
})
