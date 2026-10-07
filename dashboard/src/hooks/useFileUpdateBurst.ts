import { useEffect } from 'react'
import { onFileUpdate } from '../lib/fileUpdates'

/** How long a burst of `file_updated` frames settles before one refetch. */
export const FILE_UPDATE_BURST_MS = 300

/**
 * Runs `onBurst` once per burst of `file_updated` frames for `agent`: a sync
 * of twenty files arrives as twenty frames, and the tree is walked once for
 * them, not once per frame. Trailing debounce; cleared on unmount.
 */
export function useFileUpdateBurst(agent: string | undefined, onBurst: () => void, ms = FILE_UPDATE_BURST_MS) {
  useEffect(() => {
    if (!agent) return
    let timer: ReturnType<typeof setTimeout> | null = null
    const unsubscribe = onFileUpdate((u) => {
      if (u.agent_slug !== agent) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        timer = null
        onBurst()
      }, ms)
    })
    return () => {
      unsubscribe()
      if (timer) clearTimeout(timer)
    }
  }, [agent, onBurst, ms])
}
