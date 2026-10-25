import { useState } from 'react'

import type { TargetMismatch } from '../../store/chatStore'
import { placePhrase, useChatTargets } from '../../api/remoteMachines'
import { isLocalTarget } from '../../lib/placement'
import { loadDismissedTargets, recordDismissedTarget } from '../../lib/targetNoticeDismissals'
import { ChatTargetsMenu } from './RunsOnChip'

interface Props {
  chatId: string | null
  // The viewed chat's pin-vs-current mismatch from its store slice. null =
  // no mismatch → the banner unmounts (the slice is cleared by the first
  // warmup_ready without the fields, e.g. right after a successful move).
  mismatch: TargetMismatch | null
  // A move is refused mid-turn — disable the button instead of firing
  // a guaranteed error toast (chat status 'streaming' or 'warming').
  moveDisabled?: boolean
  // Kept for the callers that still pass it: the button opens the chat's
  // places and moves over HTTP (the "Runs on" chip's menu), never this.
  onMove?: () => void
}

const BUTTON = 'ml-auto shrink-0 px-2 py-1 rounded-control border border-current hover:underline '
  + 'disabled:opacity-50 disabled:hover:no-underline'

// Persistent (until X'd) notice that this chat is pinned to an execution
// target different from where the agent's NEW chats run, with the "move it"
// escape hatch: the chat's places as a menu (the chip's). Dismissal is
// sticky per resolved target (lib/targetNoticeDismissals): the same mismatch
// never re-shows, a NEW resolved target shows once again — and it only hides
// the banner; the chip and the sidebar kebab keep the move reachable. A
// place a person chose (`place_chosen` in the chat's targets answer) is no
// mismatch to point at: the banner stays hidden.
export default function ChatTargetBanner({ chatId, mismatch, moveDisabled = false }: Props) {
  const { data: targets } = useChatTargets(chatId)
  // Dismissed resolved-target ids for this chat, reloaded on chat switch
  // (the component stays mounted across chat navigation).
  const [dismissed, setDismissed] = useState<Set<string>>(() =>
    chatId ? loadDismissedTargets(chatId) : new Set(),
  )
  // The chat switch reloads the dismissals, in render.
  const [forChat, setForChat] = useState(chatId)
  if (forChat !== chatId) {
    setForChat(chatId)
    setDismissed(chatId ? loadDismissedTargets(chatId) : new Set())
  }

  if (!chatId || !mismatch) return null
  if (targets?.place_chosen) return null
  if (dismissed.has(mismatch.resolvedTarget)) return null

  const copy = isLocalTarget(mismatch.pinnedTarget)
    ? `This chat still runs on ${placePhrase(mismatch.pinnedTarget, mismatch.pinnedLabel)}. New chats run on ${mismatch.resolvedLabel}.`
    : `This chat runs on ${mismatch.pinnedLabel}. New chats run on ${mismatch.resolvedLabel}.`
  // Why the menu cannot open: the server's sentence, else the turn.
  const blocked = targets?.lock?.sentence || (targets && !targets.can_move ? targets.refusal : '')

  return (
    <div
      role="status"
      className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-card border border-status-warning-border bg-status-warning-bg text-status-warning-fg"
    >
      <div className="flex items-center gap-2">
        <span className="truncate min-w-0">{copy}</span>
        {targets && !blocked ? (
          <ChatTargetsMenu
            key={chatId}
            chatId={chatId}
            data={targets}
            disabled={moveDisabled}
            title={moveDisabled ? 'Finish or stop the current turn first' : 'Choose where this chat runs'}
            className={BUTTON}
          >
            Move this chat…
          </ChatTargetsMenu>
        ) : (
          <button type="button" disabled title={blocked || undefined} className={BUTTON}>
            Move this chat…
          </button>
        )}
        <button
          type="button"
          onClick={() => {
            recordDismissedTarget(chatId, mismatch.resolvedTarget)
            setDismissed(loadDismissedTargets(chatId))
          }}
          className="shrink-0 px-1.5 py-0.5 rounded-control hover:underline"
          title="Dismiss"
        >
          ×
        </button>
      </div>
    </div>
  )
}
