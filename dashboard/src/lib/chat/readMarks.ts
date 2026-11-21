// A chat's read mark (the `chat_read` frame, which the server stores and
// echoes to the chat's audience). The unread dot clears only for a frame
// that went out, and a mark is held until its echo comes back: a mark made
// while no socket is open, or sent into a socket that still read open after
// a phone woke (the health check closes it seconds later), goes out when the
// next socket opens, so a lane opened during a reconnect keeps its mark. A
// mark sent once and never echoed (a viewer the echo does not reach) is sent
// one last time at the next open and dropped. A mark older than five minutes
// at the open is dropped and its dot kept: the server stamps the read when
// the frame arrives, so a late one would mark read a reply that landed in the
// gap. A chat that is live is not held: its turn may end in the gap, and the
// chat page marks it again when the end shows on screen. The held marks ride
// sessionStorage, so the reload after a close during a stream sends them from
// the next page, and leave with a sign-out.

import { OUT } from '../../api/wireEvents'
import { useChatStore } from '../../store/chatStore'
import { isLiveChatPhase } from '../status/chat'

type Sender = (frame: Record<string, unknown>) => boolean

const STORAGE_KEY = 'otodock.readMarks'
const MAX_HELD = 50
const HOLD_MS = 5 * 60_000

// Chat id to [the times its frame went out since it was marked (0 or 1),
// when it was marked].
type Held = [sends: number, markedAt: number]

let held = load()
let sender: Sender | null = null

function load(): Map<string, Held> {
  try {
    const parsed: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '[]')
    if (!Array.isArray(parsed)) return new Map()
    return new Map(parsed.filter((e): e is [string, Held] =>
      Array.isArray(e) && typeof e[0] === 'string' && Array.isArray(e[1])
      && (e[1][0] === 0 || e[1][0] === 1) && typeof e[1][1] === 'number'))
  } catch {
    return new Map()
  }
}

function save(): void {
  try {
    if (held.size === 0) sessionStorage.removeItem(STORAGE_KEY)
    else sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...held]))
  } catch { /* no storage: the marks stay in memory */ }
}

function hold(chatId: string, sends: number): void {
  held.delete(chatId)
  held.set(chatId, [sends, Date.now()])
  for (const oldest of held.keys()) {
    if (held.size <= MAX_HELD) break
    held.delete(oldest)
  }
  save()
}

function sendOne(chatId: string): boolean {
  let out = false
  try {
    out = !!sender && sender({ type: OUT.CHAT_READ, chat_id: chatId })
  } catch { /* the socket went: the mark stays held */ }
  if (out) useChatStore.getState().setUnread(chatId, false)
  return out
}

/** The person saw the chat: send its mark, or hold it for the next socket. */
export function markChatRead(chatId: string): void {
  if (!chatId) return
  if (sendOne(chatId)) {
    hold(chatId, 1)
    return
  }
  if (isLiveChatPhase(useChatStore.getState().byChat[chatId]?.status)) return
  hold(chatId, 0)
}

/** The server's echo of a mark: it is stored, nothing is held for it. */
export function readMarkConfirmed(chatId: string): void {
  if (held.delete(chatId)) save()
}

/** Installed by useDashboardWs when a socket opens: the held marks go out,
 *  each once, until a frame does not; one held too long is dropped. */
export function setReadSender(s: Sender): void {
  sender = s
  const now = Date.now()
  for (const [chatId, [sends, markedAt]] of [...held]) {
    if (now - markedAt > HOLD_MS) {
      held.delete(chatId)
      continue
    }
    if (!sendOne(chatId)) break
    if (sends >= 1) held.delete(chatId)
    else held.set(chatId, [1, markedAt])
  }
  save()
}

/** A closing socket lets go of its own sender only: a page's old socket can
 *  close after the next page's socket installed one. */
export function releaseReadSender(s: Sender): void {
  if (sender === s) sender = null
}

/** Sign-out: the held marks are this person's, never the next one's. */
export function clearReadMarks(): void {
  held = new Map()
  save()
}

/** Tests only: forget everything, or read the stored marks again as a new
 *  page would. */
export function _resetReadMarksForTests(): void {
  held = new Map()
  sender = null
  save()
}

export function _reloadReadMarksForTests(): void {
  held = load()
  sender = null
}
