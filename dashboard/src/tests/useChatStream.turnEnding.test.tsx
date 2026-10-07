/**
 * A turn that ended typed arrives as an `error` frame carrying a `reason`:
 * the card (a `turn_ended` system block, the same row the server persists
 * inside the turn) lands in the open assistant bubble, and the `done` that
 * follows refetches nothing (the card already renders live). A bare error
 * keeps the generic prefix.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'

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
  useDashboardWs: (cb: any) => {
    captured.cb = cb
    return wsMock
  },
}))

function renderStream(extra: Record<string, unknown> = {}) {
  return renderHook(() =>
    useChatStream({
      agents: [],
      initialChatId: 'chat-1',
      queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
      ...extra,
    }),
  )
}

function lastBlock(result: any): any {
  const msgs = result.current.messages
  const blocks = msgs[msgs.length - 1].blocks
  return blocks[blocks.length - 1]
}

describe('a typed turn ending', () => {
  beforeEach(() => { wsMock.resumeChat.mockClear() })

  it('renders the card in the open bubble, after the text', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Working on it.'))
    const line = '⚠ The engine went silent with nothing running, so the turn was ended. Send the message again.'
    act(() => captured.cb.onError(line, { reason: 'silent', resets_at: '' }))
    const msgs = result.current.messages
    expect(msgs).toHaveLength(1)
    expect(msgs[0].blocks[0]).toEqual({ type: 'text', content: 'Working on it.' })
    expect(lastBlock(result)).toEqual({ type: 'system', subtype: 'turn_ended', reason: 'silent', message: line })
    expect(result.current.turnStartTime).toBeNull()
  })

  it('opens a bubble of its own when none is open', () => {
    const { result } = renderStream()
    act(() => captured.cb.onError('⚠ The engine reported an error', { reason: 'error', resets_at: '' }))
    expect(result.current.messages).toHaveLength(1)
    expect(lastBlock(result)).toMatchObject({ type: 'system', subtype: 'turn_ended', reason: 'error' })
  })

  it('the done that follows refetches nothing', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderStream({ enableDefensiveRefetch: true })
      act(() => captured.cb.onError('⚠ lost', { reason: 'lost', resets_at: '' }))
      act(() => captured.cb.onDone())
      act(() => { vi.advanceTimersByTime(1000) })
      expect(wsMock.resumeChat).not.toHaveBeenCalled()
      // The next turn's done refetches as before (an empty bubble).
      act(() => captured.cb.onDone())
      act(() => { vi.advanceTimersByTime(1000) })
      expect(wsMock.resumeChat).toHaveBeenCalled()
      expect(result.current.messages.length).toBeGreaterThan(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a chat switch after the error lets the next done refetch', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderStream({ enableDefensiveRefetch: true })
      act(() => captured.cb.onError('⚠ lost', { reason: 'lost', resets_at: '' }))
      // An old proxy sends no done after the error: the flag must not
      // outlive the turn it belongs to.
      act(() => { result.current.erroredRef.current = false })
      act(() => captured.cb.onDone())
      act(() => { vi.advanceTimersByTime(1000) })
      expect(wsMock.resumeChat).toHaveBeenCalledTimes(1)
      act(() => captured.cb.onError('⚠ lost', { reason: 'lost', resets_at: '' }))
      expect(result.current.erroredRef.current).toBe(true)
      act(() => result.current.setChatId('chat-2'))
      expect(result.current.erroredRef.current).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the Error prefix for a bare error', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Working on it.'))
    act(() => captured.cb.onError('Not logged in'))
    expect(lastBlock(result)).toEqual({ type: 'text', content: 'Working on it.\n\n**Error:** Not logged in' })
  })
})

describe('Send again', () => {
  const user = (id: string, blocks: any[]) => ({ id, role: 'user' as const, blocks, createdAt: '' })
  const answer = (id: string, blocks: any[]) => ({ id, role: 'assistant' as const, blocks, createdAt: '' })

  it('re-sends every message of the turn that ended, with their attachments', async () => {
    const { lastTurnPrompt } = await import('@/lib/messageBlocks')
    const again = lastTurnPrompt([
      user('u0', [{ type: 'text', content: 'earlier' }]),
      answer('a0', [{ type: 'text', content: 'an answer' }]),
      user('u1', [{ type: 'file_attachments', files: [{ name: 'a.pdf', path: 'users/x/a.pdf' }] },
                  { type: 'text', content: 'first' }]),
      user('u2', [{ type: 'image_attachments', images: ['p.jpg'], paths: ['users/x/p.jpg'] },
                  { type: 'text', content: 'second' }]),
      answer('a1', [{ type: 'system', subtype: 'turn_ended', reason: 'exited', message: 'x' }]),
    ] as any)
    expect(again?.text).toBe('first\n\nsecond')
    expect(again?.files.map((f) => f.uploadedPath)).toEqual(['users/x/a.pdf'])
    expect(again?.images.map((i) => i.path)).toEqual(['users/x/p.jpg'])
  })

  it('stops at the last answer text and has nothing without a message', async () => {
    const { lastTurnPrompt } = await import('@/lib/messageBlocks')
    expect(lastTurnPrompt([answer('a0', [{ type: 'text', content: 'hi' }])] as any)).toBeNull()
    const again = lastTurnPrompt([
      user('u1', [{ type: 'text', content: 'prompt' }]),
      answer('a1', [{ type: 'text', content: 'half' }]),
      user('u2', [{ type: 'text', content: 'the steer' }]),
      answer('a2', [{ type: 'system', subtype: 'turn_ended', reason: 'silent', message: 'x' }]),
    ] as any)
    expect(again?.text).toBe('the steer')
  })
})
