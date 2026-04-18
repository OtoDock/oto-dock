// X-dismiss memory for interactive PiP artifact windows. A closed (X'd) popup
// must not reappear when the chat is reopened, so `useArtifactWindows` records
// the dismissed event's chat_messages row id (`db_message_id` — the stable
// server key that both the live pty_artifact frame and the replay rows carry)
// and the replay-on-open filter drops those ids.
//
// v1 storage is localStorage, per browser — a KNOWN LIMIT: a popup dismissed on
// one device re-shows on another (mirrors other per-browser dismissals, e.g.
// the otodock connect prompt). Per-key cap keeps the entry bounded; parse/write
// failures degrade to "nothing dismissed" (re-showing is the safe failure).

const KEY_PREFIX = 'oto.pip.dismissed.'
const MAX_IDS_PER_CHAT = 200

const key = (chatId: string) => `${KEY_PREFIX}${chatId}`

export function loadDismissedPips(chatId: string): Set<number> {
  if (!chatId) return new Set()
  try {
    const raw = localStorage.getItem(key(chatId))
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return new Set()
    return new Set(arr.filter((v) => typeof v === 'number'))
  } catch {
    return new Set()
  }
}

export function recordDismissedPip(chatId: string, dbId: number): void {
  if (!chatId || typeof dbId !== 'number') return
  try {
    const ids = [...loadDismissedPips(chatId)].filter((v) => v !== dbId)
    ids.push(dbId)
    // FIFO cap — ids are monotonic row ids, so dropping the OLDEST is always
    // dropping the least-relevant (never replayed anyway once out of the
    // final turn).
    const capped = ids.slice(-MAX_IDS_PER_CHAT)
    localStorage.setItem(key(chatId), JSON.stringify(capped))
  } catch {
    /* quota/serialization failure — worst case the popup re-shows */
  }
}
