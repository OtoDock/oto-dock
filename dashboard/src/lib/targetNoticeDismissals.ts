// X-dismiss memory for the chat target-mismatch banner (ChatTargetBanner).
// Dismissal is sticky per MISMATCH, not per chat: the store is a set of
// dismissed resolved_target ids under `oto.chattarget.dismissed.<chatId>`,
// so the same situation never re-shows while a NEW resolved target (the
// agent moved again / back to local) shows once more. Dismissal only hides
// the banner — the sidebar kebab keeps the move action reachable.
//
// Mirrors lib/pipDismissals.ts: localStorage, per browser — a KNOWN LIMIT: a
// banner dismissed on one device re-shows on another. Per-key cap keeps the
// entry bounded; parse/write failures degrade to "nothing dismissed"
// (re-showing is the safe failure).

const KEY_PREFIX = 'oto.chattarget.dismissed.'
const MAX_TARGETS_PER_CHAT = 20

const key = (chatId: string) => `${KEY_PREFIX}${chatId}`

export function loadDismissedTargets(chatId: string): Set<string> {
  if (!chatId) return new Set()
  try {
    const raw = localStorage.getItem(key(chatId))
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return new Set()
    return new Set(arr.filter((v) => typeof v === 'string'))
  } catch {
    return new Set()
  }
}

export function recordDismissedTarget(chatId: string, targetId: string): void {
  if (!chatId || !targetId) return
  try {
    const ids = [...loadDismissedTargets(chatId)].filter((v) => v !== targetId)
    ids.push(targetId)
    // FIFO cap — insertion-ordered, so dropping the OLDEST drops the target
    // least likely to become the resolved one again (a chat rarely cycles
    // through many targets).
    const capped = ids.slice(-MAX_TARGETS_PER_CHAT)
    localStorage.setItem(key(chatId), JSON.stringify(capped))
  } catch {
    /* quota/serialization failure — worst case the banner re-shows */
  }
}
