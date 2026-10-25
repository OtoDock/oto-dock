/**
 * Compact activity view — grouping model.
 *
 * Collapses maximal consecutive spans of "activity" blocks (tool / thinking /
 * subagent / bgcommand / progress note) into one summary run per span.
 * Everything else (text, permission, question, plan, media, metadata, …)
 * terminates a run and renders as today. Pure derivation — ChatMessages
 * memoizes the result per message alongside the existing
 * pairBgCommandBlocks computation. Also here: when the open turn's newest
 * bubble shows "Working…" (which bubbles are the open turn's is
 * messageBlocks' `openTurnBubbles`).
 */
import type { MessageBlock } from '../components/chat/types'
import { PERSISTED, WIRE } from '../api/wireEvents'
import { ARTIFACT_KINDS, isArtifactKind } from './kinds/artifact'
import { getToolDetail } from './tools/detail'
import { ROLE, toolRole } from './tools/roles'

export type ActivityRun = {
  start: number; end: number           // inclusive block-index span
  toolCount: number                    // tool + subagent + bgcommand pills
  thinkingCount: number
  noteCount: number                    // progress notes: in the span, never running
  failedCount: number                  // tool status 'failed' + subagent/bgcommand failed flags
  running: boolean                     // tool status 'running' OR thinking !done, and while the
                                       // turn is open an active subagent/bgcommand (waited on);
                                       // never after it (a background agent outliving the turn
                                       // must not pulse the chip forever)
  currentLabel: string                 // last running block's short title
  latestNote: string                   // the span's last progress note (the chip's faint line)
  headline: string                     // the running line's words: the agent's newest (a reasoning
                                       // summary's heading still streaming, else latestNote), the
                                       // running step's title only when the span has no note
}

export interface RunOptions {
  /** The bubble belongs to the turn still open (`openTurnBubbles`). */
  turnOpen?: boolean
  /** The notes drawn as asides (`noteAsides`), by block index; the bubble's
   *  asides out of context when absent. A note drawn as text ends a run like
   *  any text. */
  asides?: ReadonlySet<number>
}

/** What decides how a bubble's notes are drawn. */
export interface BubbleContext {
  meeting?: boolean     // a meeting participant's bubble
  finished?: boolean    // the turn is over (`finishedBubble`)
}

/** The longest note still drawn as an aside: a preamble is a sentence or two. */
const ASIDE_MAX_CHARS = 280

/** How long the open turn's newest bubble must stay unchanged, with nothing
 * running, before it says "Working…": longer than a normal gap between two
 * tool calls, shorter than a long command or patch being written. */
export const WORKING_GRACE_MS = 4000

const COLLAPSIBLE_TYPES = new Set<MessageBlock['type']>([
  'tool', 'thinking', 'subagent', 'bgcommand',
])

/** A progress note: the agent's words between its steps, drawn faint. */
export const isNote = (b: MessageBlock): b is Extract<MessageBlock, { type: 'text' }> =>
  'note' in b && b.note === true

// The blocks that ask the person something, answered or not: a question
// tool, a question card, a plan review, or a plan's exit (history keeps it
// before its review; the live page draws only the review). The render kind
// `plan` has no mirror (its frame is `plan_mode`).
const ASKS_PERSON: Partial<Record<MessageBlock['type'], (b: MessageBlock) => boolean>> = {
  [PERSISTED.TOOL]: (b) => 'name' in b && toolRole(b.name) === ROLE.QUESTION,
  [WIRE.QUESTION]: () => true,
  [WIRE.PLAN_REVIEW]: () => true,
  plan: (b) => 'action' in b && b.action === 'exit',
}

function asksPersonAt(b: MessageBlock | undefined): boolean {
  return b !== undefined && (ASKS_PERSON[b.type]?.(b) ?? false)
}

/**
 * The notes of a bubble drawn as asides (faint, folded with the steps), by
 * block index. A note that is a real message is drawn as text instead: every
 * note of a meeting bubble (a participant writes its message before its
 * closing tool call), in a finished bubble the last text when it is a note
 * (the turn's last words) and the last note when the plain text after it is
 * shorter (an answer written before a closing tool, then "Noted."), a note
 * right before a question or a plan the person answers (they read it to
 * answer), and a note longer than a preamble (past ASIDE_MAX_CHARS, or more
 * than one paragraph).
 */
export function noteAsides(blocks: MessageBlock[], ctx: BubbleContext = {}): Set<number> {
  const asides = new Set<number>()
  if (ctx.meeting) return asides
  let lastText = -1
  let lastNote = -1
  blocks.forEach((b, i) => {
    if (b.type === WIRE.TEXT) lastText = i
    if (isNote(b)) lastNote = i
  })
  // The plain words after the last note (every text after it is plain).
  const after = blocks.slice(lastNote + 1)
    .map((b) => (b.type === WIRE.TEXT ? b.content : '')).join('').trim()
  blocks.forEach((b, i) => {
    if (!isNote(b)) return
    const text = b.content.trim()
    if (ctx.finished && (i === lastText || (i === lastNote && after.length < text.length))) return
    if (asksPersonAt(blocks[i + 1])) return
    if (text.length > ASIDE_MAX_CHARS || text.includes('\n\n')) return
    asides.add(i)
  })
  return asides
}

/** A reasoning summary's latest heading (its last `**…**` line): the chip's
 * label while the thinking streams. */
function summaryHeading(content: string): string {
  let heading = ''
  for (const m of content.matchAll(/^\s*\*\*(.+?)\*\*\s*$/gm)) heading = m[1].trim()
  return heading
}

/**
 * Derive activity runs over a message's block list.
 *
 * Indices in `hiddenToolIdx` (a bg-paired Bash tool block — see
 * pairBgCommandBlocks) belong to a span but contribute nothing to its counts:
 * their bgcommand pill is the command's one card. A run whose ONLY indices are
 * hidden (a bg-paired Bash separated from its pill by text) has zero counted
 * content and is DROPPED — a chip there would render "Thought" where today
 * nothing renders; the existing hiddenToolIdx skip keeps those indices blank.
 * A span of notes alone is dropped the same way: its notes render flat.
 */
export function deriveActivityRuns(
  blocks: MessageBlock[],
  hiddenToolIdx: Set<number>,
  options: RunOptions = {},
): ActivityRun[] {
  const runs: ActivityRun[] = []
  let current: ActivityRun | null = null
  // A reasoning summary's heading while its block streams, unless a note
  // came after it: the run's newest words besides its notes.
  let heading = ''
  const asides = options.asides ?? noteAsides(blocks)
  const collapsible = (b: MessageBlock, i: number): boolean =>
    COLLAPSIBLE_TYPES.has(b.type) || (isNote(b) && asides.has(i))

  const finalize = () => {
    if (current && (current.toolCount > 0 || current.thinkingCount > 0)) {
      current.headline = heading || current.latestNote || current.currentLabel
      runs.push(current)
    }
    current = null
    heading = ''
  }

  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]
    if (!collapsible(b, i)) {
      finalize()
      continue
    }
    if (!current) {
      current = {
        start: i, end: i,
        toolCount: 0, thinkingCount: 0, noteCount: 0, failedCount: 0,
        running: false, currentLabel: '', latestNote: '', headline: '',
      }
    }
    current.end = i
    if (hiddenToolIdx.has(i)) continue  // in the span, never counted

    if (isNote(b)) {
      const note = b.content.trim()
      if (note) {
        current.noteCount++
        current.latestNote = note
        heading = ''
      }
      continue
    }
    switch (b.type) {
      case 'tool':
        current.toolCount++
        if (b.status === 'failed') current.failedCount++
        if (b.status === 'running') {
          current.running = true
          current.currentLabel = getToolDetail(b.name, b.summary, b.toolInput) || b.name
        }
        break
      case 'thinking':
        current.thinkingCount++
        if (!b.done) {
          current.running = true
          heading = summaryHeading(b.content)
          current.currentLabel = heading || 'Thinking…'
        }
        break
      case 'subagent':
        current.toolCount++
        if (b.failed) current.failedCount++
        if (options.turnOpen && b.isActive) {
          current.running = true
          current.currentLabel = b.description
        }
        break
      case 'bgcommand':
        current.toolCount++
        if (b.failed) current.failedCount++
        if (options.turnOpen && b.isActive) {
          current.running = true
          current.currentLabel = b.description || b.command
        }
        break
    }
  }
  finalize()
  return runs
}

/**
 * Something in the bubble visibly runs, so "Working…" stays away: a running
 * tool, a thinking block still open, a picture or a video being made, or an
 * active sub-agent or background command in the bubble's TRAILING steps (one
 * started early in the turn, a dev server, must not hide "Working…" for the
 * rest of it). Read whatever the activity view.
 */
export function turnBusy(blocks: MessageBlock[]): boolean {
  let trailing = true
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (b.type === WIRE.METADATA) continue
    // A placeholder card: a picture or a video being made.
    if (isArtifactKind(b.type) && ARTIFACT_KINDS[b.type].placeholder) return true
    switch (b.type) {
      case PERSISTED.TOOL:
        if (b.status === 'running') return true
        break
      case WIRE.THINKING:
        if (!b.done) return true
        break
      case 'subagent':
      case 'bgcommand':
        if (trailing && b.isActive) return true
        break
    }
    if (!COLLAPSIBLE_TYPES.has(b.type) && !isNote(b)) trailing = false
  }
  return false
}

// The cards that wait on the person, and whether one still waits. The
// render kind `permission` has no mirror (its frame is `permission_prompt`).
const WAITS_ON_PERSON: Partial<Record<MessageBlock['type'], (b: MessageBlock) => boolean>> = {
  permission: (b) => !('resolved' in b && b.resolved),
  [WIRE.QUESTION]: (b) => !('answered' in b && b.answered),
  [WIRE.PLAN_REVIEW]: (b) => !('resolved' in b && b.resolved),
}

/** The bubble waits on the person: a permission, a question or a plan review
 * not answered yet, wherever it sits in the bubble. */
export function asksPerson(blocks: MessageBlock[]): boolean {
  return blocks.some((b) => WAITS_ON_PERSON[b.type]?.(b) ?? false)
}
