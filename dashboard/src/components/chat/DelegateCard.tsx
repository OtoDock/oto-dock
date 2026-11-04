import { useId, type ReactNode } from 'react'
import type { DelegateCardMark, MessageBlock } from './types'
import { delegatePreviewText } from '../../lib/messageBlocks'
import { useSearch } from '../../contexts/useSearch'
import { WIRE } from '../../api/wireEvents'
import { DelegateResultFiles } from './DelegateTaskInfo'

interface Props {
  card: DelegateCardMark
  blocks: MessageBlock[]
  /** The person's choice for this card (the page's state, never stored). */
  open: boolean
  onToggle: () => void
  /** The message's header row: avatar, agent name, badge, time. */
  header: ReactNode
  /** The card's own controls, outside the toggle (a link to the worker's
   *  chat): a click on one never toggles the card. */
  actions?: ReactNode
  /** The whole message as an expanded message renders it. */
  children: ReactNode
}

/**
 * A message to the agent from another agent or from the platform: a
 * delegate's report in the delegating chat, a worker lane's incoming prompt
 * in its own chat, the platform's own wake. Collapsed by default: the header
 * and a two-line preview in one button (a click, Enter or Space opens it),
 * the attached files as chips and the actions outside it. A find-in-chat
 * query that matches inside the card shows it expanded while the query
 * lasts, so its matches register in the list's order.
 */
export default function DelegateCard({ card, blocks, open, onToggle, header, actions, children }: Props) {
  const bodyId = useId()
  const { query } = useSearch()
  const needle = query.trim().toLowerCase()
  const searchHit = !!needle && blocks.some(
    (b) => b.type === WIRE.TEXT && b.content.toLowerCase().includes(needle),
  )
  const expanded = open || searchHit
  const preview = expanded ? '' : delegatePreviewText(card.kind, blocks)
  // Only a result carries the worker's files (a prompt's attachments are
  // its own blocks).
  const files = blocks.find((b): b is Extract<MessageBlock, { type: 'delegate_files' }> => b.type === 'delegate_files')

  return (
    <div
      className="rounded-card bg-surface-card shadow-elevation-1"
      data-testid="delegate-card"
      data-kind={card.kind}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={bodyId}
        className="w-full text-left rounded-control px-3 py-2 hover:bg-surface-hover transition-colors
                   focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring/60"
      >
        <div className="flex items-center gap-2 min-w-0">
          {header}
          <svg
            className={`ml-auto w-4 h-4 shrink-0 text-tertiary transition-transform ${expanded ? 'rotate-90' : ''}`}
            fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
        </div>
        {preview && (
          <p className="mt-1.5 text-sm text-secondary line-clamp-2" data-testid="delegate-card-preview">
            {preview}
          </p>
        )}
      </button>
      {(actions || (!expanded && files && files.files.length > 0)) && (
        <div className="px-3 pb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          {!expanded && files && files.files.length > 0 && (
            <DelegateResultFiles files={files.files} skipped={[]} compact />
          )}
          {actions}
        </div>
      )}
      <div id={bodyId} hidden={!expanded} className="px-3 pb-3 pt-1">
        {expanded ? children : null}
      </div>
    </div>
  )
}
