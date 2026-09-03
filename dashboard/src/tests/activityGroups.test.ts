/**
 * Compact activity view — run derivation. Runs are maximal consecutive spans
 * of tool/thinking/subagent/bgcommand blocks; anything else splits them.
 * Bg-paired hidden Bash indices belong to a span but are never counted, and
 * a span with zero counted content produces NO run (no phantom chip).
 */
import { describe, it, expect } from 'vitest'
import { deriveActivityRuns } from '@/lib/activityGroups'
import type { MessageBlock } from '@/components/chat/types'

type ToolBlock = Extract<MessageBlock, { type: 'tool' }>
type ThinkingBlock = Extract<MessageBlock, { type: 'thinking' }>
type SubagentBlock = Extract<MessageBlock, { type: 'subagent' }>
type BgCommandBlock = Extract<MessageBlock, { type: 'bgcommand' }>

const text = (content = 'hello'): MessageBlock => ({ type: 'text', content })
const tool = (over: Partial<ToolBlock> = {}): MessageBlock =>
  ({ type: 'tool', name: 'Bash', toolId: 't1', summary: '', status: 'done', ...over })
const thinking = (over: Partial<ThinkingBlock> = {}): MessageBlock =>
  ({ type: 'thinking', content: 'pondering', collapsed: true, done: true, ...over })
const subagent = (over: Partial<SubagentBlock> = {}): MessageBlock =>
  ({ type: 'subagent', description: 'explore repo', subagentType: 'Explore', ...over })
const bgcommand = (over: Partial<BgCommandBlock> = {}): MessageBlock =>
  ({ type: 'bgcommand', command: 'sleep 100', ...over })

const noHidden = new Set<number>()

describe('deriveActivityRuns — span boundaries', () => {
  it('returns no runs for an empty message', () => {
    expect(deriveActivityRuns([], noHidden)).toEqual([])
  })

  it('splits runs on text blocks', () => {
    const runs = deriveActivityRuns([tool(), thinking(), text(), tool()], noHidden)
    expect(runs).toHaveLength(2)
    expect(runs[0]).toMatchObject({ start: 0, end: 1, toolCount: 1, thinkingCount: 1 })
    expect(runs[1]).toMatchObject({ start: 3, end: 3, toolCount: 1, thinkingCount: 0 })
  })

  it('splits runs on permission, question and media blocks', () => {
    const blocks: MessageBlock[] = [
      tool(),
      { type: 'permission', requestId: 'r1', toolName: 'Bash', toolInput: { command: 'rm x' } },
      tool(),
      { type: 'question', toolName: 'AskUserQuestion', toolInput: { question: 'Which?' } },
      tool(),
      { type: 'images', images: [] },
      tool(),
    ]
    const runs = deriveActivityRuns(blocks, noHidden)
    expect(runs.map(r => [r.start, r.end])).toEqual([[0, 0], [2, 2], [4, 4], [6, 6]])
    for (const r of runs) expect(r.toolCount).toBe(1)
  })

  it('collapses a single activity block into its own run', () => {
    const runs = deriveActivityRuns([text(), subagent(), text()], noHidden)
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({ start: 1, end: 1, toolCount: 1 })
  })
})

describe('deriveActivityRuns — hidden bg-paired indices', () => {
  it('keeps hidden indices inside the span but never counts them', () => {
    // [hidden Bash tool, bgcommand pill, ordinary tool] — one span, 2 counted.
    const blocks: MessageBlock[] = [
      tool({ toolId: 'bg1' }),
      bgcommand({ _toolId: 'bg1' }),
      tool({ name: 'Read', toolId: 't2' }),
    ]
    const runs = deriveActivityRuns(blocks, new Set([0]))
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({ start: 0, end: 2, toolCount: 2, thinkingCount: 0 })
  })

  it('drops a run whose only indices are hidden (no phantom chip)', () => {
    // A bg-paired Bash separated from its pill by text: today nothing renders
    // there — a chip would show "Thought" out of nowhere.
    const blocks: MessageBlock[] = [text(), tool({ toolId: 'bg1' }), text()]
    expect(deriveActivityRuns(blocks, new Set([1]))).toEqual([])
  })

  it('does not let a hidden running tool mark the run as running', () => {
    const blocks: MessageBlock[] = [
      tool({ toolId: 'bg1', status: 'running' }),
      bgcommand({ _toolId: 'bg1', isActive: true }),
    ]
    const runs = deriveActivityRuns(blocks, new Set([0]))
    expect(runs).toHaveLength(1)
    expect(runs[0].running).toBe(false)
  })
})

describe('deriveActivityRuns — counting', () => {
  it('counts tool + subagent + bgcommand as steps and failures from all three', () => {
    const blocks: MessageBlock[] = [
      tool({ status: 'failed' }),
      subagent({ failed: true }),
      bgcommand({ failed: true }),
      tool({ name: 'Read', toolId: 't2' }),
    ]
    const runs = deriveActivityRuns(blocks, noHidden)
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({ toolCount: 4, failedCount: 3, running: false })
  })

  it('reports a thinking-only run with zero tools', () => {
    const runs = deriveActivityRuns([thinking()], noHidden)
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({ toolCount: 0, thinkingCount: 1, running: false })
  })
})

describe('deriveActivityRuns — running + currentLabel', () => {
  it('marks running from a running tool and labels it via getToolDetail', () => {
    const runs = deriveActivityRuns(
      [tool({ name: 'Read', status: 'running', toolInput: { file_path: '/tmp/report.xlsx' } })],
      noHidden,
    )
    expect(runs[0].running).toBe(true)
    expect(runs[0].currentLabel).toBe('/tmp/report.xlsx')
  })

  it('falls back to the tool name when no detail is derivable', () => {
    const runs = deriveActivityRuns(
      [tool({ name: 'mcp__display__display_ui', status: 'running' })],
      noHidden,
    )
    expect(runs[0].currentLabel).toBe('mcp__display__display_ui')
  })

  it('marks running from open (!done) thinking with the Thinking… label', () => {
    const runs = deriveActivityRuns([thinking({ done: false, content: '' })], noHidden)
    expect(runs[0]).toMatchObject({ running: true, currentLabel: 'Thinking…' })
  })

  it('deliberately ignores subagent/bgcommand isActive for running', () => {
    // A background agent outliving the turn must not pulse the chip forever.
    const runs = deriveActivityRuns(
      [subagent({ isActive: true }), bgcommand({ isActive: true })],
      noHidden,
    )
    expect(runs[0].running).toBe(false)
    expect(runs[0].currentLabel).toBe('')
  })

  it('takes currentLabel from the LAST running block in the span', () => {
    const runs = deriveActivityRuns(
      [
        tool({ name: 'Read', status: 'running', toolInput: { file_path: '/a.txt' } }),
        thinking({ done: false, content: '' }),
      ],
      noHidden,
    )
    expect(runs[0].currentLabel).toBe('Thinking…')

    const runs2 = deriveActivityRuns(
      [
        thinking({ done: false, content: '' }),
        tool({ name: 'Read', status: 'running', toolInput: { file_path: '/a.txt' } }),
      ],
      noHidden,
    )
    expect(runs2[0].currentLabel).toBe('/a.txt')
  })

  it('reports done runs as not running', () => {
    const runs = deriveActivityRuns([tool(), thinking()], noHidden)
    expect(runs[0]).toMatchObject({ running: false, currentLabel: '' })
  })
})
