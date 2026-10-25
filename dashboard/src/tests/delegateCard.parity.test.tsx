/**
 * One renderer for delegate cards: the live frames (a delegate_result, a
 * continue steered into a running lane) and the stored rows build the same
 * message, card key included, so a card's open state survives the refetch
 * that swaps one for the other. A result pushed mid-turn lets the turn go
 * on below it (permission cards included); one delivery's second frame
 * changes nothing.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'
import type { DisplayMessage } from '@/components/chat/types'

const wsMock = vi.hoisted(() => ({
  streaming: false,
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
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

const AGENTS = [{ name: 'worker', display_name: 'Worker Agent', color: '#2a6df4' }]

function renderStream() {
  return renderHook(() =>
    useChatStream({
      agents: AGENTS,
      initialChatId: 'chat-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }),
  )
}

const RESULT = {
  type: 'delegate_result', chat_id: 'chat-1', task_id: 'dyn-7', task_name: 'cards', agent: 'worker',
  output_text: 'The report.', status: 'failed', report_missing: true,
  files: [{ path: 'workspace/inbox/worker/r.md', bytes: 3 }], files_skipped: [],
}

const shape = (m: DisplayMessage) => ({
  role: m.role, blocks: m.blocks, agentSlug: m.agentSlug, agentDisplayName: m.agentDisplayName,
  agentColor: m.agentColor, badge: m.badge, delegateCard: m.delegateCard,
})

describe('delegate cards: live and history build one shape', () => {
  it('a delegate_result frame and its stored row', () => {
    const { result } = renderStream()
    act(() => captured.cb.onDelegateResult(RESULT))
    const live = result.current.messages.find((m) => m.delegateCard)!
    const { type: _type, chat_id: _chat, ...stored } = RESULT
    const [history] = dbMessagesToDisplay([{
      id: 9, role: 'event', content: '', event_type: 'delegate_result',
      event_data: JSON.stringify(stored), created_at: '2026-10-16T09:00:00+00:00',
    }], AGENTS)
    expect(shape(live)).toEqual(shape(history))
    expect(live.delegateCard?.kind).toBe('result')
  })

  it('a continue steered into a running lane and its stored row', () => {
    const { result } = renderStream()
    const meta = { agent_slug: 'boss', agent_display_name: 'Boss', agent_color: '#111', badge: 'delegated by' }
    act(() => captured.cb.onSteered({ type: 'steered', chat_id: 'chat-1', text: 'Now the docs.', event_data: meta, message_id: 42 }))
    const live = result.current.messages.find((m) => m.delegateCard)!
    const [history] = dbMessagesToDisplay([{
      id: 42, role: 'user', content: 'Now the docs.', event_type: '',
      event_data: JSON.stringify(meta), created_at: '2026-10-16T09:00:00+00:00',
    }], AGENTS)
    expect(shape(live)).toEqual({ ...shape(history), agentDisplayName: 'Boss' })
    expect(live.delegateCard).toEqual({ kind: 'prompt', key: 'db-42' })
  })
})

describe('a result pushed while the turn streams', () => {
  it('the turn goes on below the card, its permission card included', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Looking at the lanes.'))
    act(() => captured.cb.onDelegateResult(RESULT))
    act(() => captured.cb.onText('The lane is back.'))
    act(() => captured.cb.onPermissionPrompt({
      type: 'permission_prompt', chat_id: 'chat-1', request_id: 'r-1', tool_name: 'Bash', tool_input: { command: 'ls' },
    }))
    const msgs = result.current.messages
    const card = msgs.findIndex((m) => m.delegateCard)
    expect(card).toBeGreaterThan(0)
    expect(JSON.stringify(msgs[card - 1].blocks)).toContain('Looking at the lanes.')
    const after = msgs.slice(card + 1)
    expect(JSON.stringify(after)).toContain('The lane is back.')
    expect(after.some((m) => m.blocks.some((b) => b.type === 'permission'))).toBe(true)
  })

  it('a later round of the task with the same output is a new card', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderStream()
      act(() => captured.cb.onDelegateResult(RESULT))
      act(() => captured.cb.onDelegateResult(RESULT))
      expect(result.current.messages.filter((m) => m.delegateCard).length).toBe(1)
      act(() => { vi.advanceTimersByTime(61_000) })
      act(() => captured.cb.onDelegateResult(RESULT))
      expect(result.current.messages.filter((m) => m.delegateCard).length).toBe(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('one delivery\'s second frame adds nothing', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Working.'))
    act(() => captured.cb.onDelegateResult(RESULT))
    const count = result.current.messages.length
    act(() => captured.cb.onDelegateResult(RESULT))
    expect(result.current.messages.length).toBe(count)
    act(() => captured.cb.onText('Still here.'))
    expect(JSON.stringify(result.current.messages[result.current.messages.length - 1].blocks)).toContain('Still here.')
  })
})
