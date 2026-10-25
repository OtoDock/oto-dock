// What the page draws while an engine works: a progress note (text with
// `note`) in a block of its own, the note mark (a note with no content) that
// turns the text just streamed into a note at a tool's start, a Claude
// narration block re-sent as a note (its thinking end says so and the open
// thinking goes), a thinking end read by its phase before its text, live
// thinking shown open, and the same notes from history rows and from a
// live-state snapshot.
import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'
import { buildLiveStateMessages } from './liveStateBuilder'
import { useChatMessages } from '@/hooks/useChatMessages'

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

function renderStream() {
  return renderHook(() => useChatStream({
    agents: [], initialChatId: 'chat-1', queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
  }))
}

const shape = (m: any) => m.blocks.map((b: any) => [b.type, b.content ?? '', !!b.note])

describe('progress notes on the live stream', () => {
  it('keeps a note and the answer in separate blocks, both ways', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText("I'll list it", true))
    act(() => captured.cb.onText(' first.', true))
    act(() => captured.cb.onText('Here it is.'))
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't1' }))
    act(() => captured.cb.onText('Next the count.', true))
    expect(shape(result.current.messages[0])).toEqual([
      ['text', "I'll list it first.", true],
      ['text', 'Here it is.', false],
      ['tool', '', false],
      ['text', 'Next the count.', true],
    ])
  })

  it('turns the text just streamed into a note at the mark, before its tool starts', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText("I'll list "))
    act(() => captured.cb.onText('the folder.'))
    act(() => captured.cb.onText('', true))
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't1' }))
    act(() => captured.cb.onText('Three files.'))
    expect(shape(result.current.messages[0])).toEqual([
      ['text', "I'll list the folder.", true],
      ['tool', '', false],
      ['text', 'Three files.', false],
    ])
  })

  it('a mark opens no bubble and changes nothing after a tool or a note', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('', true))
    expect(result.current.messages).toEqual([])
    act(() => captured.cb.onText('A note.', true))
    act(() => captured.cb.onText('', true))
    act(() => captured.cb.onToolStart({ name: 'Bash', tool_id: 't1' }))
    act(() => captured.cb.onText('', true))
    expect(shape(result.current.messages[0])).toEqual([['text', 'A note.', true], ['tool', '', false]])
  })

  it('drops a narration block whose end says note, and draws the note text that follows', () => {
    const { result } = renderStream()
    act(() => captured.cb.onText('Looking.'))
    act(() => captured.cb.onThinking({ phase: 'start' }))
    act(() => captured.cb.onThinking({ text: 'Read it. Next I run it.' }))
    act(() => captured.cb.onThinking({ phase: 'end', note: true }))
    act(() => captured.cb.onText('Read it. Next I run it.', true))
    expect(shape(result.current.messages[0])).toEqual([
      ['text', 'Looking.', false],
      ['text', 'Read it. Next I run it.', true],
    ])
  })

  it('shows thinking open while it streams and folds it at its end', () => {
    const { result } = renderStream()
    act(() => captured.cb.onThinking({ phase: 'start' }))
    act(() => captured.cb.onThinking({ phase: 'delta', text: '**Planning**' }))
    const live = result.current.messages[0].blocks[0] as any
    expect(live).toMatchObject({ type: 'thinking', content: '**Planning**', collapsed: false })
    act(() => captured.cb.onThinking({ phase: 'end', text: '' }))
    expect(result.current.messages[0].blocks[0]).toMatchObject({ collapsed: true, done: true })
  })

  it('reads an end by its phase, keeping the last piece it carries', () => {
    const { result } = renderStream()
    act(() => captured.cb.onThinking({ phase: 'start' }))
    act(() => captured.cb.onThinking({ phase: 'end', text: 'all at once' }))
    expect(result.current.messages[0].blocks[0]).toMatchObject({ content: 'all at once', done: true })
    // A delta with no start of its own opens a block, never rewrites the finished one.
    act(() => captured.cb.onThinking({ phase: 'delta', text: 'stray' }))
    expect(shape(result.current.messages[0])).toEqual([['thinking', 'all at once', false], ['thinking', 'stray', false]])
  })
})

describe('progress notes from history and from a live-state snapshot', () => {
  it('reads a note row by its event_data, beside a meeting speaker', () => {
    const rows = [
      { id: 1, role: 'user', content: 'go', event_type: '', event_data: '', created_at: '2026-10-20T00:00:00Z' },
      { id: 2, role: 'assistant', content: 'Checking.', event_type: '',
        event_data: JSON.stringify({ note: true }), created_at: '2026-10-20T00:00:01Z' },
      { id: 3, role: 'assistant', content: 'Done.', event_type: '', event_data: '', created_at: '2026-10-20T00:00:02Z' },
    ]
    const msgs = dbMessagesToDisplay(rows as any, [])
    expect(shape(msgs[1])).toEqual([['text', 'Checking.', true], ['text', 'Done.', false]])
  })

  it('rebuilds a note block and open thinking from a live-state snapshot', () => {
    const msgs = buildLiveStateMessages(
      [{ type: 'text', content: 'Now the list.', note: true } as any, { type: 'text', content: 'Listed.' } as any],
      { active: true, text: '**Weighing it**' },
    )
    expect(shape(msgs[0])).toEqual([
      ['text', 'Now the list.', true], ['text', 'Listed.', false], ['thinking', '**Weighing it**', false]])
    expect(msgs[0].blocks[2]).toMatchObject({ collapsed: false })
  })
})

describe('useChatMessages: the text append keeps notes and answers apart', () => {
  it('joins text only to a last block of the same kind', () => {
    const { result } = renderHook(() => useChatMessages({
      agents: [], meetingSpeakerRef: { current: null }, chatIdRef: { current: 'chat-1' },
    }))
    const bubble = { id: 'stream-1', role: 'assistant' as const, blocks: [], createdAt: '2026-10-20T00:00:00Z' }
    act(() => {
      result.current.currentMsgRef.current = bubble
      result.current.setMessages([bubble])
    })
    act(() => result.current.appendToLastTextBlock('Plan: ', true))
    act(() => result.current.appendToLastTextBlock('list it.', true))
    act(() => result.current.appendToLastTextBlock('Listed'))
    act(() => result.current.appendToLastTextBlock(' three.'))
    act(() => result.current.appendToLastTextBlock('Next: count.', true))
    expect(shape(result.current.messages[0])).toEqual([
      ['text', 'Plan: list it.', true], ['text', 'Listed three.', false], ['text', 'Next: count.', true]])
  })
})
