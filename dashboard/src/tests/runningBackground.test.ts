/**
 * What still runs after a history rebuild: the rows read every stored spawn
 * as finished, so a history frame's `running` field marks the background
 * sub-agents and commands that are still at work running again, and names
 * the delegations still at work whose spawn may be older than the page.
 */
import { describe, it, expect } from 'vitest'
import type { DisplayMessage } from '@/components/chat/types'
import {
  backgroundSpawnOf, markRunningBackground, NO_RUNNING_BACKGROUND, runningBackgroundOf, runningFor, withRunningInView,
} from '@/lib/chat/runningBackground'
import { endedDelegations } from '@/lib/chat/delegateRows'
import type { DbMessageRow } from '@/api/wireEvents'

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
      .toEqual({ agents: [{ id: 'tuA', description: 'x' }], commands: [], delegates: [] })
  })

  it('reads the delegations, and a frame of an older proxy has none', () => {
    const lane = { id: 'dyn-1', description: 'long lane', agent: 'worker' }
    expect(runningBackgroundOf({ delegates: [lane] })).toEqual({ agents: [], commands: [], delegates: [lane] })
    expect(runningBackgroundOf({ agents: [], commands: [], delegates: [] })).toBe(NO_RUNNING_BACKGROUND)
  })
})

describe('backgroundSpawnOf', () => {
  it('names a delegate block by its task id, running while its status says so', () => {
    const running = { type: 'delegate' as const, taskName: 'lane', agent: 'worker', promptPreview: '', status: 'running' as const, _taskId: 'dyn-1' }
    expect(backgroundSpawnOf(running)).toEqual({ list: 'delegates', id: 'dyn-1', active: true, description: 'lane', agent: 'worker' })
    expect(backgroundSpawnOf({ ...running, status: 'completed' as const })?.active).toBe(false)
    // An old row or the live state's by-name fallback: no task id, no list.
    expect(backgroundSpawnOf({ ...running, _taskId: undefined })).toBeNull()
  })
})

describe('markRunningBackground', () => {
  it('turns the listed background spawns active and leaves the rest finished', () => {
    const out = markRunningBackground(view(), {
      agents: [{ id: 'tuA', description: '' }, { id: 'tuF', description: '' }],
      commands: [{ id: 'tuB', description: '' }],
      delegates: [],
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
    expect(markRunningBackground(v, { agents: [{ id: 'tuZ', description: '' }], commands: [], delegates: [] })).toBe(v)
  })

  it('leaves a delegate block as its rows say', () => {
    const v: DisplayMessage[] = [{ id: 'db-3', role: 'assistant', createdAt: '', blocks: [
      { type: 'delegate', taskName: 'lane', agent: 'worker', promptPreview: '', status: 'completed', _taskId: 'dyn-1' },
    ] }]
    const listed = { agents: [{ id: 'tuA', description: '' }], commands: [], delegates: [{ id: 'dyn-1', description: 'lane' }] }
    expect(markRunningBackground(v, listed)).toBe(v)
  })
})

describe('withRunningInView', () => {
  it('adds what the view shows running, once, and keeps the list when it adds nothing', () => {
    const live = view()
    const blocks = live[1].blocks as Array<{ isActive?: boolean }>
    blocks[0].isActive = true   // the background sub-agent the live frames lit
    blocks[1].isActive = true   // a foreground one: never a running background spawn
    const listed = { agents: [], commands: [{ id: 'tuB', description: 'wait' }], delegates: [] }
    expect(withRunningInView(listed, live)).toEqual({
      agents: [{ id: 'tuA', description: 'long reader' }],
      commands: [{ id: 'tuB', description: 'wait' }],
      delegates: [],
    })
    expect(withRunningInView(listed, view())).toBe(listed)
  })

  it('keeps a delegation the view shows running and drops one the rows report ended', () => {
    const lane: DisplayMessage[] = [{ id: 'db-4', role: 'assistant', createdAt: '', blocks: [
      { type: 'delegate', taskName: 'second lane', agent: 'worker', promptPreview: '', status: 'running', _taskId: 'dyn-2' },
    ] }]
    const listed = { agents: [], commands: [], delegates: [{ id: 'dyn-1', description: 'first lane', agent: 'worker' }] }
    // The terminal's refetch drops the spawn rows: the view's running lane is kept.
    expect(withRunningInView(listed, lane).delegates).toEqual([
      { id: 'dyn-1', description: 'first lane', agent: 'worker' },
      { id: 'dyn-2', description: 'second lane', agent: 'worker' },
    ])
    // The first lane's result is among the new rows, its spawn is not: it goes.
    const rows: DbMessageRow[] = [{
      id: 9, role: 'event', content: '', created_at: '', event_type: 'delegate_result',
      event_data: JSON.stringify({ task_id: 'dyn-1', task_name: 'first lane', status: 'completed' }),
    }]
    expect(withRunningInView(listed, [], endedDelegations(rows))).toEqual({ agents: [], commands: [], delegates: [] })
    // One added and one dropped is a change, not the same list.
    const swapped = withRunningInView(listed, lane, endedDelegations(rows))
    expect(swapped).not.toBe(listed)
    expect(swapped.delegates.map((e) => e.id)).toEqual(['dyn-2'])
    expect(withRunningInView(listed, [], new Set(['dyn-9']))).toBe(listed)
  })
})

describe('runningFor', () => {
  it('reads the list only for its own chat', () => {
    const mine = { agents: [{ id: 'tuA', description: '' }], commands: [], delegates: [], chatId: 'chat-1' }
    expect(runningFor(mine, 'chat-1')).toBe(mine)
    expect(runningFor(mine, 'chat-2')).toBe(NO_RUNNING_BACKGROUND)
    expect(runningFor(mine, null)).toBe(NO_RUNNING_BACKGROUND)
    expect(runningFor({ ...mine, chatId: null }, null)).toBe(NO_RUNNING_BACKGROUND)
  })
})
