/**
 * `costBilled` in the chat stream: a live metadata frame sets it (and stamps
 * the block), chat_history derives it from the newest persisted metadata row
 * so a reload keeps hiding a subscription chat's gauge, and a chat with no
 * flagged turn (fresh, or pre-flag history) shows it. totalCost keeps
 * accumulating regardless; billedCost is the money alone (the billed turns
 * and every tool fee), toolCost the fees' share of it.
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
    created_at: '2026-09-08T00:00:00+00:00',
  }
}

describe('costBilled in useChatStream', () => {
  it('a live metadata frame stamps the block and drives the gauge flag', () => {
    const { result } = renderStream()
    expect(result.current.costBilled).toBe(true)

    act(() => captured.cb.onText('done'))
    act(() => captured.cb.onMetadata({ cost_usd: 0.2, duration_ms: 500, cost_billed: false }))
    const last = result.current.messages[result.current.messages.length - 1]
    expect(last.blocks).toContainEqual({ type: 'metadata', costUsd: 0.2, durationMs: 500, costBilled: false })
    expect(result.current.costBilled).toBe(false)
    expect(result.current.totalCost).toBeCloseTo(0.2)  // still counted
    expect(result.current.billedCost).toBe(0)           // not money

    // The next turn on an API key shows the gauge again.
    act(() => captured.cb.onMetadata({ cost_usd: 0.1, cost_billed: true }))
    expect(result.current.costBilled).toBe(true)
    expect(result.current.totalCost).toBeCloseTo(0.3)
    expect(result.current.billedCost).toBeCloseTo(0.1)
    expect(result.current.toolCost).toBe(0)
  })

  it('a per-tool MCP fee is money and leaves the subscription note to the newest turn', () => {
    const { result } = renderStream()
    act(() => captured.cb.onMetadata({ cost_usd: 0.2, cost_billed: false }))
    expect(result.current.costBilled).toBe(false)

    act(() => captured.cb.onMcpCost({ cost_usd: 0.04, cost_billed: true, provider: 'openai', model: 'gpt-image-1', tool: 'generate_image', mcp: 'image-gen-mcp' }))
    // The model still runs on a subscription: the note stays.
    expect(result.current.costBilled).toBe(false)
    expect(result.current.totalCost).toBeCloseTo(0.24)
    // The icon's money is the fee alone, never the subscription's estimate.
    expect(result.current.billedCost).toBeCloseTo(0.04)
    expect(result.current.toolCost).toBeCloseTo(0.04)

    // A later subscription turn keeps the money already spent here…
    act(() => captured.cb.onMetadata({ cost_usd: 0.1, cost_billed: false }))
    expect(result.current.costBilled).toBe(false)
    expect(result.current.billedCost).toBeCloseTo(0.04)

    // …and a history load reads the same from the fee's own row.
    act(() => captured.cb.onChatHistory({
      chat_id: 'chat-1',
      total_cost: 0.34,
      messages: [
        row('event', '', 'mcp_cost', { type: 'mcp_cost', cost_usd: 0.04, cost_billed: true }),
        row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.1, cost_billed: false }),
      ],
    }))
    expect(result.current.costBilled).toBe(false)
    expect(result.current.billedCost).toBeCloseTo(0.04)
  })

  it('a reload of a subscription chat shows the fees its rows carry', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      chat_id: 'chat-1',
      total_cost: 2.5,
      messages: [
        row('event', '', 'mcp_cost', { type: 'mcp_cost', cost_usd: 0.03, cost_billed: true }),
        row('event', '', 'metadata', { type: 'metadata', cost_usd: 2.47, cost_billed: false }),
      ],
    }))
    expect(result.current.costBilled).toBe(false)
    expect(result.current.totalCost).toBeCloseTo(2.5)
    expect(result.current.billedCost).toBeCloseTo(0.03)
    expect(result.current.toolCost).toBeCloseTo(0.03)
  })

  it('a frame without the flag keeps the gauge shown', () => {
    const { result } = renderStream()
    act(() => captured.cb.onMetadata({ cost_usd: 0.1 }))
    expect(result.current.costBilled).toBe(true)
  })

  it('a DB history seed (rich-view toggle / rows nudge) re-derives the flag too', () => {
    const { result } = renderStream()
    act(() => result.current.seedDbHistory([
      row('user', 'a'),
      row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.2, cost_billed: false }),
    ], false))
    expect(result.current.costBilled).toBe(false)
    act(() => result.current.seedDbHistory([row('user', 'a')], false))
    expect(result.current.costBilled).toBe(true)
  })

  it('chat_history derives the flag from the newest persisted metadata row', () => {
    const { result } = renderStream()
    act(() => captured.cb.onChatHistory({
      chat_id: 'chat-1',
      total_cost: 0.9,
      messages: [
        row('user', 'a'),
        row('assistant', 'b'),
        row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.5, cost_billed: true }),
        row('user', 'c'),
        row('assistant', 'd'),
        row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.4, cost_billed: false }),
      ],
    }))
    expect(result.current.costBilled).toBe(false)
    expect(result.current.totalCost).toBeCloseTo(0.9)
    expect(result.current.billedCost).toBe(0)

    // Switching to a chat whose history predates the flag resets to shown.
    act(() => captured.cb.onChatHistory({
      chat_id: 'chat-1',
      total_cost: 0.3,
      messages: [
        row('user', 'a'),
        row('assistant', 'b'),
        row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.3 }),
      ],
    }))
    expect(result.current.costBilled).toBe(true)
    // A metered chat's money is its whole total, the row's.
    expect(result.current.billedCost).toBeCloseTo(0.3)
  })
})
