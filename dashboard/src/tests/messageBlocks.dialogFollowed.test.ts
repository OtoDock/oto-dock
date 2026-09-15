/**
 * Question and plan-approval cards resolve once ANYTHING follows them.
 *
 * An interactive terminal's picker answer writes no user row — the agent's
 * continuation lands as later blocks of the same assistant message — so the
 * old "a user message follows" rule left the card unanswered (and its
 * "answer in the terminal" hint up) forever. Headless chats still resolve on
 * the user's answer message.
 */
import { describe, it, expect } from 'vitest'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'

const AGENTS = [{ name: 'alpha', display_name: 'Alpha', color: '#2a6df4' }]

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++,
    role,
    content,
    event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '',
    created_at: '2026-09-10T18:31:00+00:00',
  }
}

const question = () => row('event', '', 'question', {
  type: 'question', tool_name: 'AskUserQuestion',
  tool_input: { questions: [{ question: 'Which colour?', options: [{ label: 'Red' }] }] },
})
const planExit = () => row('event', '', 'plan_mode', {
  type: 'plan_mode', action: 'exit', tool_input: { plan: '1. add README' },
})

function blocksOf(msgs: ReturnType<typeof dbMessagesToDisplay>) {
  return msgs.flatMap((m) => m.blocks)
}

describe('dialog cards followed by the continuation', () => {
  it('an open question with nothing after it stays unanswered', () => {
    const msgs = dbMessagesToDisplay([row('user', 'ask me'), question()], AGENTS)
    const q = blocksOf(msgs).find((b) => b.type === 'question')
    expect(q && q.type === 'question' && q.answered).toBeFalsy()
  })

  it('a question followed by the terminal continuation is followed in its turn, not answered', () => {
    // The renderer counts `followedInTurn` on interactive chats only.
    const msgs = dbMessagesToDisplay(
      [row('user', 'ask me'), question(), row('assistant', 'DONE')], AGENTS,
    )
    const q = blocksOf(msgs).find((b) => b.type === 'question')
    expect(q && q.type === 'question' && q.answered).toBeFalsy()
    expect(q && q.type === 'question' && q.followedInTurn).toBe(true)
  })

  it('a headless turn closing on its metadata block keeps the question open', () => {
    // Every -p turn persists a metadata row after its blocks; the card must
    // wait for the user's answer message, not flip on the rebuild.
    const msgs = dbMessagesToDisplay(
      [row('user', 'ask me'), question(),
       row('event', '', 'metadata', { type: 'metadata', cost_usd: 0.01, duration_ms: 1200 })],
      AGENTS,
    )
    const q = blocksOf(msgs).find((b) => b.type === 'question')
    expect(q && q.type === 'question' && q.answered).toBeFalsy()
    expect(q && q.type === 'question' && q.followedInTurn).toBe(true)
  })

  it('a question followed by the user answer message is answered (headless)', () => {
    const msgs = dbMessagesToDisplay(
      [row('user', 'ask me'), question(), row('user', 'Red')], AGENTS,
    )
    const q = blocksOf(msgs).find((b) => b.type === 'question')
    expect(q && q.type === 'question' && q.answered).toBe(true)
  })

  it('a plan approval is followed in its turn once the agent continues, resolved by a later message', () => {
    const open = dbMessagesToDisplay([row('user', 'plan it'), planExit()], AGENTS)
    const p1 = blocksOf(open).find((b) => b.type === 'plan')
    expect(p1 && p1.type === 'plan' && (p1.resolved || p1.followedInTurn)).toBeFalsy()
    const done = dbMessagesToDisplay(
      [row('user', 'plan it'), planExit(), row('assistant', 'DONE')], AGENTS,
    )
    const p2 = blocksOf(done).find((b) => b.type === 'plan')
    expect(p2 && p2.type === 'plan' && p2.resolved).toBeFalsy()
    expect(p2 && p2.type === 'plan' && p2.followedInTurn).toBe(true)
    const later = dbMessagesToDisplay(
      [row('user', 'plan it'), planExit(), row('user', 'go ahead')], AGENTS,
    )
    const p3 = blocksOf(later).find((b) => b.type === 'plan')
    expect(p3 && p3.type === 'plan' && p3.resolved).toBe(true)
  })
})
