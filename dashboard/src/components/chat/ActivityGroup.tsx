import { useState } from 'react'
import BlockRenderer from './ChatBlockRenderer'
import { useSearch } from '../../contexts/SearchContext'
import type { MessageBlock } from './types'
import type { ActivityRun } from '../../lib/activityGroups'
import type { BgCommandPair } from '../../lib/messageBlocks'

interface Props {
  run: ActivityRun
  /** The message's FULL block array — the group renders its span by index so
   * blockId/blockOrder stay identical to the flat (Detailed) layout. */
  blocks: MessageBlock[]
  msgId: string
  msgIdx: number
  /** Bg-paired Bash tool indices (pairBgCommandBlocks) — re-skipped INSIDE
   * the expanded span exactly like the flat loop does. */
  hiddenToolIdx: Set<number>
  bgPairs: Map<number, BgCommandPair>
  chatId?: string
  /** Message-level slug fallback already applied by the caller (msgSlug || agentName). */
  agentName?: string
  onPermissionRespond: (requestId: string, approved: boolean) => void
}

function spanIndices(run: ActivityRun): number[] {
  const out: number[] = []
  for (let i = run.start; i <= run.end; i++) out.push(i)
  return out
}

/**
 * Compact activity view — one collapsed chip per run of activity blocks
 * (tool / thinking / subagent / bgcommand), expanding to the real cards.
 *
 * The chip is a single muted line on the chat background (no card border):
 * finished runs show "N steps" (+ amber "M failed"), thinking-only runs show
 * "Thought", live runs pulse with "Working · {current action}".
 */
export default function ActivityGroup({
  run,
  blocks,
  msgId,
  msgIdx,
  hiddenToolIdx,
  bgPairs,
  chatId,
  agentName,
  onPermissionRespond,
}: Props) {
  const [expanded, setExpanded] = useState(false)
  const { query } = useSearch()

  const renderSpanBlock = (i: number) => {
    // Exact forwarding contract of the flat assistant loop (ChatMessages) for
    // the props a collapsible block can consume — bgPair is load-bearing:
    // BgCommandInfo reads its command/output EXCLUSIVELY from it.
    return (
      <BlockRenderer
        key={i}
        block={blocks[i]}
        blockId={`${msgId}-b${i}`}
        blockOrder={msgIdx * 1000 + i}
        isUserMessage={false}
        chatId={chatId}
        agentName={agentName}
        onPermissionRespond={onPermissionRespond}
        bgPair={bgPairs.get(i)}
      />
    )
  }

  const steps = `${run.toolCount} ${run.toolCount === 1 ? 'step' : 'steps'}`

  return (
    <div>
      <button
        type="button"
        data-testid="activity-chip"
        onClick={() => setExpanded(v => !v)}
        className="flex w-fit max-w-full items-center gap-1.5 py-0.5 text-xs text-p-text-light hover:text-p-text-secondary transition-colors cursor-pointer"
      >
        <span
          className={`shrink-0 text-[9px] transform transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden
        >
          {'▶'}
        </span>
        {run.running ? (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-brand animate-pulse shrink-0" />
            <span className="font-medium animate-pulse shrink-0">Working</span>
            {run.currentLabel && (
              <span className="truncate min-w-0">{'·'} {run.currentLabel}</span>
            )}
            {run.toolCount > 0 && (
              <span className="shrink-0">{'·'} {steps}</span>
            )}
          </>
        ) : run.toolCount > 0 ? (
          <>
            <svg className="w-3 h-3 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span className="font-medium shrink-0">{steps}</span>
            {run.failedCount > 0 && (
              <span className="text-p-accent-yellow shrink-0">{'·'} {run.failedCount} failed</span>
            )}
          </>
        ) : (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-p-accent-purple/60 shrink-0" />
            <span className="font-medium">Thought</span>
          </>
        )}
      </button>
      {expanded && (
        <div
          data-testid="activity-group-expanded"
          className="mt-1 border-l-2 border-p-border-light/70 pl-3 space-y-3"
        >
          {spanIndices(run).map(i => (hiddenToolIdx.has(i) ? null : renderSpanBlock(i)))}
        </div>
      )}
      {/* While a FindBar query is active, keep the run's thinking blocks
          MOUNTED but hidden (ThinkingBlock's own hidden-while-query pattern):
          among collapsible renderers only ThinkingBlock registers search
          matches, and late registration would scramble global match order.
          The group stays visually collapsed — no layout shift per keystroke. */}
      {!expanded && !!query && run.thinkingCount > 0 && (
        <div className="hidden" data-testid="activity-group-search-mount">
          {spanIndices(run).map(i =>
            blocks[i].type === 'thinking' ? renderSpanBlock(i) : null)}
        </div>
      )}
    </div>
  )
}
