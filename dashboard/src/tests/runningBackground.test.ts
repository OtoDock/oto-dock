/**
 * What still runs after a history rebuild: the rows read every stored spawn
 * as finished, so a history frame's `running` field marks the background
 * sub-agents and commands that are still at work running again.
 */
import { describe, it, expect } from 'vitest'
import type { DisplayMessage } from '@/components/chat/types'
import {
  markRunningBackground, NO_RUNNING_BACKGROUND, runningBackgroundOf, runningFor, withRunningInView,
} from '@/lib/chat/runningBackground'

const view = (): DisplayMessage[] => [
  { id: 'db-1', role: 'user', blocks: [{ type: 'text', content: 'go' }], createdAt: '' },
  {
    id: 'db-2', role: 'assistant', createdAt: '', blocks: [
      { type: 'subagent', description: 'long reader', subagentType: '', isActive: false, _toolId: 'tuA', _background: true },
      { type: 'subagent', description: 'fg helper', subagentType: '', isActive: false, _toolId: 'tuF', _background: false },
      { type: 'bgcommand', command: 'sleep 150', isActive: false, _toolId: 'tuB' },
      { type: 'bgcommand', command: 'make', isActive: false, _toolId: 'tuDone' },
    ],
  },
]

describe('runningBackgroundOf', () => {
  it('reads the field and treats an absent one as nothing', () => {
    expect(runningBackgroundOf(undefined)).toBe(NO_RUNNING_BACKGROUND)
    expect(runningBackgroundOf({})).toBe(NO_RUNNING_BACKGROUND)
    expect(runningBackgroundOf({ agents: [{ id: 'tuA', description: 'x' }, { id: '', description: 'y' }] }))
      .toEqual({ agents: [{ id: 'tuA', description: 'x' }], commands: [] })
  })
})

describe('markRunningBackground', () => {
  it('turns the listed background spawns active and leaves the rest finished', () => {
    const out = markRunningBackground(view(), {
      agents: [{ id: 'tuA', description: '' }, { id: 'tuF', description: '' }],
      commands: [{ id: 'tuB', description: '' }],
    })
    const blocks = out[1].blocks as Array<{ _toolId?: string; isActive?: boolean }>
    expect(blocks.map((b) => [b._toolId, b.isActive])).toEqual([
      ['tuA', true],      // a background sub-agent still at work
      ['tuF', false],     // a foreground one is never a running badge after its turn
      ['tuB', true],
      ['tuDone', false],
    ])
  })

  it('returns the same list when nothing is listed or nothing matched', () => {
    const v = view()
    expect(markRunningBackground(v, NO_RUNNING_BACKGROUND)).toBe(v)
    expect(markRunningBackground(v, { agents: [{ id: 'tuZ', description: '' }], commands: [] })).toBe(v)
  })
})

describe('withRunningInView', () => {
  it('adds what the view shows running, once, and keeps the list when it adds nothing', () => {
    const live = view()
    const blocks = live[1].blocks as Array<{ isActive?: boolean }>
    blocks[0].isActive = true   // the background sub-agent the live frames lit
    blocks[1].isActive = true   // a foreground one: never a running background spawn
    const listed = { agents: [], commands: [{ id: 'tuB', description: 'wait' }] }
    expect(withRunningInView(listed, live)).toEqual({
      agents: [{ id: 'tuA', description: 'long reader' }],
      commands: [{ id: 'tuB', description: 'wait' }],
    })
    expect(withRunningInView(listed, view())).toBe(listed)
  })
})

describe('runningFor', () => {
  it('reads the list only for its own chat', () => {
    const mine = { agents: [{ id: 'tuA', description: '' }], commands: [], chatId: 'chat-1' }
    expect(runningFor(mine, 'chat-1')).toBe(mine)
    expect(runningFor(mine, 'chat-2')).toBe(NO_RUNNING_BACKGROUND)
    expect(runningFor(mine, null)).toBe(NO_RUNNING_BACKGROUND)
    expect(runningFor({ ...mine, chatId: null }, null)).toBe(NO_RUNNING_BACKGROUND)
  })
})
