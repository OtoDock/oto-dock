/**
 * Compact activity view — rendering. Runs of tool/thinking blocks collapse
 * into one chip by default (expandable to the real cards); permission and
 * question blocks always render outside groups; Detailed mode restores the
 * historical flat layout; the Appearance toggle flips live; an active search
 * keeps a collapsed group's thinking MOUNTED (hidden) so match order holds.
 */
import { useState } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatMessages from '@/components/chat/ChatMessages'
import { AppearanceSection } from '@/pages/UserSettings.general'
import { SEGMENT } from '@/components/ui/classes'
import { ThemeContext, type Theme } from '@/contexts/useTheme'
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

  it('shows the failed count in the warning colour', () => {
    renderChat([msg([
      readTool('/tmp/a.txt', { status: 'failed' }),
      readTool('/tmp/b.txt', { toolId: 't2' }),
    ])])
    const chip = screen.getByTestId('activity-chip')
    expect(chip).toHaveTextContent('2 steps')
    const failed = screen.getByText('· 1 failed')
    expect(failed.className).toContain('text-status-warning-fg')
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

  it('the Appearance pickers are named groups whose pressed button follows the choice', () => {
    // The theme's own state, without ThemeProvider's system query (jsdom has
    // no matchMedia).
    function ThemeHost({ children }: { children: React.ReactNode }) {
      const [theme, setTheme] = useState<Theme>('system')
      return <ThemeContext.Provider value={{ theme, setTheme, resolvedTheme: 'light' }}>{children}</ThemeContext.Provider>
    }
    renderChat([], <ThemeHost><AppearanceSection /></ThemeHost>)
    const pressed = (group: HTMLElement) =>
      within(group).getAllByRole('button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent)
    const theme = screen.getByRole('group', { name: 'Theme' })
    expect(pressed(theme)).toEqual(['System'])
    fireEvent.click(within(theme).getByRole('button', { name: 'Dark' }))
    expect(pressed(theme)).toEqual(['Dark'])
    expect(within(theme).getByRole('button', { name: 'Dark' }).className).toContain(SEGMENT.on)
    expect(within(theme).getByRole('button', { name: 'Light' }).className).toContain(SEGMENT.off)
    fireEvent.click(within(theme).getByRole('button', { name: 'System' }))
    const activity = screen.getByRole('group', { name: 'Chat activity' })
    expect(pressed(activity)).toEqual(['Compact'])
    fireEvent.click(within(activity).getByRole('button', { name: 'Detailed' }))
    expect(pressed(activity)).toEqual(['Detailed'])
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

function renderStreaming(messages: DisplayMessage[], streaming = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = (msgs: DisplayMessage[], live: boolean) => (
    <QueryClientProvider client={qc}>
      <SearchProvider query="">
        <ChatMessages messages={msgs} agentName="dev-agent" onPermissionRespond={() => {}} streaming={live} />
      </SearchProvider>
    </QueryClientProvider>
  )
  const r = render(view(messages, streaming))
  return { ...r, update: (msgs: DisplayMessage[], live = true) => r.rerender(view(msgs, live)) }
}

const note = (content: string): MessageBlock => ({ type: 'text', content, note: true })
const userMsg: DisplayMessage = { id: 'u1', role: 'user', blocks: [{ type: 'text', content: 'go' }], createdAt: '2026-10-20T00:00:00Z' }
const metaBlock: MessageBlock = { type: 'metadata', costUsd: 0, durationMs: 1 }

describe('Progress notes in the steps', () => {
  it('shows the run latest note as a faint line under the collapsed chip, in place when expanded', () => {
    renderChat([msg([note('I will read it.'), readTool('/tmp/a.txt'), note('Now the count.'), readTool('/tmp/b.txt')])])
    expect(screen.getByTestId('activity-note').textContent).toBe('Now the count.')
    fireEvent.click(screen.getByTestId('activity-chip'))
    expect(screen.queryByTestId('activity-note')).toBeNull()
    expect(screen.getAllByTestId('progress-note').map((n) => n.textContent)).toEqual(['I will read it.', 'Now the count.'])
  })

  it('renders a note with no steps around it flat and faint', () => {
    renderChat([msg([note('Thinking it over.'), { type: 'text', content: 'The answer.' }])])
    expect(screen.getByTestId('progress-note').textContent).toBe('Thinking it over.')
    expect(screen.queryByTestId('activity-chip')).toBeNull()
  })

  it('mounts a collapsed chip notes for search, never the faint line', () => {
    renderChat([msg([readTool('/tmp/a.txt'), note('the needle here')])], undefined, 'needle')
    const mount = screen.getByTestId('activity-group-search-mount')
    expect(mount.querySelector('[data-search-match]')?.textContent).toBe('needle')
    expect(screen.getByTestId('activity-note').querySelector('[data-search-match]')).toBeNull()
  })
})

describe('Working… while the turn is open', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  const open = (blocks: MessageBlock[]): DisplayMessage => msg(blocks, { id: 'stream-1' })

  it('shows after the grace with nothing running, and goes when a block arrives', () => {
    const quiet = [readTool('/tmp/a.txt')]
    const { update } = renderStreaming([userMsg, open(quiet)])
    act(() => { vi.advanceTimersByTime(3900) })
    expect(screen.queryByTestId('turn-working')).toBeNull()
    act(() => { vi.advanceTimersByTime(200) })
    expect(screen.getByTestId('turn-working').textContent).toBe('Working…')
    update([userMsg, open([...quiet, { type: 'text', content: 'Done' }])])
    expect(screen.queryByTestId('turn-working')).toBeNull()
  })

  it('never shows between two quick steps, nor while something runs or a card asks', () => {
    const { update } = renderStreaming([userMsg, open([readTool('/tmp/a.txt')])])
    act(() => { vi.advanceTimersByTime(2000) })
    update([userMsg, open([readTool('/tmp/a.txt'), readTool('/tmp/b.txt', { toolId: 't2' })])])
    act(() => { vi.advanceTimersByTime(2500) })
    expect(screen.queryByTestId('turn-working')).toBeNull()
    update([userMsg, open([readTool('/tmp/a.txt', { status: 'running' })])])
    act(() => { vi.advanceTimersByTime(10000) })
    expect(screen.queryByTestId('turn-working')).toBeNull()
    update([userMsg, open([{ type: 'permission', requestId: 'r', toolName: 'Bash', toolInput: {} }])])
    act(() => { vi.advanceTimersByTime(10000) })
    expect(screen.queryByTestId('turn-working')).toBeNull()
  })

  it('never shows once the turn ended, nor on the last turn bubble while a server turn starts', () => {
    const { update } = renderStreaming([userMsg, open([readTool('/tmp/a.txt')])])
    update([userMsg, open([readTool('/tmp/a.txt')])], false)
    act(() => { vi.advanceTimersByTime(10000) })
    expect(screen.queryByTestId('turn-working')).toBeNull()
    update([userMsg, open([readTool('/tmp/a.txt'), metaBlock])], true)
    act(() => { vi.advanceTimersByTime(10000) })
    expect(screen.queryByTestId('turn-working')).toBeNull()
  })

  it('pulses the open bubble chip for a waited sub-agent and stops at the turn end', () => {
    const waiting: MessageBlock = { type: 'subagent', description: 'count slowly', subagentType: 'general-purpose', isActive: true, _background: true }
    const earlier = msg([waiting, { type: 'text', content: 'Started.' }, metaBlock], { id: 'db-0' })
    const { update } = renderStreaming([earlier, userMsg, open([{ ...waiting, _toolId: 'a2' } as MessageBlock])])
    const chips = screen.getAllByTestId('activity-chip')
    expect(chips[0].textContent).not.toContain('Working')   // an earlier turn never pulses
    expect(chips[1].textContent).toContain('count slowly')
    update([earlier, userMsg, open([{ ...waiting, _toolId: 'a2' } as MessageBlock, metaBlock])], false)
    expect(screen.getAllByTestId('activity-chip')[1].textContent).not.toContain('Working')
  })
})

describe("The running chip reads the agent's words", () => {
  const live = (blocks: MessageBlock[]): DisplayMessage => msg(blocks, { id: 'stream-1' })

  it('shows the note on the running line while its tool runs, and not again under it', () => {
    renderStreaming([userMsg, live([note('I will list the folder.'), readTool('/tmp/a.txt', { status: 'running' })])])
    const chip = screen.getByTestId('activity-chip').textContent ?? ''
    expect(chip).toContain('Working')
    expect(chip).toContain('I will list the folder.')
    expect(chip).not.toContain('/tmp/a.txt')
    expect(screen.queryByTestId('activity-note')).toBeNull()
    fireEvent.click(screen.getByTestId('activity-chip'))
    expect(screen.getByText('/tmp/a.txt')).toBeInTheDocument()   // the steps keep the tool
  })

  it("shows the running step's name when the span has no note", () => {
    renderStreaming([userMsg, live([readTool('/tmp/a.txt', { status: 'running' })])])
    expect(screen.getByTestId('activity-chip').textContent).toContain('/tmp/a.txt')
  })

  it('keeps the note as the faint line when a reasoning heading runs after it', () => {
    const reasoning: MessageBlock = { type: 'thinking', content: '**Checking the folder**\n\nthen', collapsed: false, done: false }
    renderStreaming([userMsg, live([note('First the list.'), readTool('/tmp/a.txt'), reasoning])])
    expect(screen.getByTestId('activity-chip').textContent).toContain('Checking the folder')
    expect(screen.getByTestId('activity-note').textContent).toBe('First the list.')
  })
})

describe('A note that is a real message reads as text', () => {
  const directTo: MessageBlock = { type: 'tool', name: 'mcp__meetings-mcp__direct_to', toolId: 'd1', summary: '', status: 'done' }

  it("shows a Codex meeting participant's message as text, outside the chip", () => {
    renderChat([msg([note('I think we should ship on Friday, after the walk.'), directTo],
      { badge: 'meeting', agentSlug: 'writer' })])
    expect(screen.queryByTestId('progress-note')).toBeNull()
    expect(screen.queryByTestId('activity-note')).toBeNull()
    expect(screen.getByText('I think we should ship on Friday, after the walk.')).toBeInTheDocument()
    expect(screen.getByTestId('activity-chip').textContent).toContain('1 step')
  })

  it("shows a finished bubble's only text, a note, as text", () => {
    renderChat([msg([note('Here is the report you asked for.'),
      readTool('/tmp/report.md'), { type: 'metadata', costUsd: 0, durationMs: 1 }])])
    expect(screen.queryByTestId('progress-note')).toBeNull()
    expect(screen.getByText('Here is the report you asked for.')).toBeInTheDocument()
  })

  it('still folds a short preamble before a tool', () => {
    renderChat([msg([note('I will read it.'), readTool('/tmp/a.txt'), { type: 'text', content: 'It holds two lines.' },
      { type: 'metadata', costUsd: 0, durationMs: 1 }])])
    expect(screen.getByTestId('activity-note').textContent).toBe('I will read it.')
  })

  it('copies and reads aloud the answer without the asides, and a note drawn as text with it', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const done: MessageBlock = { type: 'metadata', costUsd: 0, durationMs: 1 }
    const { unmount } = renderChat([msg([note('I will read it.'), readTool('/tmp/a.txt'), { type: 'text', content: 'The answer is two lines.' }, done])])
    fireEvent.click(screen.getByTitle('Copy message'))
    expect(writeText).toHaveBeenLastCalledWith('The answer is two lines.')
    unmount()
    const second = renderChat([msg([readTool('/tmp/a.txt'), note('Here are my closing words.'), done])])
    fireEvent.click(screen.getByTitle('Copy message'))
    expect(writeText).toHaveBeenLastCalledWith('Here are my closing words.')
    second.unmount()
    // An answer written before a memory save, then "Noted.": text, copied whole.
    const save: MessageBlock = { type: 'tool', name: 'mcp__memory-mcp__memory', toolId: 'm1', summary: '', status: 'done' }
    renderChat([msg([note('Your meeting is at 3 pm tomorrow with Anna.'), save, { type: 'text', content: 'Noted.' }, done])])
    expect(screen.queryByTestId('activity-note')).toBeNull()
    expect(screen.getByText('Your meeting is at 3 pm tomorrow with Anna.')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Copy message'))
    expect(writeText).toHaveBeenLastCalledWith('Your meeting is at 3 pm tomorrow with Anna.\n\nNoted.')
  })
})

describe('Working… never in a meeting or a terminal chat', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  const qc = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })

  it('never shows under a meeting bubble', () => {
    render(
      <QueryClientProvider client={qc()}>
        <SearchProvider query="">
          <ChatMessages messages={[userMsg, msg([readTool('/tmp/a.txt')], { id: 'stream-m', badge: 'meeting', agentSlug: 'writer' })]}
            agentName="dev-agent" onPermissionRespond={() => {}} streaming />
        </SearchProvider>
      </QueryClientProvider>,
    )
    act(() => { vi.advanceTimersByTime(10000) })
    expect(screen.queryByTestId('turn-working')).toBeNull()
  })

  it('never shows in a chat a terminal drives', () => {
    render(
      <QueryClientProvider client={qc()}>
        <SearchProvider query="">
          <ChatMessages messages={[userMsg, msg([readTool('/tmp/a.txt')], { id: 'stream-t' })]}
            agentName="dev-agent" onPermissionRespond={() => {}} streaming dialogsInTerminal />
        </SearchProvider>
      </QueryClientProvider>,
    )
    act(() => { vi.advanceTimersByTime(10000) })
    expect(screen.queryByTestId('turn-working')).toBeNull()
  })
})
