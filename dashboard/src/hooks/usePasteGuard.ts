import { useMemo, useRef } from 'react'

/** How long a held paste keeps the send keys adding lines when the modifier's
 * release is never seen (focus moved mid-chord, a missed keyup). */
export const PASTE_GUARD_MS = 3000

type Mods = { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }

const held = (e: Mods) => e.shiftKey || e.ctrlKey || e.metaKey

/**
 * A paste made with a modifier down (Ctrl/Cmd+V, Shift+Insert) arms the guard
 * until Shift, Ctrl and Meta are all released: an Enter pressed meanwhile is
 * the person's next line, not a send. Armed by the paste event itself, so any
 * keyboard layout counts (a Greek or Cyrillic layout can report its own
 * letter for Ctrl+V's key); a menu or right-click paste with nothing held does
 * not arm it.
 */
export function usePasteGuard() {
  const modsRef = useRef(false)
  const armedAtRef = useRef<number | null>(null)
  return useMemo(() => ({
    onKeyDown: (e: Mods) => {
      modsRef.current = held(e)
      if (!modsRef.current) armedAtRef.current = null
    },
    onKeyUp: (e: Mods) => {
      modsRef.current = held(e)
      if (!modsRef.current) armedAtRef.current = null
    },
    onPaste: () => {
      if (modsRef.current) armedAtRef.current = Date.now()
    },
    onBlur: () => {
      modsRef.current = false
      armedAtRef.current = null
    },
    held: () => armedAtRef.current !== null && Date.now() - armedAtRef.current < PASTE_GUARD_MS,
  }), [])
}
