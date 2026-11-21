// Whether a frame belongs to the chat on screen: the one answer the dashboard
// socket's drop rule (hooks/useDashboardWs.ts) and every chat stream hook's
// staleness check read. A frame of one chat never changes another chat's view.
import { FRAMES, PER_CHAT_FRAMES, WIRE } from '../../api/wireEvents'

/**
 * Whether a frame that names `frameChat` belongs to the chat on screen.
 *
 * - `shown` undefined: the consumer has no chat of its own and takes every
 *   frame (an app page's socket).
 * - A frame that names a chat belongs only to that chat's view. The new-chat
 *   view (`shown` null) owns no chat until its own first send mints one, so a
 *   frame naming any chat is not its own.
 * - A frame that names no chat passes, unless it carries a chat's `words`:
 *   words that do not say whose they are go nowhere.
 */
export function isForShownChat(frameChat: unknown, shown: string | null | undefined,
                               words = false): boolean {
  if (shown === undefined) return true
  if (typeof frameChat === 'string' && frameChat) return frameChat === shown
  return !words
}

/** The per-chat frames that carry a chat's words: the ones whose block is
 *  stored, the text and the live state. */
export const WORDS_FRAMES: ReadonlySet<string> = new Set(
  Object.entries(FRAMES)
    .filter(([name, f]) => f.perChat && (f.persisted !== null
      || name === WIRE.TEXT || name === WIRE.LIVE_STATE))
    .map(([name]) => name),
)

/** The socket's drop rule for one frame: a per-chat frame reaches the view
 *  only when it belongs to the chat on screen; a global frame always does. */
export function frameIsForShownChat(type: unknown, frameChat: unknown,
                                    shown: string | null | undefined): boolean {
  if (typeof type !== 'string' || !PER_CHAT_FRAMES.has(type)) return true
  return isForShownChat(frameChat, shown, WORDS_FRAMES.has(type))
}
