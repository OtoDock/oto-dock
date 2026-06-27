import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ActiveChatsPanel from '@/components/chat/ActiveChatsPanel'

// ─── ActiveChatsPanel (render logic; hook + agents mocked) ──────────────────

const navigateMock = vi.fn()
vi.mock('react-router-dom', () => ({ useNavigate: () => navigateMock }))

const rowsMock = vi.fn<() => unknown[]>(() => [])
vi.mock('@/hooks/useActiveChats', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/hooks/useActiveChats')>()
  return { ...orig, useActiveChats: () => rowsMock() }
})

vi.mock('@/api/agents', () => ({
  useAgents: () => ({
    data: [
      { name: 'alpha', display_name: 'Alpha Agent', color: '#10B981' },
      { name: 'beta', display_name: 'Beta Agent', color: '#3B82F6' },
    ],
  }),
}))

describe('ActiveChatsPanel', () => {
  beforeEach(() => {
    navigateMock.mockClear()
    rowsMock.mockReturnValue([])
  })

  it('renders nothing when no chats are active', () => {
    const { container } = render(
      <ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('shows foreign-agent rows with display names and pulse on streaming', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'alpha', title: 'Fix the tests', phase: 'streaming' },
      { id: 'c2', agent: 'beta', title: 'Morning brief', phase: 'warming' },
    ])
    render(<ActiveChatsPanel currentAgent="gamma" activeChatId={null} onSelect={() => {}} />)
    expect(screen.getByText('Active now')).toBeTruthy()
    expect(screen.getByText('Alpha Agent')).toBeTruthy()
    expect(screen.getByText('Beta Agent')).toBeTruthy()
    expect(screen.getByTitle('Generating response…').className).toContain('oto-row-live')
    expect(screen.getByTitle('Preparing session…')).toBeTruthy()
  })

  it('sidebar hides ALL of the current agent\'s own rows — chats and tasks alike', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'alpha', title: 'Doubled in history', phase: 'streaming' },
      { id: 't1', agent: 'alpha', title: 'Nightly digest', phase: 'streaming', sourceType: 'task' },
      { id: 't2', agent: 'beta', title: 'Foreign task', phase: 'streaming', sourceType: 'task' },
    ])
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />)
    // Chats live in the history list below; tasks live in the Task history
    // view (the toggle carries their pulse) — only foreign rows remain.
    expect(screen.queryByText('Doubled in history')).toBeNull()
    expect(screen.queryByText('Nightly digest')).toBeNull()
    expect(screen.getByText('Foreign task')).toBeTruthy()
  })

  it('task view flips the dedup: own live chats show, own tasks stay in the list below', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'alpha', title: 'Own live chat', phase: 'streaming' },
      { id: 't1', agent: 'alpha', title: 'Own task', phase: 'streaming', sourceType: 'task' },
      { id: 't2', agent: 'beta', title: 'Foreign task', phase: 'streaming', sourceType: 'task' },
    ])
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} tasksMode />)
    expect(screen.getByText('Own live chat')).toBeTruthy()
    expect(screen.queryByText('Own task')).toBeNull()
    expect(screen.getByText('Foreign task')).toBeTruthy()
  })

  it('excludes the currently-viewed chat (viewing it IS watching it)', () => {
    rowsMock.mockReturnValue([
      { id: 'c-viewed', agent: 'beta', title: 'On screen', phase: 'streaming' },
    ])
    const { container } = render(
      <ActiveChatsPanel currentAgent="alpha" activeChatId="c-viewed" onSelect={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('home variant: same-agent click selects in place; sidebar foreign click navigates', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'alpha', title: 'Local', phase: 'streaming' },
      { id: 'c2', agent: 'beta', title: 'Remote', phase: 'streaming' },
    ])
    const onSelect = vi.fn()
    render(
      <ActiveChatsPanel variant="home" currentAgent="alpha" activeChatId={null} onSelect={onSelect} />,
    )
    fireEvent.click(screen.getByText('Local'))
    expect(onSelect).toHaveBeenCalledWith('c1')
    expect(navigateMock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Remote'))
    expect(navigateMock).toHaveBeenCalledWith('/chat/beta/c2')
  })

  it('task rows are purple, labeled, and open the chat page in task mode', () => {
    rowsMock.mockReturnValue([
      { id: 'task-run-42', agent: 'beta', title: 'Nightly digest', phase: 'streaming', sourceType: 'task' },
    ])
    const onSelect = vi.fn()
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={onSelect} />)
    const row = screen.getByTitle('Task running…')
    expect(row.className).toContain('ring-p-accent-purple/40')
    expect(row.className).toContain('oto-row-live-purple')
    expect(screen.getByText('Beta Agent · task')).toBeTruthy()
    fireEvent.click(screen.getByText('Nightly digest'))
    // Task runs render on the chat page — the row deep-links with task mode on.
    expect(onSelect).not.toHaveBeenCalled()
    expect(navigateMock).toHaveBeenCalledWith('/chat/beta/task-run-42?tasks=1')
  })

  it('finished task rows never carry the unread dot', () => {
    rowsMock.mockReturnValue([
      { id: 'task-run-9', agent: 'beta', title: 'Done task', phase: 'finished', sourceType: 'task' },
    ])
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />)
    const row = screen.getByTitle('Task finished')
    expect(row.querySelector('span.w-2.h-2')).toBeNull()
  })

  it('unified live language: streaming pulses without a dot; finished holds the tint with a dot', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'beta', title: 'Streaming row', phase: 'streaming' },
      { id: 'c2', agent: 'beta', title: 'Finished row', phase: 'finished' },
    ])
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />)
    const streaming = screen.getByTitle('Generating response…')
    expect(streaming.className).toContain('oto-row-live')
    expect(streaming.querySelector('span.w-2.h-2')).toBeNull()
    const finished = screen.getByTitle('Finished — not opened yet')
    expect(finished.className).toContain('bg-brand-surface')
    expect(finished.className).not.toContain('oto-row-live')
    expect(finished.querySelector('span.w-2.h-2')).toBeTruthy()
  })

  it('home variant keeps the current agent\'s chats (no chat list to duplicate against)', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'alpha', title: 'Fix the tests', phase: 'streaming' },
    ])
    render(
      <ActiveChatsPanel variant="home" currentAgent="alpha" activeChatId={null} onSelect={() => {}} />,
    )
    expect(screen.getByTestId('active-chats-home')).toBeTruthy()
    expect(screen.queryByTestId('active-chats-panel')).toBeNull()
    expect(screen.getByText('Fix the tests')).toBeTruthy()

    rowsMock.mockReturnValue([])
    const { container } = render(
      <ActiveChatsPanel variant="home" currentAgent="alpha" activeChatId={null} onSelect={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('caps visible rows and expands via +N more', () => {
    rowsMock.mockReturnValue(
      Array.from({ length: 8 }, (_, i) => ({
        id: `c${i}`, agent: 'beta', title: `Chat ${i}`, phase: 'streaming',
      })),
    )
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />)
    expect(screen.queryByText('Chat 7')).toBeNull()
    fireEvent.click(screen.getByText('+2 more'))
    expect(screen.getByText('Chat 7')).toBeTruthy()
  })
})
