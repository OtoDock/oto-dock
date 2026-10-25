import { useEffect, useState } from 'react'

interface Props {
  // The fallback_reason value from the last warmup_ready event. The
  // banner triggers on `user-override-offline` and ignores other reasons
  // (admin-target hard-fails surface as a target_unavailable system block
  // in the chat stream, not here).
  fallbackReason: string | null | undefined
  // Optional machine name to include in the message ("'Linux-box' is
  // offline…"). Falls back to a generic phrasing when missing.
  machineName?: string
  // Auto-dismiss after this many ms (default 8000).
  durationMs?: number
}

// Brief amber strip rendered above the chat input / task input. Soft
// notice that the user's per-agent override is offline and the session
// fell back to the agent default / local. Does not block input.
export default function RemoteFallbackBanner({
  fallbackReason, machineName, durationMs = 8000,
}: Props) {
  // Shown for the window after each new override reason; the timer hides it.
  const [seenReason, setSeenReason] = useState(fallbackReason)
  const [hidden, setHidden] = useState(false)
  if (seenReason !== fallbackReason) {
    setSeenReason(fallbackReason)
    setHidden(false)
  }
  const show = fallbackReason === 'user-override-offline' && !hidden

  useEffect(() => {
    if (fallbackReason !== 'user-override-offline') return
    const t = setTimeout(() => setHidden(true), durationMs)
    return () => clearTimeout(t)
  }, [fallbackReason, durationMs])

  if (!show) return null

  const name = machineName ? `'${machineName}'` : 'Your remote machine'
  return (
    <div
      role="status"
      className="mb-2 w-full max-w-4xl mx-auto px-3 py-2 rounded-card bg-status-warning-bg border border-status-warning-border text-xs text-status-warning-fg"
    >
      {name} is offline, so this chat runs where the agent usually runs.
    </div>
  )
}
