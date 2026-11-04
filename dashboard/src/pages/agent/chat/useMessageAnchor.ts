/**
 * useMessageAnchor — `?message=<id>`: the chat opened at one reply (a
 * report's notice, the Reports page's Open chat). The id is the reply's
 * first stored row. Captured on its arrival and taken off the URL alone
 * (`?tasks=1`, `?q=` and the rest stay, and Back does not anchor again),
 * kept for the chat of that URL. Once that chat's history is on the page,
 * older pages load while the reply is above them (at most `MAX_PAGES`),
 * then the bubble the row starts, or the one a page boundary left it in, is
 * scrolled into view and marked for a moment. Not found, or a live terminal
 * in place of the list: the chat stays where it opened, quietly.
 *
 * Returns whether an anchor for the open chat is still on its way
 * (`pending`: the find bar holds its own first scroll meanwhile) and the
 * bubble it landed on (`landed`, a new object each time).
 */
import { useEffect, useRef, useState } from 'react'
import type { useSearchParams } from 'react-router-dom'

import type { DisplayMessage } from '../../../components/chat/types'
import type { OlderPage } from '../../../hooks/useChatMessages'
import { storedRowId } from '../../../lib/messageBlocks'

/** About a thousand rows back; a reply further up is not looked for. */
export const MAX_PAGES = 20
/** How long the reply stays marked (`[data-anchored]` in index.css). */
export const ANCHOR_MARK_MS = 2000

const ROW_ID = /^\d{1,10}$/

/** The bubble holding row `id`: the one with the largest first stored row at
 *  or below it (a page boundary can start a reply's bubble at a later row
 *  until the older page lands). Null when every stored bubble is newer. */
export function anchorBubble(messages: DisplayMessage[], id: number): DisplayMessage | null {
  let best: DisplayMessage | null = null
  let bestId = -1
  for (const m of messages) {
    const first = storedRowId(m.id)
    if (first !== null && first <= id && first > bestId) {
      best = m
      bestId = first
    }
  }
  return best
}

function markInView(messageId: string): void {
  const el = Array.from(document.querySelectorAll<HTMLElement>('[data-msg-id]'))
    .find((e) => e.dataset.msgId === messageId)
  if (!el) return
  const tall = el.getBoundingClientRect().height > window.innerHeight * 0.8
  el.scrollIntoView({ block: tall ? 'start' : 'center' })
  el.setAttribute('data-anchored', '')
  setTimeout(() => el.removeAttribute('data-anchored'), ANCHOR_MARK_MS)
}

export function useMessageAnchor({
  searchParams, setSearchParams, urlChatId, chatId, messages, loadOlder, hasMoreOlder, loadingOlder, listShown,
}: {
  searchParams: URLSearchParams
  setSearchParams: ReturnType<typeof useSearchParams>[1]
  urlChatId: string | undefined
  chatId: string | null
  messages: DisplayMessage[]
  loadOlder: () => Promise<OlderPage>
  hasMoreOlder: boolean
  loadingOlder: boolean
  listShown: boolean
}) {
  const [anchor, setAnchor] = useState<{ chatId: string; id: number } | null>(null)
  // The anchor this hook is done with (found, or given up on), and the same
  // as state for what the hook reports.
  const handled = useRef<{ chatId: string; id: number } | null>(null)
  const [settled, setSettled] = useState<{ chatId: string; id: number } | null>(null)
  const [landed, setLanded] = useState<{ bubbleId: string } | null>(null)
  // A page that failed once is asked again (a re-run), then given up on.
  const [retry, setRetry] = useState(0)
  const progress = useRef({ pages: 0, retried: false, asking: false })

  // Captured in render, on the parameter's arrival.
  const raw = searchParams.get('message')
  const [seen, setSeen] = useState<string | null>(null)
  if (raw !== seen) {
    setSeen(raw)
    if (raw && urlChatId && ROW_ID.test(raw)) setAnchor({ chatId: urlChatId, id: Number(raw) })
  }
  useEffect(() => {
    if (!raw) return
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('message')
      return next
    }, { replace: true })
  }, [raw, setSearchParams])

  useEffect(() => {
    if (!anchor || handled.current === anchor || chatId !== anchor.chatId) return
    const done = () => {
      handled.current = anchor
      progress.current = { pages: 0, retried: false, asking: false }
      setSettled(anchor)
    }
    if (urlChatId !== anchor.chatId) {
      done()
      return
    }
    const stored = messages.map((m) => storedRowId(m.id)).filter((id): id is number => id !== null)
    if (!stored.length) return  // the history is not on the page yet
    if (!listShown) {
      done()
      return
    }
    const p = progress.current
    if (Math.min(...stored) > anchor.id && hasMoreOlder) {
      if (loadingOlder || p.asking) return  // the list's own page lands first
      if (p.pages >= MAX_PAGES) {
        done()
        return
      }
      p.asking = true
      void loadOlder().then((page) => {
        p.asking = false
        if (page === 'landed') p.pages += 1
        else if (page === 'nothing' && !p.retried) p.retried = true
        else if (page === 'nothing') {
          done()
          return
        }
        // A page that landed changes the list and re-runs this; a 'stale'
        // one (another page in flight, a history held) waits for the list
        // to change; a failed one is asked once more.
        if (page === 'nothing') setRetry((n) => n + 1)
      })
      return
    }
    const target = anchorBubble(messages, anchor.id)
    done()
    if (target) {
      requestAnimationFrame(() => {
        markInView(target.id)
        setLanded({ bubbleId: target.id })
      })
    }
  }, [anchor, chatId, urlChatId, messages, hasMoreOlder, loadingOlder, listShown, loadOlder, retry])

  const pending = anchor !== null && settled !== anchor && chatId === anchor.chatId
  return { pending, landed }
}
