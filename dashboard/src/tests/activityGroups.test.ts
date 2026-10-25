/**
 * Compact activity view — run derivation. Runs are maximal consecutive spans
 * of tool/thinking/subagent/bgcommand blocks; anything else splits them.
 * Bg-paired hidden Bash indices belong to a span but are never counted, and
 * a span with zero counted content produces NO run (no phantom chip).
 */
import { describe, it, expect } from 'vitest'
import { asksPerson, deriveActivityRuns, noteAsides, turnBusy } from '@/lib/activityGroups'
import { openTurnBubbles } from '@/lib/messageBlocks'
import type { DisplayMessage, MessageBlock } from '@/components/chat/types'

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

const note = (content = 'Next I list it.'): MessageBlock => ({ type: 'text', content, note: true })

describe('deriveActivityRuns — progress notes', () => {
  it('keeps a note inside the span, counted apart and never running', () => {
    const runs = deriveActivityRuns([tool(), note('First.'), tool(), note('Then.')], noHidden)
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({ start: 0, end: 3, toolCount: 2, noteCount: 2, running: false, latestNote: 'Then.' })
  })

  it('drops a span of notes alone, so they render flat', () => {
    expect(deriveActivityRuns([note(), note()], noHidden)).toEqual([])
    const runs = deriveActivityRuns([note('Lead.'), text(), tool()], noHidden)
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({ start: 2, end: 2, noteCount: 0 })
  })

  it('a plain text block still ends a run, a note does not', () => {
    const runs = deriveActivityRuns([tool(), note(), tool(), text('Answer'), tool()], noHidden)
    expect(runs.map((r) => [r.start, r.end])).toEqual([[0, 2], [4, 4]])
  })
})

describe('deriveActivityRuns — while the turn is open', () => {
  it('counts an active sub-agent or background command as running only while open', () => {
    const blocks = [subagent({ isActive: true, description: 'scan the repo' })]
    expect(deriveActivityRuns(blocks, noHidden)[0].running).toBe(false)
    expect(deriveActivityRuns(blocks, noHidden, { turnOpen: true })[0]).toMatchObject({
      running: true, currentLabel: 'scan the repo' })
    const cmd = [bgcommand({ isActive: true, command: 'sleep 40' })]
    expect(deriveActivityRuns(cmd, noHidden, { turnOpen: true })[0]).toMatchObject({
      running: true, currentLabel: 'sleep 40' })
    expect(deriveActivityRuns(cmd, noHidden, { turnOpen: false })[0].running).toBe(false)
  })

  it('a finished sub-agent never runs, open or not', () => {
    expect(deriveActivityRuns([subagent({ isActive: false })], noHidden, { turnOpen: true })[0].running).toBe(false)
  })

  it('labels open thinking with its latest summary heading', () => {
    const runs = deriveActivityRuns(
      [thinking({ done: false, content: '**Planning the steps**\n\nfirst\n\n**Checking the folder**\n\nthen' })],
      noHidden)
    expect(runs[0].currentLabel).toBe('Checking the folder')
  })
})

const bubble = (id: string, blocks: MessageBlock[], over: Partial<DisplayMessage> = {}): DisplayMessage =>
  ({ id, role: 'assistant', blocks, createdAt: '2026-10-20T00:00:00Z', ...over })
const user = (id: string): DisplayMessage => ({ id, role: 'user', blocks: [text('hi')], createdAt: '2026-10-20T00:00:00Z' })
const meta: MessageBlock = { type: 'metadata', costUsd: 0, durationMs: 1 }

describe('openTurnBubbles', () => {
  it('is empty while the chat does not stream', () => {
    expect(openTurnBubbles([user('u1'), bubble('a1', [tool()])], false).size).toBe(0)
  })

  it('takes the unfinished bubbles back to the last finished one, over a steer', () => {
    const msgs = [bubble('a0', [text(), meta]), user('u1'), bubble('a1', [subagent({ isActive: true })]),
      { ...user('steer'), steered: true }, bubble('a2', [tool({ status: 'running' })])]
    expect([...openTurnBubbles(msgs, true)]).toEqual(['a2', 'a1'])
  })

  it("stops at the turn's own message: an earlier errored bubble is not this turn's", () => {
    // An untyped error ends a turn with neither metadata nor a turn-ended card.
    const errored = bubble('a0', [bgcommand({ isActive: true }), text('**Error:** boom')])
    const msgs = [user('u0'), errored, user('u1'), bubble('a1', [tool({ status: 'running' })])]
    expect([...openTurnBubbles(msgs, true)]).toEqual(['a1'])
  })

  it('names none while a server turn starts before its first frame', () => {
    expect(openTurnBubbles([user('u1'), bubble('a1', [text(), meta])], true).size).toBe(0)
  })

  it('stops at a turn that ended short and at a delegate card', () => {
    const ended: MessageBlock = { type: 'system', subtype: 'turn_ended', message: 'x' }
    expect(openTurnBubbles([bubble('a1', [ended]), bubble('a2', [])], true)).toEqual(new Set(['a2']))
    const card = bubble('d1', [text()], { delegateCard: { kind: 'result', key: 'k' } })
    expect(openTurnBubbles([card, bubble('a2', [])], true)).toEqual(new Set(['a2']))
  })
})

describe('turnBusy and asksPerson', () => {
  it('is busy with a running tool, open thinking or a picture being made', () => {
    expect(turnBusy([tool({ status: 'running' }), text()])).toBe(true)
    expect(turnBusy([thinking({ done: false })])).toBe(true)
    expect(turnBusy([{ type: 'image_generating', promptPreview: 'p', model: 'm' }])).toBe(true)
    expect(turnBusy([tool(), text()])).toBe(false)
  })

  it('counts an active background item only in the trailing steps', () => {
    expect(turnBusy([text(), bgcommand({ isActive: true }), note()])).toBe(true)
    // A dev server started early, then text: quiet now, "Working…" may show.
    expect(turnBusy([bgcommand({ isActive: true }), text('Started it.'), tool()])).toBe(false)
  })

  it('asks the person while a card waits, wherever it sits', () => {
    const perm: MessageBlock = { type: 'permission', requestId: 'r', toolName: 'Bash', toolInput: {} }
    expect(asksPerson([perm, tool({ status: 'done' })])).toBe(true)
    expect(asksPerson([{ ...perm, resolved: true } as MessageBlock])).toBe(false)
    expect(asksPerson([{ type: 'question', toolName: 'q', toolInput: {} }])).toBe(true)
    expect(asksPerson([{ type: 'plan_review', requestId: 'p', plan: 'x', toolInput: {} }])).toBe(true)
  })
})

describe('noteAsides: a note that is a real message is drawn as text', () => {
  const long = 'x'.repeat(281)

  it('folds a short preamble before a tool, and keeps it folded in a turn still open', () => {
    expect([...noteAsides([note('I will list it.'), tool()])]).toEqual([0])
    expect([...noteAsides([note('I will list it.'), tool(), note('Then the count.')], {})]).toEqual([0, 2])
  })

  it('draws every note of a meeting bubble as text', () => {
    expect(noteAsides([note('My view: ship it.'), tool({ name: 'mcp__meetings-mcp__direct_to' })],
      { meeting: true }).size).toBe(0)
  })

  it("draws a finished bubble's last words as text when they are a note", () => {
    const blocks = [note('I will write it.'), tool(), note('Here is the report.'), tool(), meta]
    expect([...noteAsides(blocks, { finished: true })]).toEqual([0])
    // With a longer answer after it, the last note is a preamble again.
    expect([...noteAsides([note('Checking.'), tool(), text('The folder holds three files.'), meta],
      { finished: true })]).toEqual([0])
    // A sign-off shorter than the note: the note is the answer.
    expect(noteAsides([note('Checking.'), tool(), text('Done.'), meta], { finished: true }).size).toBe(0)
    // While the turn runs, it is a preamble.
    expect([...noteAsides([note('Checking.'), tool(), text('Done.')])]).toEqual([0])
  })

  it('reads an answer written before a closing tool as text when only a sign-off follows', () => {
    const answer = note('Your meeting is at 3 pm tomorrow with Anna.')
    const save = tool({ name: 'mcp__memory-mcp__memory' })
    expect(noteAsides([answer, save, text('Noted.'), meta], { finished: true }).size).toBe(0)
    // Only the last note: an earlier preamble still folds.
    expect([...noteAsides([note('I will look it up.'), tool(), answer, save, text('Noted.'), meta],
      { finished: true })]).toEqual([0])
    // A worker's report before attach_result_files, then a short close.
    const report = note('The build passes and the two flaky tests are fixed.')
    expect(noteAsides([report, tool({ name: 'mcp__delegation-mcp__attach_result_files' }),
      text('Attached.'), meta], { finished: true }).size).toBe(0)
  })

  it('draws a note longer than a preamble, or of more than one paragraph, as text', () => {
    expect(noteAsides([note(long), tool()]).size).toBe(0)
    expect(noteAsides([note('First part.\n\nSecond part.'), tool()]).size).toBe(0)
    expect(noteAsides([note('\n\nOne line after its separator.'), tool()]).size).toBe(1)
  })

  it('a note drawn as text ends a run like any text', () => {
    const blocks = [tool(), note('My message to the room.'), tool()]
    const runs = deriveActivityRuns(blocks, noHidden, { asides: new Set() })
    expect(runs.map((r) => [r.start, r.end, r.noteCount])).toEqual([[0, 0, 0], [2, 2, 0]])
  })

  it('draws a note right before a step or card that asks the person as text', () => {
    const ask = note('Two configs: 8080 or 8400?')
    expect(noteAsides([ask, tool({ name: 'AskUserQuestion', status: 'running' })]).size).toBe(0)
    expect(noteAsides([ask, tool({ name: 'request_user_input' })]).size).toBe(0)
    expect(noteAsides([ask, { type: 'question', toolName: 'AskUserQuestion', toolInput: {}, answered: true }]).size)
      .toBe(0)
    expect(noteAsides([ask, { type: 'plan_review', requestId: 'r', plan: 'p', toolInput: {} }]).size).toBe(0)
    // A preamble to a command that then asks permission: its tool sits between.
    expect([...noteAsides([note('I will remove the build.'), tool({ status: 'running' }),
      { type: 'permission', requestId: 'r1', toolName: 'Bash', toolInput: { command: 'rm -r build' } }])])
      .toEqual([0])
  })

  it('reads a note before a plan the same live and from history', () => {
    const review: MessageBlock = { type: 'plan_review', requestId: 'r', plan: 'p', toolInput: {} }
    const live = [note("Here's my plan."), review]
    const history = [note("Here's my plan."), { type: 'plan', action: 'exit' } as MessageBlock, review]
    expect(noteAsides(live).size).toBe(0)
    expect(noteAsides(history).size).toBe(0)
    // Entering plan mode asks nothing.
    expect([...noteAsides([note('Planning first.'), { type: 'plan', action: 'enter' } as MessageBlock, tool()])])
      .toEqual([0])
  })
})

describe("the chip's running line: the agent's words before a step's name", () => {
  it('reads the latest note while a tool runs, the tool when the span has no note', () => {
    const running = tool({ status: 'running', summary: 'ls -la' })
    expect(deriveActivityRuns([note('I will list the folder.'), running], noHidden)[0])
      .toMatchObject({ running: true, headline: 'I will list the folder.', currentLabel: 'ls -la' })
    expect(deriveActivityRuns([running], noHidden)[0]).toMatchObject({ headline: 'ls -la' })
    const later = deriveActivityRuns([note('First.'), tool(), note('Now the count.'), running], noHidden)[0]
    expect(later.headline).toBe('Now the count.')
  })

  it('a reasoning summary streaming after the note is newer; a bare "Thinking…" is not', () => {
    const heading = thinking({ done: false, content: '**Checking the folder**\n\nthen' })
    expect(deriveActivityRuns([note('First.'), tool(), heading], noHidden)[0].headline).toBe('Checking the folder')
    const bare = thinking({ done: false, content: 'no heading yet' })
    expect(deriveActivityRuns([note('First.'), tool(), bare], noHidden)[0]).toMatchObject({
      headline: 'First.', currentLabel: 'Thinking…' })
  })

  it("a waited sub-agent's description shows only when the span has no note", () => {
    const sub = subagent({ isActive: true, description: 'scan the repo' })
    expect(deriveActivityRuns([sub], noHidden, { turnOpen: true })[0].headline).toBe('scan the repo')
    expect(deriveActivityRuns([note('A sub-agent scans it.'), sub], noHidden, { turnOpen: true })[0].headline)
      .toBe('A sub-agent scans it.')
  })
})
