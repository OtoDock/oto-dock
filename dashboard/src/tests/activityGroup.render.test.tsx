/**
 * Compact activity view — rendering. Runs of tool/thinking blocks collapse
 * into one chip by default (expandable to the real cards); permission and
 * question blocks always render outside groups; Detailed mode restores the
 * historical flat layout; the Appearance toggle flips live; an active search
 * keeps a collapsed group's thinking MOUNTED (hidden) so match order holds.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatMessages from '@/components/chat/ChatMessages'
import { AppearanceSection } from '@/pages/UserSettings.general'
import { SearchProvider } from '@/contexts/SearchContext'
import type { DisplayMessage, MessageBlock } from '@/components/chat/types'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)

// jsdom persists localStorage across tests within the file — the display
// pref must start from its default (compact) every time.
beforeEach(() => {
  localStorage.clear()
})

const readTool = (path = '/tmp/report.xlsx', over: Partial<Extract<MessageBlock, { type: 'tool' }>> = {}): MessageBlock =>
  ({ type: 'tool', name: 'Read', toolId: 't1', summary: '', status: 'done', toolInput: { file_path: path }, ...over })

function msg(blocks: MessageBlock[], over: Partial<DisplayMessage> = {}): DisplayMessage {
  return {
    id: 'db-1',
    role: 'assistant',
    blocks,
    createdAt: '2026-08-06T00:00:00+00:00',
    ...over,
  }
}

function renderChat(messages: DisplayMessage[], extra?: React.ReactNode, searchQuery = '') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SearchProvider query={searchQuery}>
        {extra}
        <ChatMessages
          messages={messages}
          agentName="dev-agent"
          onPermissionRespond={() => {}}
        />
      </SearchProvider>
    </QueryClientProvider>,
  )
}

describe('ActivityGroup — compact default', () => {
  it('collapses a tool run into a chip with the step count', () => {
    renderChat([msg([
      readTool('/tmp/a.txt'),
      readTool('/tmp/b.txt', { toolId: 't2' }),
      { type: 'text', content: 'all done' },
    ])])
    const chip = screen.getByTestId('activity-chip')
    expect(chip).toHaveTextContent('2 steps')
    // The real tool cards are NOT mounted while collapsed.
    expect(screen.queryByText('Read')).toBeNull()
    expect(screen.queryByText('/tmp/a.txt')).toBeNull()
    // Surrounding text renders as today.
    expect(screen.getByText('all done')).toBeInTheDocument()
  })

  it('expands to the real ToolActivity cards on click', () => {
    renderChat([msg([
      readTool('/tmp/a.txt'),
      readTool('/tmp/b.txt', { toolId: 't2' }),
    ])])
    fireEvent.click(screen.getByTestId('activity-chip'))
    expect(screen.getByTestId('activity-group-expanded')).toBeInTheDocument()
    expect(screen.getAllByText('Read')).toHaveLength(2)
    expect(screen.getByText('/tmp/a.txt')).toBeInTheDocument()
    expect(screen.getByText('/tmp/b.txt')).toBeInTheDocument()
    // Collapse again from the chip.
    fireEvent.click(screen.getByTestId('activity-chip'))
    expect(screen.queryByText('Read')).toBeNull()
  })

  it('shows the failed count with the amber accent', () => {
    renderChat([msg([
      readTool('/tmp/a.txt', { status: 'failed' }),
      readTool('/tmp/b.txt', { toolId: 't2' }),
    ])])
    const chip = screen.getByTestId('activity-chip')
    expect(chip).toHaveTextContent('2 steps')
    const failed = screen.getByText('· 1 failed')
    expect(failed.className).toContain('text-p-accent-yellow')
  })

  it('labels a thinking-only run "Thought" and keeps the content unmounted', () => {
    renderChat([msg([
      { type: 'thinking', content: 'pondering deeply about xlsx', collapsed: true, done: true },
    ])])
    expect(screen.getByTestId('activity-chip')).toHaveTextContent('Thought')
    expect(screen.queryByText(/pondering deeply/)).toBeNull()
  })

  it('renders permission and question blocks OUTSIDE groups, visible while collapsed', () => {
    renderChat([msg([
      readTool('/tmp/a.txt'),
      { type: 'permission', requestId: 'r1', toolName: 'Bash', toolInput: { command: 'rm -rf /tmp/x' } },
      readTool('/tmp/b.txt', { toolId: 't2' }),
      { type: 'question', toolName: 'AskUserQuestion', toolInput: { question: 'Which color?' } },
      readTool('/tmp/c.txt', { toolId: 't3' }),
    ])])
    // Three single-step runs split by the permission + question blocks.
    const chips = screen.getAllByTestId('activity-chip')
    expect(chips).toHaveLength(3)
    for (const chip of chips) expect(chip).toHaveTextContent('1 step')
    // The interactive cards render flat and stay actionable.
    expect(screen.getByRole('button', { name: 'Allow' })).toBeInTheDocument()
    expect(screen.getByText('Which color?')).toBeInTheDocument()
    // The grouped tool cards themselves stay collapsed.
    expect(screen.queryByText('Read')).toBeNull()
  })
})

describe('ActivityGroup — detailed mode', () => {
  it('renders the flat layout with no chips', () => {
    localStorage.setItem('activity-display', 'detailed')
    renderChat([msg([
      readTool('/tmp/a.txt'),
      readTool('/tmp/b.txt', { toolId: 't2' }),
    ])])
    expect(screen.queryByTestId('activity-chip')).toBeNull()
    expect(screen.getAllByText('Read')).toHaveLength(2)
  })

  it('flips live from the Appearance toggle', () => {
    renderChat([msg([readTool('/tmp/a.txt')])], <AppearanceSection />)
    // Compact default: chip, no card.
    expect(screen.getByTestId('activity-chip')).toBeInTheDocument()
    expect(screen.queryByText('Read')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Detailed' }))
    expect(screen.queryByTestId('activity-chip')).toBeNull()
    expect(screen.getByText('Read')).toBeInTheDocument()
    expect(localStorage.getItem('activity-display')).toBe('detailed')

    fireEvent.click(screen.getByRole('button', { name: 'Compact' }))
    expect(screen.getByTestId('activity-chip')).toBeInTheDocument()
    expect(screen.queryByText('Read')).toBeNull()
  })
})

describe('ActivityGroup — active search', () => {
  it('keeps a collapsed group\'s thinking mounted (hidden) so matches register', () => {
    renderChat(
      [msg([
        readTool('/tmp/a.txt'),
        { type: 'thinking', content: 'the needle in the haystack', collapsed: true, done: true },
      ])],
      undefined,
      'needle',
    )
    // Visually still collapsed: chip present, tool card absent.
    expect(screen.getByTestId('activity-chip')).toBeInTheDocument()
    expect(screen.queryByText('Read')).toBeNull()
    // The thinking block is mounted inside the hidden search container and
    // its SearchHighlight marks register in the DOM.
    const mount = screen.getByTestId('activity-group-search-mount')
    expect(mount.className).toContain('hidden')
    const mark = mount.querySelector('[data-search-match]')
    expect(mark).not.toBeNull()
    expect(mark!.textContent).toBe('needle')
  })

  it('does not mount hidden thinking without a query', () => {
    renderChat([msg([
      readTool('/tmp/a.txt'),
      { type: 'thinking', content: 'the needle in the haystack', collapsed: true, done: true },
    ])])
    expect(screen.queryByTestId('activity-group-search-mount')).toBeNull()
  })
})
