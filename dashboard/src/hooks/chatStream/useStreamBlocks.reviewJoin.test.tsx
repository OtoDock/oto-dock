// One reply across background work: the review a finished background job
// wakes goes on from the agent's bubble above it (`continuation`), drawn
// joined with the nudge's line, and the history draws it the way the live
// stream did. A turn's live part joins its saved part the same way. A page
// of older history asked for in another window is dropped.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, render, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'
import ChatMessages from '@/components/chat/ChatMessages'
import { useChatStream } from '@/hooks/useChatStream'
import { useChatMessages, type OlderPage } from '@/hooks/useChatMessages'
import { continuesAbove, dbMessagesToDisplay, nudgeNote } from '@/lib/messageBlocks'
import { LIVE_BLOCK, SERVER_TURN_CAUSE, SYSTEM_SUBTYPE } from '@/api/wireEvents'
import type { DisplayMessage } from '@/components/chat/types'

const wsMock = vi.hoisted(() => ({
  streaming: true,
  sendMessage: vi.fn(),
  sendPermission: vi.fn(),
  sendPlanReviewResponse: vi.fn(),
  sendQuestionResponse: vi.fn(),
  resumeChat: vi.fn(),
  implementPlan: vi.fn(),
  sendLocationResponse: vi.fn(),
  subscribe: vi.fn(() => () => {}),
}))
const captured = vi.hoisted(() => ({ cb: null as any }))
vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => { captured.cb = cb; return wsMock },
}))
const older = vi.hoisted(() => ({ fetch: vi.fn() }))
vi.mock('@/api/chats', async (orig) => ({
  ...(await orig<typeof import('@/api/chats')>()),
  fetchOlderMessages: older.fetch,
}))

let nextId = 1
beforeEach(() => { nextId = 1 })
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++,
    role,
    content,
    event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '',
    created_at: '2026-10-22T03:20:00+00:00',
  }
}
// The stored nudge row: its data names no type (the column does).
const nudgeRow = (kind: 'bg_command_nudge' | 'bg_nudge', count?: number) =>
  row('event', '', kind, count === undefined ? {} : { count, labels: ['b1 — a build'] })
const metadataRow = () => row('event', '', 'metadata', { type: 'metadata', cost_usd: 0, duration_ms: 5 })
const toolRow = (id: string) => row('event', '', 'tool', { type: 'tool', name: 'Bash', tool_id: id, summary: '' })

// What the reader sees of a bubble: its role, its speaker, its join, its blocks.
const shape = (m: DisplayMessage) => [
  m.role, m.agentSlug ?? '', m.badge ?? '', !!m.continuation,
  m.blocks.map((b: any) => [b.type, b.subtype ?? '', b.count ?? 0, b.content ?? '']),
]

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// What the page draws of a list: the headers (the agent's name), the joined
// bubbles, the nudge lines and the footers (one copy button per bubble).
function drawn(messages: DisplayMessage[]) {
  vi.stubGlobal('ResizeObserver', ObserverStub)
  vi.stubGlobal('IntersectionObserver', ObserverStub)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <ChatMessages messages={messages} agentName="a" agentDisplayName="Agent A" onPermissionRespond={() => {}} />
    </QueryClientProvider>,
  )
  const page = {
    headers: within(view.container).queryAllByText('Agent A').length,
    joined: view.container.querySelectorAll('[data-continuation]').length,
    notes: [...view.container.querySelectorAll('[data-testid="review-note"]')].map((n) => n.textContent),
    footers: within(view.container).queryAllByTitle('Copy message').length,
  }
  view.unmount()
  vi.unstubAllGlobals()
  return page
}

const turnStart = (cause: string) => ({ type: 'server_turn_start', chat_id: 'chat-1', cause })

function renderStream() {
  return renderHook(() => useChatStream({
    agents: [], initialChatId: 'chat-1', queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
  }))
}

describe('a background review in the history', () => {
  it('goes on from the agent bubble above, starting with the nudge and its count', () => {
    const msgs = dbMessagesToDisplay([
      row('user', 'run the suites'),
      row('assistant', 'Started the suites.'),
      metadataRow(),
      nudgeRow('bg_command_nudge', 14),
      toolRow('t9'),
      row('assistant', 'All fourteen passed.'),
      metadataRow(),
    ], [])
    expect(msgs.map((m) => [m.id, !!m.continuation])).toEqual([['db-1', false], ['db-2', false], ['db-evt-4', true]])
    expect(msgs[2].blocks[0]).toEqual({ type: 'system', subtype: SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED, count: 14 })
    expect(nudgeNote(msgs[2].blocks[0])).toBe('14 background commands finished')
  })

  it('keeps its header after a person, a delegate result or another agent; speaks as a meeting bubble above', () => {
    const afterPerson = dbMessagesToDisplay([row('user', 'hi'), nudgeRow('bg_nudge', 2), row('assistant', 'Both agents reported.')], [])
    expect(afterPerson.map((m) => !!m.continuation)).toEqual([false, false])
    const afterResult = dbMessagesToDisplay([
      row('assistant', 'Delegated.'),
      row('event', '', 'delegate_result', { type: 'delegate_result', task_id: 'k1', agent: 'w', output_text: 'done' }),
      nudgeRow('bg_command_nudge', 1),
      row('assistant', 'Reviewed.'),
    ], [{ name: 'w', display_name: 'Worker' }])
    expect(afterResult.map((m) => [!!m.delegateCard, !!m.continuation])).toEqual([[false, false], [true, false], [false, false]])
    const afterOther = dbMessagesToDisplay([
      row('assistant', 'My turn.', '', { agent_slug: 'other', agent_display_name: 'Other' }),
      nudgeRow('bg_command_nudge', 1),
      row('assistant', 'Reviewed.'),
    ], [])
    expect(afterOther.map((m) => !!m.continuation)).toEqual([false, false])
    const meeting = dbMessagesToDisplay([
      row('event', '', 'system', { type: 'system', subtype: SYSTEM_SUBTYPE.MEETING_TURN_START, agent: 'a2' }),
      row('assistant', 'In the meeting.'),
      nudgeRow('bg_command_nudge', 1),
      row('assistant', 'Reviewed.'),
    ], [])
    expect(meeting.map((m) => [m.agentSlug ?? '', m.badge ?? '', !!m.continuation])).toEqual([['a2', 'meeting', false], ['a2', 'meeting', true]])
  })

  it('a scheduled wake, an artifact or app action still opens a reply with its header', () => {
    const msgs = dbMessagesToDisplay([
      row('assistant', 'Checking back later.'),
      row('event', '', 'schedule_wake', { prompt: 'Check the log.' }),
      row('assistant', 'The log is clean.'),
    ], [])
    expect(msgs.map((m) => !!m.continuation)).toEqual([false, false])
  })

  it('reads the nudge in words by its kind and count', () => {
    const line = (subtype: string, count?: number) => nudgeNote({ type: 'system', subtype, ...(count ? { count } : {}) })
    expect(line(SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED, 1)).toBe('1 background command finished')
    expect(line(SYSTEM_SUBTYPE.BG_AGENTS_COMPLETED, 2)).toBe('2 background agents finished')
    expect(line(SYSTEM_SUBTYPE.BG_AGENTS_COMPLETED, 1)).toBe('1 background agent finished')
    expect(line(SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED)).toBe('Background commands finished')
    expect(nudgeNote({ type: 'system', subtype: SYSTEM_SUBTYPE.CONTEXT_COMPRESSED })).toBeNull()
    expect(nudgeNote({ type: 'text', content: 'x' })).toBeNull()
  })

  it('joins only an agent reply to an agent reply of the same speaker', () => {
    const a = (over: Partial<DisplayMessage> = {}): DisplayMessage => ({ id: 'x', role: 'assistant', blocks: [], createdAt: '', ...over })
    expect(continuesAbove(a(), a())).toBe(true)
    expect(continuesAbove(undefined, a())).toBe(false)
    expect(continuesAbove(a({ role: 'user' }), a())).toBe(false)
    expect(continuesAbove(a({ delegateCard: { kind: 'result', key: 'k' } }), a())).toBe(false)
    expect(continuesAbove(a({ agentSlug: 'other' }), a())).toBe(false)
  })
})

describe('a background review on the live stream', () => {
  async function finishTurn(text: string) {
    act(() => captured.cb.onText(text))
    act(() => captured.cb.onMetadata({ cost_usd: 0, duration_ms: 5 }))
    act(() => captured.cb.onDone())
    await act(async () => {})  // the streaming bubble lets go on a microtask
  }

  afterEach(() => { vi.useRealTimers() })

  it('opens the review bubble with the nudge at the turn start, as the history draws it', async () => {
    const { result } = renderStream()
    await finishTurn('Started the suites.')
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 14 }))
    act(() => captured.cb.onServerTurnStart(turnStart(SERVER_TURN_CAUSE.BG_REVIEW)))
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't9' }))
    act(() => captured.cb.onToolEnd({ name: 'Bash', tool_id: 't9' }))
    await finishTurn('All fourteen passed.')
    const live = result.current.messages
    const history = dbMessagesToDisplay([
      row('user', 'run the suites'),
      row('assistant', 'Started the suites.'),
      metadataRow(),
      nudgeRow('bg_command_nudge', 14),
      toolRow('t9'),
      row('assistant', 'All fourteen passed.'),
      metadataRow(),
    ], []).slice(1)
    expect(live.map(shape)).toEqual(history.map(shape))
    expect(live[1].continuation).toBe(true)
    // The page draws both the same: one header, one joined bubble with its
    // line, a footer per bubble.
    const page = drawn(live)
    expect(page).toEqual(drawn(history))
    expect(page).toEqual({ headers: 1, joined: 1, notes: ['14 background commands finished'], footers: 2 })
  })

  it('a nudge whose turn did not start does not label a later turn, at any time', async () => {
    const { result } = renderStream()
    await finishTurn('Started the build.')
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 2 }))
    // The review was kept (another starter held the chat); a scheduled
    // wake's turn starts at once.
    act(() => captured.cb.onServerTurnStart(turnStart(SERVER_TURN_CAUSE.WAKE)))
    await finishTurn('The wake.')
    const last = result.current.messages[result.current.messages.length - 1]
    expect(last.blocks.some((b) => nudgeNote(b) !== null)).toBe(false)
    expect(last.continuation).toBeFalsy()
    // Nor a kick's or a result's turn; a review's own turn still draws it.
    for (const cause of [SERVER_TURN_CAUSE.KICK, SERVER_TURN_CAUSE.DELEGATE_RESULT]) {
      act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 1 }))
      act(() => captured.cb.onServerTurnStart(turnStart(cause)))
      await finishTurn(`A ${cause} turn.`)
      expect(result.current.messages[result.current.messages.length - 1].continuation).toBeFalsy()
    }
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 3 }))
    act(() => captured.cb.onServerTurnStart(turnStart(SERVER_TURN_CAUSE.BG_REVIEW)))
    await finishTurn('Reviewed.')
    const review = result.current.messages[result.current.messages.length - 1]
    expect(review.blocks.map((b) => nudgeNote(b))).toEqual(['3 background commands finished', null, null])
    expect(review.continuation).toBe(true)
  })

  it('a review in a meeting speaks as the meeting bubble above it, live and in the history', async () => {
    const meetingRows = () => [
      row('event', '', 'system', { type: 'system', subtype: SYSTEM_SUBTYPE.MEETING_TURN_START, agent: 'a2', agent_display_name: 'Two' }),
      row('assistant', 'My point.', '', { agent_slug: 'a2', agent_display_name: 'Two' }),
      metadataRow(),
    ]
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', has_more: false, messages: meetingRows() }))
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 1 }))
    act(() => captured.cb.onServerTurnStart(turnStart(SERVER_TURN_CAUSE.BG_REVIEW)))
    act(() => captured.cb.onText('Reviewed.'))
    const live = result.current.messages
    nextId = 1
    const history = dbMessagesToDisplay([
      ...meetingRows(),
      nudgeRow('bg_command_nudge', 1),
      row('assistant', 'Reviewed.', '', { agent_slug: 'a2', agent_display_name: 'Two' }),
    ], [])
    expect(live.map(shape)).toEqual(history.map(shape))
    expect(live[1]).toMatchObject({ agentSlug: 'a2', badge: 'meeting', continuation: true })
  })

  it('no nudge when the frame only clears badges, or a result starts the turn', async () => {
    const { result } = renderStream()
    await finishTurn('Started.')
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 0 }))
    act(() => captured.cb.onServerTurnStart(turnStart(SERVER_TURN_CAUSE.BG_REVIEW)))
    await finishTurn('A server turn.')
    expect(result.current.messages.map((m) => [m.blocks[0].type, !!m.continuation])).toEqual([['text', false], ['text', false]])

    act(() => captured.cb.onBgAgentsComplete({ type: 'bg_agents_complete', count: 2 }))
    act(() => captured.cb.onDelegateResult({ type: 'delegate_result', task_id: 'k1', task_name: 'n', agent: 'w', status: 'completed', output_text: 'report' }))
    act(() => captured.cb.onServerTurnStart(turnStart(SERVER_TURN_CAUSE.DELEGATE_RESULT)))
    await finishTurn('Synthesis.')
    const last = result.current.messages[result.current.messages.length - 1]
    expect(last.blocks[0]).toEqual({ type: 'text', content: 'Synthesis.' })
    expect(last.continuation).toBeFalsy()
  })

  it('a frame during a turn mints nothing', async () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Working.'))
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 3 }))
    await finishTurn(' Done.')
    act(() => captured.cb.onServerTurnStart(turnStart(SERVER_TURN_CAUSE.BG_REVIEW)))
    await finishTurn('Next.')
    expect(result.current.messages.map((m) => m.blocks.some((b) => nudgeNote(b) !== null))).toEqual([false, false])
  })
})

describe("a run's own review inside its turn", () => {
  // The run's turn on the live stream: the main turn ends with its metadata
  // and no `done` (the pump runs on), the review's frames come flagged.
  function mainTurn() {
    act(() => captured.cb.onText('Started the jobs.'))
    act(() => captured.cb.onMetadata({ cost_usd: 0, duration_ms: 5 }))
  }
  async function reviewTurn(text: string, tool = 't9') {
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: tool }))
    act(() => captured.cb.onToolEnd({ name: 'Bash', tool_id: tool }))
    act(() => captured.cb.onText(text))
    act(() => captured.cb.onMetadata({ cost_usd: 0, duration_ms: 5 }))
  }
  const historyOf = (...between: ReturnType<typeof row>[]) => {
    nextId = 1
    return dbMessagesToDisplay([
      row('user', 'run the jobs'),
      row('assistant', 'Started the jobs.'),
      metadataRow(),
      ...between,
    ], []).slice(1)
  }

  it('ends the bubble at the flagged frame and goes on joined, as the rows draw it', async () => {
    const { result } = renderStream()
    mainTurn()
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 2, review: true }))
    await reviewTurn('Both finished.')
    act(() => captured.cb.onDone())
    await act(async () => {})
    const live = result.current.messages
    const history = historyOf(nudgeRow('bg_command_nudge', 2), toolRow('t9'), row('assistant', 'Both finished.'), metadataRow())
    expect(live.map(shape)).toEqual(history.map(shape))
    expect(live.map((m) => m.role)).toEqual(['assistant', 'assistant'])
    expect(live[1].continuation).toBe(true)
    const page = drawn(live)
    expect(page).toEqual(drawn(history))
    expect(page).toMatchObject({ headers: 1, joined: 1, notes: ['2 background commands finished'] })
  })

  it('two kinds make one bubble with two lines, also before the first is drawn', () => {
    const { result } = renderStream()
    mainTurn()
    act(() => {
      captured.cb.onBgAgentsComplete({ type: 'bg_agents_complete', count: 1, review: true })
      captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 2, review: true })
    })
    act(() => captured.cb.onText('All three reported.'))
    const live = result.current.messages
    const history = historyOf(nudgeRow('bg_nudge', 1), nudgeRow('bg_command_nudge', 2), row('assistant', 'All three reported.'))
    expect(live.map(shape)).toEqual(history.map(shape))
    expect(live).toHaveLength(2)
    expect(live[1].blocks.map((b) => nudgeNote(b))).toEqual(['1 background agent finished', '2 background commands finished', null])
  })

  it('a second round opens its own joined bubble after the first review', async () => {
    const { result } = renderStream()
    mainTurn()
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 1, review: true }))
    await reviewTurn('Started one more.', 't9')
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 1, review: true }))
    await reviewTurn('That one finished too.', 't10')
    const live = result.current.messages
    const history = historyOf(
      nudgeRow('bg_command_nudge', 1), toolRow('t9'), row('assistant', 'Started one more.'), metadataRow(),
      nudgeRow('bg_command_nudge', 1), toolRow('t10'), row('assistant', 'That one finished too.'), metadataRow(),
    )
    expect(live.map(shape)).toEqual(history.map(shape))
    expect(live.map((m) => !!m.continuation)).toEqual([false, true, true])
  })

  it("keeps a sibling's badge that still runs; a frame between turns still clears it", () => {
    const { result } = renderStream()
    act(() => captured.cb.onBgCommandSpawn({ type: 'bg_command_spawn', tool_use_id: 'b2', command: 'sleep 999' }))
    mainTurn()
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 1, review: true }))
    const active = () => result.current.messages.flatMap((m) => m.blocks).filter((b: any) => b.type === 'bgcommand' && b.isActive)
    expect(active()).toHaveLength(1)
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 1 }))
    expect(active()).toHaveLength(0)
  })

  it('a flagged frame while a Stop winds the turn down opens nothing', () => {
    const { result } = renderStream()
    mainTurn()
    // The page's Stop raised the straggler guard; the server's `aborted`
    // has not come yet.
    result.current.abortedRef.current = true
    const before = result.current.messages.length
    act(() => captured.cb.onBgCommandsComplete({ type: 'bg_commands_complete', count: 1, review: true }))
    expect(result.current.messages).toHaveLength(before)
  })
})

describe("a turn's live part below its saved part", () => {
  const liveTool = { type: LIVE_BLOCK.TOOL, name: 'Bash', tool_id: 't5', active: true }

  it('goes on from the unfinished bubble the history ends with', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', has_more: false, live_pending: true,
      messages: [row('user', 'build it'), row('assistant', 'Building.'), toolRow('t4')],
    }))
    act(() => captured.cb.onLiveState({ type: 'live_state', chat_id: 'chat-1', streaming: true, live_blocks: [liveTool] }))
    expect(result.current.messages.map((m) => [m.role, !!m.continuation])).toEqual([
      ['user', false], ['assistant', false], ['assistant', true],
    ])
  })

  it('keeps its header after a finished bubble or a person', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', has_more: false, live_pending: true,
      messages: [row('assistant', 'Earlier.'), metadataRow(), row('user', 'next')],
    }))
    act(() => captured.cb.onLiveState({ type: 'live_state', chat_id: 'chat-1', streaming: true, live_blocks: [liveTool] }))
    expect(result.current.messages.map((m) => !!m.continuation)).toEqual([false, false, false])
  })
})

describe('a page of older history', () => {
  function renderMessages(chatId: { current: string | null }) {
    return renderHook(() => {
      const speaker = useRef(null)
      return useChatMessages({ agents: [], meetingSpeakerRef: speaker, chatIdRef: chatId })
    })
  }

  it('is dropped when the rows were reset while it loaded', async () => {
    const chatId = { current: 'chat-1' as string | null }
    const { result } = renderMessages(chatId)
    act(() => result.current.seedDbHistory([{ id: 50, role: 'assistant', content: 'newest', created_at: '' }], true))
    let resolve: (v: unknown) => void = () => {}
    older.fetch.mockReturnValueOnce(new Promise((r) => { resolve = r }))
    let loading: Promise<OlderPage> = Promise.resolve('nothing')
    act(() => { loading = result.current.loadOlder() })
    act(() => result.current.seedDbHistory([{ id: 90, role: 'assistant', content: 'a new window', created_at: '' }], true))
    let page: OlderPage | undefined
    await act(async () => {
      resolve({ messages: [{ id: 10, role: 'assistant', content: 'stale', created_at: '' }], has_more: false })
      page = await loading
    })
    expect(page).toBe('stale')
    expect(result.current.messages.map((m) => m.id)).toEqual(['db-90'])
    expect(result.current.hasMoreOlder).toBe(true)
    expect(result.current.loadingOlder).toBe(false)
  })

  it('is not asked for while a history waits for its live state', async () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', has_more: true, live_pending: true,
      messages: [row('user', 'build it'), row('assistant', 'Building.')],
    }))
    older.fetch.mockClear()
    let page: OlderPage | undefined
    await act(async () => { page = await result.current.loadOlder() })
    expect(page).toBe('stale')
    expect(older.fetch).not.toHaveBeenCalled()
    act(() => captured.cb.onLiveState({ type: 'live_state', chat_id: 'chat-1', streaming: true, live_blocks: [] }))
    older.fetch.mockResolvedValueOnce({ messages: [], has_more: false })
    await act(async () => { page = await result.current.loadOlder() })
    expect(older.fetch).toHaveBeenCalledTimes(1)
    expect(page).toBe('nothing')
  })

  it('is never asked for with the cursor of another chat', () => {
    const chatId = { current: 'chat-1' as string | null }
    const { result } = renderMessages(chatId)
    act(() => result.current.seedDbHistory([{ id: 50, role: 'assistant', content: 'newest', created_at: '' }], true))
    chatId.current = 'chat-2'
    older.fetch.mockClear()
    act(() => { void result.current.loadOlder() })
    expect(older.fetch).not.toHaveBeenCalled()
  })
})
