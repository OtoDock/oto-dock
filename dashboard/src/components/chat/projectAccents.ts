import { Chat } from '../../api/chats'

// Sidebar linkage accents for delegation participants. Role-based, always on
// (operator decision, 2026-07-12 — replaced the per-project hues and the
// >2-chats gating): every delegated worker gets a violet left border, every
// orchestrator (any session that used delegate) an amber one. When a chat has
// been quiet for a day the accent fades toward normal ("finish-fade") instead
// of staying loud forever.

const WORKER_HUE = 'border-l-violet-500 dark:border-l-violet-400'
const WORKER_HUE_FADED = 'border-l-violet-500/40 dark:border-l-violet-400/40'
const ORCHESTRATOR_HUE = 'border-l-amber-500 dark:border-l-amber-400'
const ORCHESTRATOR_HUE_FADED = 'border-l-amber-500/40 dark:border-l-amber-400/40'

const FADE_AFTER_MS = 24 * 3_600_000

/** Border classes for one sidebar row ('' = no accent). The ACTIVE chat is
 * always '' — the sidebar draws its own brand selection bar there, and an
 * accent beside it reads as a double border. Pass `now` in tests. */
export function rowAccentClass(
  chat: Chat, opts: { now?: number; active?: boolean } = {},
): string {
  if (opts.active) return ''
  const isOrchestrator = chat.delegate_role === 'orchestrator'
  const isWorker =
    chat.origin === 'delegated' || chat.delegate_role === 'worker'
  if (!isOrchestrator && !isWorker) return ''
  const updated = Date.parse(chat.updated_at) || 0
  const faded = (opts.now ?? Date.now()) - updated > FADE_AFTER_MS
  if (isOrchestrator) {
    return faded ? ORCHESTRATOR_HUE_FADED : ORCHESTRATOR_HUE
  }
  return faded ? WORKER_HUE_FADED : WORKER_HUE
}
