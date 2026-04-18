import { useEffect, useState } from 'react'

// Defer rendering a component for `delayMs` after `condition` becomes true.
// Used by InstallProgressBar to avoid a flash on cached-MCP install (which
// completes in <100ms — the banner would render and instantly disappear).
//
// Returns true once condition has been true for at least delayMs continuously.
// Resets immediately when condition flips false.
export function useDelayMount(condition: boolean, delayMs: number): boolean {
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    if (!condition) {
      setMounted(false)
      return
    }
    const t = setTimeout(() => setMounted(true), delayMs)
    return () => clearTimeout(t)
  }, [condition, delayMs])

  return mounted
}
