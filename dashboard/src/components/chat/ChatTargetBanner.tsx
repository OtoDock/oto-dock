import { useEffect, useState } from 'react'

import type { TargetMismatch } from '../../store/chatStore'
import { loadDismissedTargets, recordDismissedTarget } from '../../lib/targetNoticeDismissals'
import MoveChatConfirm from './MoveChatConfirm'

interface Props {
  chatId: string | null
  // The viewed chat's pin-vs-current mismatch from its store slice. null =
  // no mismatch → the banner unmounts (the slice is cleared by the first
  // warmup_ready without the fields, e.g. right after a successful move).
  mismatch: TargetMismatch | null
  // The move op is refused mid-turn — disable the button instead of firing
  // a guaranteed error toast (chat status 'streaming' or 'warming').
  moveDisabled?: boolean
  // Fires the move_chat WS op for the OPEN chat (after the confirm).
  onMove: () => void
}

// Persistent (until X'd) notice that this chat is pinned to an execution
// target different from where the agent's NEW chats run, with the "move it"
// escape hatch. Dismissal is sticky per resolved target
// (lib/targetNoticeDismissals): the same mismatch never re-shows, a NEW
// resolved target shows once again — and it only hides the banner; the
// sidebar kebab keeps the move action reachable.
export default function ChatTargetBanner({ chatId, mismatch, moveDisabled = false, onMove }: Props) {
  const [confirming, setConfirming] = useState(false)
  // Dismissed resolved-target ids for this chat, reloaded on chat switch
  // (the component stays mounted across chat navigation).
  const [dismissed, setDismissed] = useState<Set<string>>(() =>
    chatId ? loadDismissedTargets(chatId) : new Set(),
  )
  useEffect(() => {
    setDismissed(chatId ? loadDismissedTargets(chatId) : new Set())
    setConfirming(false)
  }, [chatId])

  if (!chatId || !mismatch) return null
  if (dismissed.has(mismatch.resolvedTarget)) return null

  const copy = mismatch.pinnedTarget === 'local'
    ? `This chat still runs on the local sandbox — new chats run on ${mismatch.resolvedLabel}.`
    : `This chat runs on ${mismatch.pinnedLabel} — new chats run on ${mismatch.resolvedLabel}.`

  return (
    <>
      <div
        role="status"
        className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-sm border border-amber-300/40 bg-amber-50/40 text-amber-900 dark:bg-amber-500/10 dark:text-amber-200"
      >
        <div className="flex items-center gap-2">
          <span className="truncate min-w-0">{copy}</span>
          <button
            type="button"
            onClick={() => setConfirming(true)}
            disabled={moveDisabled}
            title={moveDisabled ? 'Finish or stop the current turn first' : undefined}
            className="ml-auto shrink-0 px-2 py-1 rounded-sm border border-amber-500/40 hover:bg-amber-500/20 disabled:opacity-50 disabled:hover:bg-transparent"
          >
            Move this chat to {mismatch.resolvedLabel}
          </button>
          <button
            type="button"
            onClick={() => {
              recordDismissedTarget(chatId, mismatch.resolvedTarget)
              setDismissed(loadDismissedTargets(chatId))
            }}
            className="shrink-0 px-1.5 py-0.5 rounded-sm hover:bg-amber-500/20"
            title="Dismiss"
          >
            ×
          </button>
        </div>
      </div>
      {confirming && (
        <MoveChatConfirm
          label={mismatch.resolvedLabel}
          onConfirm={() => { setConfirming(false); onMove() }}
          onCancel={() => setConfirming(false)}
        />
      )}
    </>
  )
}
