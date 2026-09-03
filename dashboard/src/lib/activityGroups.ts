/**
 * Compact activity view — grouping model.
 *
 * Collapses maximal consecutive spans of "activity" blocks (tool / thinking /
 * subagent / bgcommand) into one summary run per span. Everything else (text,
 * permission, question, plan, media, metadata, …) terminates a run and renders
 * as today. Pure derivation — ChatMessages memoizes the result per message
 * alongside the existing pairBgCommandBlocks computation.
 */
import type { MessageBlock } from '../components/chat/types'
import { getToolDetail } from '../components/chat/ToolActivity'

export type ActivityRun = {
  start: number; end: number           // inclusive block-index span
  toolCount: number                    // tool + subagent + bgcommand pills
  thinkingCount: number
  failedCount: number                  // tool status 'failed' + subagent/bgcommand failed flags
  running: boolean                     // tool status 'running' OR thinking !done — deliberately
                                       // NOT subagent.isActive/bgcommand.isActive (a background
                                       // agent outliving the turn must not pulse the chip forever)
  currentLabel: string                 // last running block's short title
}

const COLLAPSIBLE_TYPES = new Set<MessageBlock['type']>([
  'tool', 'thinking', 'subagent', 'bgcommand',
])

/**
 * Derive activity runs over a message's block list.
 *
 * Indices in `hiddenToolIdx` (a bg-paired Bash tool block — see
 * pairBgCommandBlocks) belong to a span but contribute nothing to its counts:
 * their bgcommand pill is the command's one card. A run whose ONLY indices are
 * hidden (a bg-paired Bash separated from its pill by text) has zero counted
 * content and is DROPPED — a chip there would render "Thought" where today
 * nothing renders; the existing hiddenToolIdx skip keeps those indices blank.
 */
export function deriveActivityRuns(
  blocks: MessageBlock[],
  hiddenToolIdx: Set<number>,
): ActivityRun[] {
  const runs: ActivityRun[] = []
  let current: ActivityRun | null = null

  const finalize = () => {
    if (current && (current.toolCount > 0 || current.thinkingCount > 0)) {
      runs.push(current)
    }
    current = null
  }

  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]
    if (!COLLAPSIBLE_TYPES.has(b.type)) {
      finalize()
      continue
    }
    if (!current) {
      current = {
        start: i, end: i,
        toolCount: 0, thinkingCount: 0, failedCount: 0,
        running: false, currentLabel: '',
      }
    }
    current.end = i
    if (hiddenToolIdx.has(i)) continue  // in the span, never counted

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
          current.currentLabel = 'Thinking…'
        }
        break
      case 'subagent':
        current.toolCount++
        if (b.failed) current.failedCount++
        break
      case 'bgcommand':
        current.toolCount++
        if (b.failed) current.failedCount++
        break
    }
  }
  finalize()
  return runs
}
