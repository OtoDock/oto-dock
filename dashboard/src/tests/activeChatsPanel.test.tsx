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
      { name: 'alpha', display_name: 'Alpha Agent', color: '#10B981', collaborative: true, default_scope: 'user' },
      // Shared-only mode (collaborative false + agent scope).
      { name: 'beta', display_name: 'Beta Agent', color: '#3B82F6', collaborative: false, default_scope: 'agent' },
      // Meta present but the visibility fields are absent (stale payload).
      { name: 'delta', display_name: 'Delta Agent', color: '#F59E0B' },
    ],
  }),
}))

describe('ActiveChatsPanel', () => {
  beforeEach(() => {
    navigateMock.mockClear()
    rowsMock.mockReturnValue([])
    // The collapse prefs persist in localStorage — isolate every test.
    localStorage.clear()
  })

  const expandHome = () =>
    fireEvent.click(screen.getByRole('button', { name: /Active now/ }))

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
    expect(screen.getByRole('button', { name: /Active now · 2/ })).toBeTruthy()
    expect(screen.getByText('Alpha Agent')).toBeTruthy()
    expect(screen.getByText('Beta Agent')).toBeTruthy()
    expect(screen.getByTitle('Generating response…').className).toContain('oto-row-live')
    expect(screen.getByTitle('Preparing session…')).toBeTruthy()
  })

  it('chat view: own chats dedup into the history list, own live TASKS stay', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'alpha', title: 'Doubled in history', phase: 'streaming' },
      { id: 't1', agent: 'alpha', title: 'Nightly digest', phase: 'streaming', sourceType: 'task' },
      { id: 't2', agent: 'beta', title: 'Foreign task', phase: 'streaming', sourceType: 'task' },
    ])
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />)
    // Symmetric suppression: hide only what the list below already shows.
    // The chat list shows CHATS — own live tasks have no other row on screen
    // (the pulsing tasks toggle doesn't say WHICH task), so they stay.
    expect(screen.queryByText('Doubled in history')).toBeNull()
    expect(screen.getByText('Nightly digest')).toBeTruthy()
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

  it('sidebar dedup truth table — both modes, undefined sourceType, viewed chat', () => {
    const rows = [
      { id: 'own-chat', agent: 'alpha', title: 'Own chat', phase: 'streaming' },
      // No sourceType (store-only row, no seed yet): treated as a plain chat.
      { id: 'own-untyped', agent: 'alpha', title: 'Own untyped', phase: 'streaming', sourceType: undefined },
      { id: 'own-task', agent: 'alpha', title: 'Own task', phase: 'streaming', sourceType: 'task' },
      // Own-agent finished-linger task: flashes ~4s in chat mode — accepted
      // cosmetic of the symmetric rule (it is not in the chat list either).
      { id: 'own-task-done', agent: 'alpha', title: 'Own done task', phase: 'finished', sourceType: 'task' },
      { id: 'f-chat', agent: 'beta', title: 'Foreign chat', phase: 'streaming' },
      { id: 'f-task', agent: 'beta', title: 'Foreign task', phase: 'streaming', sourceType: 'task' },
      { id: 'viewed', agent: 'beta', title: 'Viewed chat', phase: 'streaming' },
    ]
    rowsMock.mockReturnValue(rows)
    const chatMode = render(
      <ActiveChatsPanel currentAgent="alpha" activeChatId="viewed" onSelect={() => {}} />,
    )
    expect(screen.queryByText('Own chat')).toBeNull()
    expect(screen.queryByText('Own untyped')).toBeNull()
    expect(screen.getByText('Own task')).toBeTruthy()
    expect(screen.getByText('Own done task')).toBeTruthy()
    expect(screen.getByText('Foreign chat')).toBeTruthy()
    expect(screen.getByText('Foreign task')).toBeTruthy()
    expect(screen.queryByText('Viewed chat')).toBeNull()
    chatMode.unmount()

    render(
      <ActiveChatsPanel currentAgent="alpha" activeChatId="viewed" onSelect={() => {}} tasksMode />,
    )
    expect(screen.getByText('Own chat')).toBeTruthy()
    expect(screen.getByText('Own untyped')).toBeTruthy()
    expect(screen.queryByText('Own task')).toBeNull()
    expect(screen.queryByText('Own done task')).toBeNull()
    expect(screen.getByText('Foreign chat')).toBeTruthy()
    expect(screen.getByText('Foreign task')).toBeTruthy()
    expect(screen.queryByText('Viewed chat')).toBeNull()
  })

  it('orphan exemption: shared-owner chat stays in chat mode when the agent is no longer shared-only', () => {
    rowsMock.mockReturnValue([
      // Legacy `agent::`-owned chat on a now-personal agent: the chat list
      // below filters it out, so the strip is its only surface — keep it.
      { id: 'orph', agent: 'alpha', title: 'Legacy shared chat', phase: 'streaming', ownerIsShared: true },
      { id: 'own', agent: 'alpha', title: 'Own plain chat', phase: 'streaming' },
    ])
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />)
    expect(screen.getByText('Legacy shared chat')).toBeTruthy()
    expect(screen.queryByText('Own plain chat')).toBeNull()
  })

  it('orphan exemption does not apply to shared-only agents (their list shows shared chats)', () => {
    rowsMock.mockReturnValue([
      { id: 'orph', agent: 'beta', title: 'Shared-mode chat', phase: 'streaming', ownerIsShared: true },
    ])
    const { container } = render(
      <ActiveChatsPanel currentAgent="beta" activeChatId={null} onSelect={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('task mode keeps shared-owner TASK rows suppressed (task list filters on run scope, not owner)', () => {
    rowsMock.mockReturnValue([
      { id: 'task-run-5', agent: 'alpha', title: 'Shared-owner task', phase: 'streaming', sourceType: 'task', ownerIsShared: true },
    ])
    const { container } = render(
      <ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} tasksMode />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('agent meta without the visibility fields suppresses the orphan (no widest-mode fallback)', () => {
    rowsMock.mockReturnValue([
      { id: 'orph', agent: 'delta', title: 'Fieldless orphan', phase: 'streaming', ownerIsShared: true },
    ])
    const { container } = render(
      <ActiveChatsPanel currentAgent="delta" activeChatId={null} onSelect={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('section unmounts only when truly empty after the dedup', () => {
    // Chat mode with nothing but own chats → every row is deduped into the
    // list below → the whole section renders nothing.
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'alpha', title: 'Own chat', phase: 'streaming' },
    ])
    const { container } = render(
      <ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
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
    expandHome()
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
    expandHome()
    expect(screen.getByText('Fix the tests')).toBeTruthy()

    rowsMock.mockReturnValue([])
    const { container } = render(
      <ActiveChatsPanel variant="home" currentAgent="alpha" activeChatId={null} onSelect={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('renders every row inside a height-capped scroll container (no +N more)', () => {
    rowsMock.mockReturnValue(
      Array.from({ length: 8 }, (_, i) => ({
        id: `c${i}`, agent: 'beta', title: `Chat ${i}`, phase: 'streaming',
      })),
    )
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />)
    // All 8 rows are in the DOM — overflow scrolls instead of truncating.
    expect(screen.getByText('Chat 7')).toBeTruthy()
    expect(screen.queryByText(/more$/)).toBeNull()
    const container = screen.getByText('Chat 0').closest('.overflow-y-auto')!
    expect(container).toBeTruthy()
    expect(container.className).toContain('max-h-34')
  })

  it('home variant starts collapsed with a live dot; expanding persists', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'beta', title: 'Hidden until expanded', phase: 'streaming' },
    ])
    const first = render(
      <ActiveChatsPanel variant="home" currentAgent="alpha" activeChatId={null} onSelect={() => {}} />,
    )
    const header = screen.getByRole('button', { name: /Active now · 1/ })
    expect(header.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('Hidden until expanded')).toBeNull()
    // Collapsed + something generating → the header carries the pulse dot.
    expect(header.querySelector('span.animate-pulse')).toBeTruthy()
    fireEvent.click(header)
    expect(screen.getByText('Hidden until expanded')).toBeTruthy()
    expect(localStorage.getItem('active-now-home')).toBe('open')
    first.unmount()
    // A fresh mount honors the persisted choice.
    render(
      <ActiveChatsPanel variant="home" currentAgent="alpha" activeChatId={null} onSelect={() => {}} />,
    )
    expect(screen.getByText('Hidden until expanded')).toBeTruthy()
  })

  it('sidebar variant starts expanded; collapsing persists', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'beta', title: 'Visible by default', phase: 'streaming' },
    ])
    render(<ActiveChatsPanel currentAgent="alpha" activeChatId={null} onSelect={() => {}} />)
    expect(screen.getByText('Visible by default')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Active now/ }))
    expect(screen.queryByText('Visible by default')).toBeNull()
    expect(localStorage.getItem('active-now-sidebar')).toBe('closed')
  })

  it('home variant lays rows out two-up on md+ (grid classes)', () => {
    rowsMock.mockReturnValue([
      { id: 'c1', agent: 'beta', title: 'Grid row', phase: 'streaming' },
    ])
    localStorage.setItem('active-now-home', 'open')
    render(
      <ActiveChatsPanel variant="home" currentAgent="alpha" activeChatId={null} onSelect={() => {}} />,
    )
    const container = screen.getByText('Grid row').closest('.overflow-y-auto')!
    expect(container.className).toContain('md:grid-cols-2')
    expect(container.className).toContain('md:max-h-23')
  })
})
