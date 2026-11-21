// X-dismiss memory for a chat's notices a person hides: the target-mismatch
// banner (ChatTargetBanner, kind `chattarget`) and the usage-limit card
// under a terminal (TerminalView, kind `limitcard`). Dismissal is sticky per
// SITUATION, not per chat: the store is a set of dismissed keys under
// `oto.<kind>.dismissed.<chatId>` (a resolved target id, a limit's reset),
// so the same situation never re-shows while a NEW one shows once more.
// Dismissal only hides the notice: the sidebar kebab keeps the move action
// reachable, and the chat's history keeps the limit card.
//
// Mirrors lib/pipDismissals.ts: localStorage, per browser — a KNOWN LIMIT: a
// notice dismissed on one device re-shows on another. Per-key cap keeps the
// entry bounded; parse/write failures degrade to "nothing dismissed"
// (re-showing is the safe failure).

export type ChatNoticeKind = 'chattarget' | 'limitcard'

const MAX_KEYS_PER_CHAT = 20

const storeKey = (kind: ChatNoticeKind, chatId: string) => `oto.${kind}.dismissed.${chatId}`

export function loadDismissed(kind: ChatNoticeKind, chatId: string): Set<string> {
  if (!chatId) return new Set()
  try {
    const raw = localStorage.getItem(storeKey(kind, chatId))
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return new Set()
    return new Set(arr.filter((v) => typeof v === 'string'))
  } catch {
    return new Set()
  }
}

export function recordDismissed(kind: ChatNoticeKind, chatId: string, key: string): void {
  if (!chatId || !key) return
  try {
    const keys = [...loadDismissed(kind, chatId)].filter((v) => v !== key)
    keys.push(key)
    // FIFO cap — insertion-ordered, so dropping the OLDEST drops the
    // situation least likely to come back (a chat rarely cycles through
    // many targets, and a limit's reset never returns).
    const capped = keys.slice(-MAX_KEYS_PER_CHAT)
    localStorage.setItem(storeKey(kind, chatId), JSON.stringify(capped))
  } catch {
    /* quota/serialization failure — worst case the notice re-shows */
  }
}
