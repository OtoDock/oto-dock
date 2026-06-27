/**
 * Codex plan-mode implement card must append LIVE (not only on reload).
 *
 * The backend synthesizes a `plan_mode {action:"exit", synthetic:true}` event at
 * the end of a plan-mode codex turn. onPlanMode supersedes any prior exit card
 * and appends the new one. The bug: the supersede step rebuilt every message
 * object via .map(), replacing the streaming assistant message WITHOUT updating
 * currentMsgRef — so the follow-up appendBlock (which requires
 * last === currentMsgRef.current) silently no-op'd and the card only appeared
 * after a reload (from the pump-persisted DB row). The fix supersedes + appends
 * in one setMessages, keeping currentMsgRef in sync.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'

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

function lastAssistant(result: any) {
  const msgs = result.current.messages
  return [...msgs].reverse().find((m: any) => m.role === 'assistant')
}

describe('codex plan-mode implement card (live append)', () => {
  beforeEach(() => {
    h.ws.streaming = true
  })

  it('appends the plan card to the live streaming message', () => {
    const { result } = renderStream()
    // Seed a streaming assistant message (the plan text arrives as the turn's
    // final agentMessage before the synthetic exit).
    act(() => h.cb.onText('- Add --version\n- Add a CLI test'))
    act(() =>
      h.cb.onPlanMode({
        action: 'exit',
        synthetic: true,
        tool_input: { plan: '- Add --version\n- Add a CLI test' },
      }),
    )

    const msg = lastAssistant(result)
    const plan = msg.blocks.find((b: any) => b.type === 'plan')
    expect(plan).toBeTruthy()
    expect(plan.action).toBe('exit')
    expect(plan.toolInput.plan).toContain('--version')
  })

  it('supersedes a prior plan card on a refinement turn', () => {
    const { result } = renderStream()
    act(() => h.cb.onText('v1 plan'))
    act(() =>
      h.cb.onPlanMode({ action: 'exit', synthetic: true, tool_input: { plan: 'v1 plan' } }),
    )
    // Second plan turn: new streaming message + a fresh synthetic exit.
    act(() => h.cb.onDone?.())
    act(() => h.cb.onText('v2 plan'))
    act(() =>
      h.cb.onPlanMode({ action: 'exit', synthetic: true, tool_input: { plan: 'v2 plan' } }),
    )

    const allPlans = result.current.messages.flatMap((m: any) =>
      m.blocks.filter((b: any) => b.type === 'plan' && b.action === 'exit'),
    )
    expect(allPlans.length).toBe(2)
    const superseded = allPlans.filter((p: any) => p.superseded)
    const active = allPlans.filter((p: any) => !p.superseded)
    expect(superseded.length).toBe(1)
    expect(active.length).toBe(1)
    expect(active[0].toolInput.plan).toBe('v2 plan')
  })

  it('ignores a non-synthetic (Claude) plan_mode exit', () => {
    const { result } = renderStream()
    act(() => h.cb.onText('some answer'))
    act(() => h.cb.onPlanMode({ action: 'exit', tool_input: { plan: 'x' } }))

    const msg = lastAssistant(result)
    expect(msg.blocks.some((b: any) => b.type === 'plan')).toBe(false)
  })
})
