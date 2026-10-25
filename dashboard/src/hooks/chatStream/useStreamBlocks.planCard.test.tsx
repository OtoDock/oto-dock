/**
 * A Codex plan's card: its plan arrives as a plan_review nothing waits on
 * (`between_turns` on its input), answered between turns. A revision makes
 * the earlier card read as revised, live and after a reload, and a plan's
 * exit draws no card of its own. The history reads the cards the turn saved
 * unanswered from the chat's plan rows (withPlanRows).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'
import { PLAN_IMPLEMENTED, PLAN_REVISED, withPlanRows } from '@/lib/messageBlocks'
import type { DisplayMessage, MessageBlock } from '@/components/chat/types'

const h = vi.hoisted(() => ({
  cb: null as any,
  ws: {
    streaming: true,
    sendMessage: vi.fn(),
    sendPermission: vi.fn(),
    sendPlanReviewResponse: vi.fn(),
    sendQuestionResponse: vi.fn(),
    resumeChat: vi.fn(),
    implementPlan: vi.fn(),
    sendLocationResponse: vi.fn(),
    changeMode: vi.fn(),
    subscribe: vi.fn(() => () => {}),
  },
}))

vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => {
    h.cb = cb
    return h.ws
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

function reviews(result: any) {
  return result.current.messages.flatMap((m: any) => m.blocks.filter((b: any) => b.type === 'plan_review'))
}

const codexReview = (id: string, plan: string) => ({
  request_id: id, plan, filename: 'plan-chat1.md',
  tool_input: { plan, planFilePath: 'plan-chat1.md', between_turns: true },
})

describe('a Codex plan card (live)', () => {
  beforeEach(() => {
    h.ws.streaming = true
  })

  it('appends the plan card to the live streaming message', () => {
    const { result } = renderStream()
    act(() => h.cb.onText('- Add --version\n- Add a CLI test'))
    act(() => h.cb.onPlanReview(codexReview('r1', '- Add --version')))

    const [card] = reviews(result)
    expect(card.requestId).toBe('r1')
    expect(card.resolved).toBeFalsy()
    expect(card.filename).toBe('plan-chat1.md')
  })

  it('reads an earlier card as revised when a revision arrives', () => {
    const { result } = renderStream()
    act(() => h.cb.onText('v1 plan'))
    act(() => h.cb.onPlanReview(codexReview('r1', 'v1 plan')))
    act(() => h.cb.onDone?.())
    act(() => h.cb.onText('v2 plan'))
    act(() => h.cb.onPlanReview(codexReview('r2', 'v2 plan')))

    const [v1, v2] = reviews(result)
    expect(v1).toMatchObject({ requestId: 'r1', resolved: true, action: PLAN_REVISED })
    expect(v2.requestId).toBe('r2')
    expect(v2.resolved).toBeFalsy()
  })

  it('draws no card for a plan exit', () => {
    const { result } = renderStream()
    act(() => h.cb.onText('some answer'))
    act(() => h.cb.onPlanMode({ action: 'exit', tool_input: { plan: 'x' } }))

    expect(result.current.messages.some((m: any) => m.blocks.some((b: any) => b.type === 'plan'))).toBe(false)
  })
})

describe('withPlanRows', () => {
  const saved = (id: string, betweenTurns: boolean, extra: Partial<MessageBlock> = {}): MessageBlock => ({
    type: 'plan_review', requestId: id, plan: 'p', filename: 'plan-chat1.md',
    toolInput: betweenTurns ? { between_turns: true } : { plan: 'p' },
    resolved: true, action: 'reject', unanswered: true, ...extra,
  } as MessageBlock)
  const turn = (id: string, blocks: MessageBlock[]): DisplayMessage => ({ id, role: 'assistant', blocks, createdAt: '' })
  const cards = (msgs: DisplayMessage[]) => msgs.flatMap((m) => m.blocks) as any[]

  it('keeps the newest Codex review answerable while its row is pending', () => {
    const out = cards(withPlanRows(
      [turn('db-1', [saved('r1', true)]), turn('db-2', [saved('r2', true)])],
      [{ filename: 'plan-chat1.md', status: 'pending' }],
    ))
    expect(out[0]).toMatchObject({ resolved: true, action: PLAN_REVISED })
    expect(out[1].resolved).toBe(false)
    expect(out[1].action).toBeUndefined()
  })

  it('names the answer from the row', () => {
    const implemented = cards(withPlanRows([turn('db-1', [saved('r1', true)])],
      [{ filename: 'plan-chat1.md', status: 'implemented' }]))
    expect(implemented[0]).toMatchObject({ resolved: true, action: PLAN_IMPLEMENTED })
    const rejected = cards(withPlanRows([turn('db-1', [saved('r1', true)])],
      [{ filename: 'plan-chat1.md', status: 'rejected' }]))
    expect(rejected[0]).toMatchObject({ resolved: true, action: 'reject' })
  })

  it('leaves a Claude review whose turn ended unanswered cancelled', () => {
    const out = cards(withPlanRows([turn('db-1', [saved('r1', false)])],
      [{ filename: 'plan-chat1.md', status: 'pending' }]))
    expect(out[0]).toMatchObject({ resolved: true, action: 'reject' })
  })

  it('leaves an answer the turn saved as it is', () => {
    const answered = saved('r1', true, { action: 'implement_auto', unanswered: undefined })
    const out = cards(withPlanRows([turn('db-1', [answered])],
      [{ filename: 'plan-chat1.md', status: 'pending' }]))
    expect(out[0]).toMatchObject({ resolved: true, action: 'implement_auto' })
  })
})
