/**
 * A page rebuilt from rows after the turn (a reload, a reconnect, the
 * history the server re-sends when the agent's own wake turn starts) keeps
 * the badges of the background work still running: the history frame names
 * it, the blocks turn active again, the done frames clear them one by one,
 * and a spawn outside the loaded rows still shows on the status bar.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'

const wsMock = vi.hoisted(() => ({
  streaming: false,
  sendMessage: vi.fn(),
  sendPermission: vi.fn(),
  sendPlanReviewResponse: vi.fn(),
  sendQuestionResponse: vi.fn(),
  resumeChat: vi.fn(),
  leaveChat: vi.fn(),
  implementPlan: vi.fn(),
  sendLocationResponse: vi.fn(),
  subscribe: vi.fn(() => () => {}),
}))

const captured = vi.hoisted(() => ({ cb: null as any }))

const older = vi.hoisted(() => ({ rows: [] as any[] }))

vi.mock('@/api/chats', async (orig) => ({
  ...(await orig<typeof import('@/api/chats')>()),
  fetchOlderMessages: vi.fn(async () => ({ messages: older.rows, has_more: false })),
}))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }),
  )
}

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++,
    role,
    content,
    event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '',
    created_at: '2026-10-31T00:00:00+00:00',
  }
}

const rows = () => [
  row('user', 'start the work'),
  row('event', '', 'task_spawn', { type: 'task_spawn', description: 'long reader', run_in_background: true, tool_use_id: 'tuA' }),
  row('event', '', 'bg_command_spawn', { type: 'bg_command_spawn', tool_use_id: 'tuB', command: 'sleep 150', description: 'wait' }),
  row('assistant', 'Both are running in the background.'),
]

const badges = (r: { current: { activeAgents: Array<{ id: string; type: string }> } }) =>
  r.current.activeAgents.map((a) => `${a.type}:${a.id}`).sort()

describe('the background badges after a history rebuild', () => {
  it('the rows alone read finished, the frame keeps what still runs', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: rows() }))
    expect(badges(result)).toEqual([])

    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', messages: rows(),
      running: { agents: [{ id: 'tuA', description: 'long reader' }], commands: [{ id: 'tuB', description: 'wait' }] },
    }))
    expect(badges(result)).toEqual(['bgcommand:tuB', 'general-purpose:tuA'])

    act(() => captured.cb.onBgAgentDone({ tool_use_id: 'tuA' }))
    expect(badges(result)).toEqual(['bgcommand:tuB'])
    act(() => captured.cb.onBgCommandDone({ tool_use_id: 'tuB', status: 'completed' }))
    expect(badges(result)).toEqual([])
  })

  it('a spawn outside the loaded rows keeps its badge until it ends', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', messages: rows(),
      running: { agents: [{ id: 'tuOld', description: 'started turns back' }] },
    }))
    expect(badges(result)).toEqual(['general-purpose:tuOld'])
    expect(result.current.activeAgents[0].description).toBe('started turns back')
    act(() => captured.cb.onBgAgentsComplete({ count: 0 }))
    expect(badges(result)).toEqual([])
  })

  it('a refetch of the rows after the turn keeps a live background badge', () => {
    // The check's verdict row nudges the page to refetch its newest page over
    // REST, which carries no running list: the live state wins there.
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: [] }))
    act(() => captured.cb.onTaskSpawn({ type: 'task_spawn', description: 'long reader', run_in_background: true, tool_use_id: 'tuA' }))
    act(() => captured.cb.onDone({}))
    expect(badges(result)).toEqual(['general-purpose:tuA'])
    act(() => result.current.seedDbHistory(rows(), false))
    expect(badges(result)).toEqual(['general-purpose:tuA'])
  })

  it('a new chat shows none of the last chat\'s badges', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', messages: rows(),
      running: { commands: [{ id: 'tuB', description: 'wait' }] },
    }))
    expect(badges(result)).toEqual(['bgcommand:tuB'])
    act(() => { result.current.setMessages([]); result.current.setChatId(null) })
    expect(badges(result)).toEqual([])
  })

  it('a chat picked before its history arrives shows none of them either', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', messages: rows(),
      running: { agents: [{ id: 'tuOld', description: 'started turns back' }] },
    }))
    expect(badges(result)).toEqual(['general-purpose:tuOld'])
    act(() => { result.current.setMessages([]); result.current.setChatId('chat-2') })
    expect(badges(result)).toEqual([])
    // Back on the first chat its own list still counts.
    act(() => result.current.setChatId('chat-1'))
    expect(badges(result)).toEqual(['general-purpose:tuOld'])
  })

  it('a Stop leaves the background work running, as a reload shows it', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: [] }))
    act(() => captured.cb.onTaskSpawn({ type: 'task_spawn', description: 'long reader', run_in_background: true, tool_use_id: 'tuA' }))
    act(() => captured.cb.onTaskSpawn({ type: 'task_spawn', description: 'helper', run_in_background: false, tool_use_id: 'tuF' }))
    act(() => captured.cb.onBgCommandSpawn({ tool_use_id: 'tuB', command: 'sleep 150', description: 'wait' }))
    act(() => captured.cb.onAborted({}))
    const blocks = result.current.messages.flatMap((m: any) => m.blocks)
    const byId = Object.fromEntries(blocks.filter((b: any) => b._toolId).map((b: any) => [b._toolId, b]))
    expect(byId.tuA.isActive).toBe(true)
    expect(byId.tuB.isActive).toBe(true)
    expect(byId.tuF).toMatchObject({ isActive: false, failed: true })
    expect(badges(result)).toEqual(['bgcommand:tuB', 'general-purpose:tuA'])
  })

  it('a refetched page that lost an old spawn keeps its badge', () => {
    // The spawn row is older than the newest page a check's verdict refetches.
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: [] }))
    act(() => captured.cb.onTaskSpawn({ type: 'task_spawn', description: 'long reader', run_in_background: true, tool_use_id: 'tuA' }))
    act(() => captured.cb.onDone({}))
    act(() => result.current.seedDbHistory([row('assistant', 'a later answer')], true))
    expect(badges(result)).toEqual(['general-purpose:tuA'])
    expect(result.current.activeAgents[0].description).toBe('long reader')
    act(() => captured.cb.onBgAgentDone({ tool_use_id: 'tuA' }))
    expect(badges(result)).toEqual([])
  })

  it('a delta carries the list, and a later delta without one empties it', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: [] }))
    act(() => captured.cb.onChatHistoryDelta({
      type: 'chat_history_delta', chat_id: 'chat-1', since_id: 0, messages: rows(),
      running: { agents: [{ id: 'tuA', description: 'long reader' }] },
    }))
    expect(badges(result)).toEqual(['general-purpose:tuA'])
    act(() => captured.cb.onChatHistoryDelta({
      type: 'chat_history_delta', chat_id: 'chat-1', since_id: 0, messages: [row('assistant', 'done now')],
    }))
    expect(badges(result)).toEqual([])
  })

  it('the commands complete frame clears the commands, a review frame clears nothing', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', messages: [],
      running: { agents: [{ id: 'tuOld', description: 'reader' }], commands: [{ id: 'tuC', description: 'wait' }] },
    }))
    act(() => captured.cb.onBgAgentsComplete({ count: 1, review: true }))
    expect(badges(result)).toEqual(['bgcommand:tuC', 'general-purpose:tuOld'])
    act(() => captured.cb.onBgCommandsComplete({ count: 0 }))
    expect(badges(result)).toEqual(['general-purpose:tuOld'])
  })

  it('a held history keeps its list for the paint that follows', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', messages: rows(), live_pending: true,
      running: { agents: [{ id: 'tuA', description: 'long reader' }] },
    }))
    act(() => captured.cb.onDone({}))
    expect(badges(result)).toEqual(['general-purpose:tuA'])
    const spawn = result.current.messages.flatMap((m: any) => m.blocks).find((b: any) => b._toolId === 'tuA')
    expect(spawn.isActive).toBe(true)
  })

  it('an older page that brings a listed spawn shows one badge for it', async () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', has_more: true, messages: [row('assistant', 'newest')],
      running: { agents: [{ id: 'tuOld', description: 'reader' }] },
    }))
    expect(badges(result)).toEqual(['general-purpose:tuOld'])
    older.rows = [row('event', '', 'task_spawn', { type: 'task_spawn', description: 'reader', run_in_background: true, tool_use_id: 'tuOld' })]
    await act(async () => { await result.current.loadOlder() })
    expect(badges(result)).toEqual(['general-purpose:tuOld'])
    const spawn = result.current.messages.flatMap((m: any) => m.blocks).find((b: any) => b._toolId === 'tuOld')
    expect(spawn.isActive).toBe(true)
  })
})

describe('the delegated badge after a history rebuild', () => {
  const lane = { id: 'dyn-1', description: 'long lane', agent: 'worker' }
  const spawnRow = () => row('event', '', 'delegate_spawn', { type: 'delegate_spawn', task_id: 'dyn-1', task_name: 'long lane', agent: 'worker' })
  const resultRow = () => row('event', '', 'delegate_result', { task_id: 'dyn-1', task_name: 'long lane', agent: 'worker', status: 'completed' })
  const describeOf = (r: { current: { activeAgents: Array<{ type: string; description: string }> } }) =>
    r.current.activeAgents.filter((a) => a.type === 'delegate').map((a) => a.description)

  it('a delegation started before the page shows at open and goes with its result frame', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', messages: [row('assistant', 'newest')],
      running: { agents: [], commands: [], delegates: [lane] },
    }))
    expect(badges(result)).toEqual(['delegate:delegate-dyn-1'])
    expect(describeOf(result)).toEqual(['long lane → worker'])
    // A frame with no task id drops nothing (the by-name fallback).
    act(() => captured.cb.onDelegateResult({ type: 'delegate_result', task_name: 'other', agent: 'worker', status: 'completed' }))
    expect(badges(result)).toEqual(['delegate:delegate-dyn-1'])
    act(() => captured.cb.onDelegateResult({ type: 'delegate_result', task_id: 'dyn-1', task_name: 'long lane', agent: 'worker', status: 'completed' }))
    expect(badges(result)).toEqual([])
  })

  it('the terminal and history switch keeps it: the REST page lost the spawn', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: [row('user', 'go'), spawnRow()] }))
    expect(badges(result)).toEqual(['delegate:delegate-dyn-1'])
    // The newest page no longer holds the spawn row (the history toggle, a chat_rows refetch).
    act(() => result.current.seedDbHistory([row('assistant', 'a later answer')], true))
    expect(badges(result)).toEqual(['delegate:delegate-dyn-1'])
    expect(describeOf(result)).toEqual(['long lane → worker'])
    // A refetch that brings its result ends it, the live frame missed.
    act(() => result.current.seedDbHistory([row('assistant', 'later still'), resultRow()], true))
    expect(badges(result)).toEqual([])
  })

  it('a scroll up that brings the spawn shows one badge for it', async () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      type: 'chat_history', chat_id: 'chat-1', has_more: true, messages: [row('assistant', 'newest')],
      running: { delegates: [lane] },
    }))
    older.rows = [spawnRow()]
    await act(async () => { await result.current.loadOlder() })
    expect(badges(result)).toEqual(['delegate:delegate-dyn-1'])
  })

  it('a Stop leaves a delegation running: the worker reports later', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({ type: 'chat_history', chat_id: 'chat-1', messages: [] }))
    act(() => captured.cb.onDelegateSpawn({ type: 'delegate_spawn', task_id: 'dyn-1', task_name: 'long lane', agent: 'worker', prompt_preview: '' }))
    act(() => captured.cb.onAborted({}))
    const block = result.current.messages.flatMap((m: any) => m.blocks).find((b: any) => b.type === 'delegate')
    expect(block.status).toBe('running')
    expect(badges(result)).toEqual(['delegate:delegate-dyn-1'])
  })
})
