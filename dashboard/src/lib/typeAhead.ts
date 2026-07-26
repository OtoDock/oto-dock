// Windows-Explorer-style type-ahead matching (Phase G, 1.4.0). Pure logic so
// the workspace overlay's keyboard handler stays thin and this is unit-testable.
//
// Semantics (matching Explorer / VS Code):
// - Printable chars accumulate into a buffer; the buffer resets after
//   RESET_MS of inactivity.
// - The buffer prefix-matches item names case-insensitively.
// - Repeating a single char ("aaa") CYCLES through items starting with that
//   char instead of matching the literal "aaa".

export const TYPE_AHEAD_RESET_MS = 1000

export interface TypeAheadState {
  buffer: string
  lastKeyAt: number
}

export const emptyTypeAhead = (): TypeAheadState => ({ buffer: '', lastKeyAt: 0 })

/** Advance the buffer with a typed char (already filtered to printable). */
export function pushChar(
  state: TypeAheadState, ch: string, now: number,
): TypeAheadState {
  const stale = now - state.lastKeyAt > TYPE_AHEAD_RESET_MS
  const buffer = (stale ? '' : state.buffer) + ch.toLowerCase()
  return { buffer, lastKeyAt: now }
}

/**
 * Find the next matching index among `names` (visible items, in display
 * order). `currentIndex` is the currently-selected index (-1 = none).
 *
 * Single-repeated-char buffers cycle: "a" pressed repeatedly walks through
 * every name starting with "a", wrapping. Multi-char buffers select the
 * FIRST prefix match (stable while typing "te" → "tes" → "test").
 */
export function findMatch(
  names: string[], state: TypeAheadState, currentIndex: number,
): number {
  const { buffer } = state
  if (!buffer) return -1
  const isCycle = buffer.length > 1 && buffer.split('').every((c) => c === buffer[0])
  const needle = isCycle ? buffer[0] : buffer
  const matches = (name: string) => name.toLowerCase().startsWith(needle)
  if (isCycle) {
    // Start looking AFTER the current selection, wrapping.
    for (let step = 1; step <= names.length; step++) {
      const i = (currentIndex + step) % names.length
      if (matches(names[i])) return i
    }
    return -1
  }
  for (let i = 0; i < names.length; i++) {
    if (matches(names[i])) return i
  }
  return -1
}
