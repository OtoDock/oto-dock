import { useState, useEffect, useImperativeHandle, useLayoutEffect, useRef, useCallback, useMemo, type Ref } from 'react'
import { SoundIcon } from './SoundIcon'
import { ReportReplyButton } from './ReportReplyButton'

import type { ChatMessagesHandle, DisplayMessage, MessageBlock } from './types'
import { continuesAbove, delegateCardOf, finishedBubble, isAgentReply, nudgeNote, openTurnBubbles, pairBgCommandBlocks, REPORT_MISSING_BADGE, sendAgainKind, supersededUiBlocks, uiTitlesByPath } from '../../lib/messageBlocks'
import { asksPerson, deriveActivityRuns, isNote, noteAsides, turnBusy, WORKING_GRACE_MS, type ActivityRun } from '../../lib/activityGroups'
import { useActivityDisplay } from '../../hooks/useActivityDisplay'
import type { OlderPage } from '../../hooks/useChatMessages'
import BlockRenderer from './ChatBlockRenderer'
import ActivityGroup from './ActivityGroup'
import DelegateCard from './DelegateCard'
import { useDelegateCards } from '../../hooks/useDelegateCards'
import ErrorBoundary from '../ErrorBoundary'
import type { QueuedMessage } from '../../store/types'
import { QUEUE_WAITING } from '../../api/wireEvents'
import { FILE_ICON_PATH, FOLDER_ICON_PATH } from './attachmentIcons'
import { useSearch } from '../../contexts/useSearch'

/** "Working…" under the open turn's newest bubble once it stayed unchanged
 * for the grace with nothing running: every frame replaces the blocks array,
 * so the array the timer went quiet on is the clock. setState only in the
 * timer (a reset on change is the identity compare, not a write). */
function TurnWorking({ blocks }: { blocks: MessageBlock[] }) {
  const [quietOn, setQuietOn] = useState<MessageBlock[] | null>(null)
  useEffect(() => {
    const timer = setTimeout(() => setQuietOn(blocks), WORKING_GRACE_MS)
    return () => clearTimeout(timer)
  }, [blocks])
  if (quietOn !== blocks) return null
  return (
    <div role="status" data-testid="turn-working" className="flex items-center gap-1.5 py-1 text-xs text-tertiary">
      <span className="w-1.5 h-1.5 rounded-full bg-status-info-solid animate-pulse" />
      <span className="animate-pulse">Working…</span>
    </div>
  )
}

/** The chips of a queued or held message, from the meta the frame carried:
 * the saved photos through the agent files URL (the same URL the history
 * bubbles use) and one badge per file. */
function QueuedAttachments({ item, agentName }: { item: QueuedMessage; agentName?: string }) {
  const images = (item.images ?? []).filter(i => i.path)
  const files = item.files ?? []
  if (!images.length && !files.length) return null
  return (
    <div className="flex flex-col gap-1 mb-1" data-testid="queued-attachments">
      {images.length > 0 && (
        <div className="flex gap-2 flex-wrap">
          {images.map((img, i) => (
            agentName ? (
              <img
                key={`qi-${i}`}
                src={`/v1/agents/${agentName}/files/${encodeURI(img.path!)}`}
                alt={img.name}
                className="w-14 h-14 rounded-card object-cover border border-subtle"
              />
            ) : (
              <span key={`qi-${i}`} className="text-xs text-secondary">{img.name}</span>
            )
          ))}
        </div>
      )}
      {files.map((f, i) => (
        <div key={`qf-${i}`} className="flex items-center gap-1.5 text-xs text-secondary bg-surface-card rounded-control px-2.5 py-1.5 w-fit">
          <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d={f.folder ? FOLDER_ICON_PATH : FILE_ICON_PATH} />
          </svg>
          <span className="truncate max-w-[200px]">{f.name}</span>
        </div>
      ))}
    </div>
  )
}

interface Props {
  messages: DisplayMessage[]
  agentName?: string
  // The page agent's CURRENT display name (from AgentChat's already-loaded
  // agent record) — useAgents() is undefined during the cold-load round-trip,
  // so resolving it here from the hook would paint the slug-cased name first.
  agentDisplayName?: string
  agentColor?: string
  chatId?: string
  onPermissionRespond: (requestId: string, approved: boolean) => void
  onPlanReviewResponse?: (requestId: string, action: string) => void
  onImplementPlan?: (planPath: string, mode: string) => void
  onImplementPlanCodex?: (mode: string) => void
  onQuestionAnswer?: (response: string) => void
  onQuestionAnswerStructured?: (requestId: string, answers: Record<string, { answers: string[] }>) => void
  onSendMessage?: (text: string) => void
  onPlanFetched?: (filename: string, content: string) => void
  onArtifactInteraction?: (token: string, title: string, payload: unknown) => Promise<{ status: string; reason?: string }>
  streaming?: boolean
  queuedMessages?: QueuedMessage[]
  /** Steers the engine accepted but has not reached yet (held until the
   * next block boundary): shown, never editable — they already sit in the
   * running turn. */
  pendingSteers?: QueuedMessage[]
  onCancelQueued?: (index: number) => void
  /** Whether this person may cancel a chip: their own, or any for an admin
   *  (a shared chat shows a teammate's chips too). Every chip without it. */
  mayCancelQueued?: (item: QueuedMessage) => boolean
  /** The turn-ended card's button: the page sends what the card asks for
   *  (Resume, or the turn's messages again), named by the message the card
   *  is in; the list reads which from the transcript. */
  onSendAgain?: (messageId: string) => void
  // Lazy chat-history scroll-back (loads older turns when the top comes into
  // view). Resolves how the page settled: `nothing` rests the fill pass,
  // `stale` starts it again with the next rows.
  onLoadOlder?: () => void | Promise<OlderPage>
  hasMoreOlder?: boolean
  loadingOlder?: boolean
  /** A live interactive terminal owns this chat's dialogs — question and
   * plan cards render read-only (see BlockRenderer). */
  dialogsInTerminal?: boolean
  /** The open delegate cards, held by the page (`useDelegateCards`); without
   * them the list keeps its own. */
  openCards?: ReadonlySet<string>
  onToggleCard?: (key: string) => void
  ref?: Ref<ChatMessagesHandle>
}

// --- Scroll tuning (items: autoscroll hysteresis + arrow settle) ---

// Accumulated upward gesture travel (wheel deltas / finger px) that breaks away
// from the pinned-to-bottom autoscroll during streaming. One mouse-wheel tick
// (~100px) unsticks immediately; a trackpad/touch graze under this stays pinned.
const UNSTICK_INTENT_PX = 60
// Settled |scrollTop| below which autoscroll re-arms during streaming. Wider
// than the old 10px absolute-bottom rule so a momentum glide back down actually
// re-sticks, still small enough that deliberate reading positions stay free.
const RESTICK_STREAMING_PX = 48
// Idle near-bottom threshold (unchanged historical behavior) — doubles as the
// scroll-to-bottom button visibility threshold.
const NEAR_BOTTOM_IDLE_PX = 100
const SCROLL_BTN_SHOW_PX = 100
// Trailing-timer settle fallback for browsers without the `scrollend` event.
const SCROLL_SETTLE_MS = 120
// A settled position only re-sticks when a user gesture happened recently —
// content churn during streaming (reseeds, image loads) can transiently clamp
// scrollTop near 0 and must never yank a reading viewport back to the bottom.
const GESTURE_RESTICK_WINDOW_MS = 3000
// How far above the view the top of the loaded history asks for the next
// older page: far enough that it usually lands before the top is reached.
const OLDER_PREFETCH_PX = 600
// Pages asked for in a row without a gesture while the loaded history does
// not reach that far (a page of steps alone folds into one short bubble):
// bounds the fill on a chat whose pages are all small, or never arrive.
const FILL_MAX_PAGES = 6

// --- Helpers ---

// Format timestamp for message headers
function formatMessageTime(iso: string): string {
  const d = new Date(iso)
  const day = d.getDate()
  const mon = d.toLocaleString('en', { month: 'short' })
  const h = d.getHours().toString().padStart(2, '0')
  const m = d.getMinutes().toString().padStart(2, '0')
  return `${day} ${mon}, ${h}:${m}`
}

// Extract plain text from message blocks for the copy button and read
// aloud: a note drawn as an aside is not the answer (`asides`), one drawn as
// text is.
function extractPlainText(blocks: MessageBlock[], asides?: ReadonlySet<number>): string {
  return blocks
    .filter((b, i): b is { type: 'text'; content: string } => b.type === 'text' && !(isNote(b) && asides?.has(i)))
    .map(b => (isNote(b) ? b.content.trim() : b.content))
    .join('\n\n')
}

// --- Copy button component ---

function CopyMessageButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = useCallback(() => {
    if (!text.trim()) return
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }, [text])

  return (
    <button
      onClick={handleCopy}
      title="Copy message"
      className="copy-msg-btn p-1 rounded-sm hover:bg-surface-hover text-tertiary hover:text-secondary"
    >
      {copied ? (
        <svg className="w-3.5 h-3.5 text-status-success-fg" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
        </svg>
      ) : (
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
        </svg>
      )}
    </button>
  )
}

// --- Scroll-to-bottom button ---

function ScrollToBottomButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="scroll-to-bottom-btn flex items-center justify-center w-9 h-9 rounded-full
                 bg-surface-overlay shadow-elevation-3 text-link hover:text-link-hover
                 transition-all duration-200"
    >
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7m0 0l-7-7m7 7V3" />
      </svg>
    </button>
  )
}


// --- Main component ---

export default function ChatMessages({
  messages,
  agentName,
  agentDisplayName,
  agentColor,
  chatId,
  onPermissionRespond,
  onPlanReviewResponse,
  onImplementPlan,
  onImplementPlanCodex,
  onQuestionAnswer,
  onQuestionAnswerStructured,
  dialogsInTerminal,
  onSendMessage,
  onPlanFetched,
  onArtifactInteraction,
  streaming,
  queuedMessages,
  pendingSteers,
  onCancelQueued,
  mayCancelQueued,
  onSendAgain,
  onLoadOlder,
  hasMoreOlder,
  loadingOlder,
  openCards: openCardsProp,
  onToggleCard: onToggleCardProp,
  ref,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const olderSentinelRef = useRef<HTMLDivElement>(null)
  const userScrolledUp = useRef(false)
  // The prepend anchor of a scroll-back page: armed when the page is
  // requested, refreshed by the gestures while it loads, consumed when it
  // lands (the layout effect below the observer).
  const prependAnchorRef = useRef<number | null>(null)
  // Where the loaded history's top was when a page was asked for, measured
  // from the content's bottom: what lands above it fades in.
  const oldEdgeRef = useRef<number | null>(null)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const { query: searchQuery, currentMatch } = useSearch()
  const streamingRef = useRef(streaming)
  useLayoutEffect(() => { streamingRef.current = streaming })

  // A re-displayed ui artifact renders only at its newest position — older
  // instances of the same workspace file collapse to a chip (see UiArtifact) —
  // and an html-less re-display (no title on the wire) inherits the latest
  // title known for its path.
  const supersededUi = useMemo(() => supersededUiBlocks(messages), [messages])
  const uiTitles = useMemo(() => uiTitlesByPath(messages), [messages])

  // Delegate cards: the page's open set when it holds one, else the list's.
  const ownCards = useDelegateCards(chatId)
  const openCards = openCardsProp ?? ownCards.openCards
  const onToggleCard = onToggleCardProp ?? ownCards.toggleCard
  // Two cards with one key (two rounds of a task with the same text) get an
  // occurrence index, so one toggle never opens both. Counted from the
  // newest message: loading older ones prepends and never shifts an index.
  const cardKeys = useMemo(() => {
    const seen = new Map<string, number>()
    const keys = new Map<string, string>()
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      const key = m.delegateCard?.key
      if (!key) continue
      const n = seen.get(key) ?? 0
      seen.set(key, n + 1)
      keys.set(m.id, n ? `${key}#${n}` : key)
    }
    return keys
  }, [messages])

  // Compact activity view: runs of tool/thinking/subagent/bgcommand blocks
  // collapse into one chip each (ActivityGroup). Derived per message here,
  // alongside the other per-list memos; in Detailed mode the map stays empty
  // and the block loop below renders exactly the historical flat layout.
  const activityDisplay = useActivityDisplay()
  // The bubbles of the turn still open: their chips count a waited sub-agent
  // or background command as running, and the newest says "Working…".
  const openBubbles = useMemo(() => openTurnBubbles(messages, !!streaming), [messages, streaming])
  // The notes each bubble draws as asides (the rest are real messages,
  // drawn as text): a meeting bubble's, a finished bubble's last words.
  const asidesByMsg = useMemo(() => {
    const map = new Map<string, Set<number>>()
    for (const msg of messages) {
      map.set(msg.id, noteAsides(msg.blocks, { meeting: msg.badge === 'meeting', finished: finishedBubble(msg) }))
    }
    return map
  }, [messages])
  const activityRunsByMsg = useMemo(() => {
    const map = new Map<string, { byStart: Map<number, ActivityRun>; grouped: Set<number> }>()
    if (activityDisplay !== 'compact') return map
    for (const msg of messages) {
      if (msg.role === 'user' && !msg.agentSlug) continue
      const { hiddenToolIdx } = pairBgCommandBlocks(msg.blocks)
      const runs = deriveActivityRuns(msg.blocks, hiddenToolIdx, {
        turnOpen: openBubbles.has(msg.id), asides: asidesByMsg.get(msg.id) ?? new Set(),
      })
      if (runs.length === 0) continue
      const byStart = new Map<number, ActivityRun>()
      const grouped = new Set<number>()
      for (const r of runs) {
        byStart.set(r.start, r)
        for (let i = r.start; i <= r.end; i++) grouped.add(i)
      }
      map.set(msg.id, { byStart, grouped })
    }
    return map
  }, [messages, activityDisplay, openBubbles, asidesByMsg])

  // Auto-scroll during streaming: column-reverse handles initial load positioning,
  // but during streaming we need to keep the user at the bottom as new content arrives.
  // In flex-col-reverse, scrollTop=0 = bottom (newest messages).
  useEffect(() => {
    if (streaming && !userScrolledUp.current && !searchQuery && containerRef.current) {
      containerRef.current.scrollTop = 0
    }
  }, [messages, streaming, searchQuery])

  // Scroll to current search match
  useEffect(() => {
    if (!searchQuery) return
    const timer = setTimeout(() => {
      const el = containerRef.current?.querySelector(`[data-search-index="${currentMatch}"]`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }
    }, 50)
    return () => clearTimeout(timer)
  }, [searchQuery, currentMatch])

  // Detect user scroll intent via wheel/touch events (not onScroll, which fires
  // from content growth during streaming in flex-col-reverse and causes false
  // resets). Unsticking during streaming is INTENT-based with hysteresis: upward
  // gesture travel accumulates until UNSTICK_INTENT_PX breaks the pin (position
  // alone can't decide — the pin keeps snapping scrollTop back to 0, which is
  // what made slow trackpad/touch gestures lose the fight). A downward gesture
  // resets the accumulator; re-sticking happens on scroll settle below.
  const upIntentRef = useRef(0)
  const lastGestureAtRef = useRef(0)
  const lastTouchYRef = useRef<number | null>(null)
  // Pages the fill pass asked for since the last gesture or new history, and
  // its step (defined with the scroll-back below; a gesture runs one).
  const fillPagesRef = useRef(0)
  const fillStepRef = useRef<() => void>(() => {})
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    // Shared post-gesture position check: button visibility + fast-path resets.
    const checkPosition = () => {
      requestAnimationFrame(() => {
        if (!containerRef.current) return
        const st = Math.abs(containerRef.current.scrollTop)
        setShowScrollBtn(st >= SCROLL_BTN_SHOW_PX)
        if (!streamingRef.current) {
          if (st < NEAR_BOTTOM_IDLE_PX) {
            userScrolledUp.current = false
            setShowScrollBtn(false)
          } else {
            userScrolledUp.current = true
          }
        }
      })
    }
    const accumulateUpIntent = (upDelta: number) => {
      if (upDelta > 0) {
        upIntentRef.current += upDelta
        if (streamingRef.current && upIntentRef.current >= UNSTICK_INTENT_PX) {
          userScrolledUp.current = true
        }
      } else if (upDelta < 0) {
        upIntentRef.current = 0
      }
    }
    const refreshPrependAnchor = () => {
      // The user kept scrolling while an older page loads — restore to where
      // they ARE, not where they were when the sentinel fired.
      if (prependAnchorRef.current !== null && containerRef.current) {
        prependAnchorRef.current = containerRef.current.scrollTop
      }
    }
    const handleWheel = (e: WheelEvent) => {
      lastGestureAtRef.current = Date.now()
      fillPagesRef.current = 0
      accumulateUpIntent(-e.deltaY)
      refreshPrependAnchor()
      checkPosition()
      fillStepRef.current()
    }
    const handleTouchStart = (e: TouchEvent) => {
      if (e.touches.length === 1) lastTouchYRef.current = e.touches[0].clientY
    }
    const handleTouchMove = (e: TouchEvent) => {
      lastGestureAtRef.current = Date.now()
      fillPagesRef.current = 0
      if (e.touches.length === 1) {
        const y = e.touches[0].clientY
        if (lastTouchYRef.current != null) {
          // Finger moving down reveals older content = upward scroll intent.
          accumulateUpIntent(y - lastTouchYRef.current)
        }
        lastTouchYRef.current = y
      }
      refreshPrependAnchor()
      checkPosition()
      fillStepRef.current()
    }
    const handleTouchEnd = () => { lastTouchYRef.current = null }
    el.addEventListener('wheel', handleWheel, { passive: true })
    el.addEventListener('touchstart', handleTouchStart, { passive: true })
    el.addEventListener('touchmove', handleTouchMove, { passive: true })
    el.addEventListener('touchend', handleTouchEnd, { passive: true })
    return () => {
      el.removeEventListener('wheel', handleWheel)
      el.removeEventListener('touchstart', handleTouchStart)
      el.removeEventListener('touchmove', handleTouchMove)
      el.removeEventListener('touchend', handleTouchEnd)
    }
  }, [])

  // Scroll-settle detection (`scrollend` where supported, trailing timer
  // elsewhere): the momentum tail of a fling lands AFTER the last wheel/touch
  // event, so the one-shot checks above read a stale position — this is what
  // left the scroll-to-bottom arrow visible at the bottom, and what made
  // re-sticking after a glide back down unreliable. Position reads here are
  // safe during streaming (they never SET userScrolledUp); re-sticking is
  // gesture-gated so content churn can't yank a reading viewport.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onSettle = () => {
      const c = containerRef.current
      if (!c) return
      const st = Math.abs(c.scrollTop)
      setShowScrollBtn(st >= SCROLL_BTN_SHOW_PX)
      const gestureRecent = Date.now() - lastGestureAtRef.current < GESTURE_RESTICK_WINDOW_MS
      if (streamingRef.current) {
        if (gestureRecent && st < RESTICK_STREAMING_PX) {
          userScrolledUp.current = false
          upIntentRef.current = 0
        }
      } else if (st < NEAR_BOTTOM_IDLE_PX) {
        userScrolledUp.current = false
        upIntentRef.current = 0
        setShowScrollBtn(false)
      }
    }
    const supportsScrollEnd = 'onscrollend' in window
    let settleTimer: ReturnType<typeof setTimeout> | null = null
    const onScrollFallback = () => {
      if (settleTimer) clearTimeout(settleTimer)
      settleTimer = setTimeout(onSettle, SCROLL_SETTLE_MS)
    }
    if (supportsScrollEnd) {
      el.addEventListener('scrollend', onSettle)
    } else {
      el.addEventListener('scroll', onScrollFallback, { passive: true })
    }
    // Content resize (image/tool-card load, list reseed, viewport change) can
    // put the view at the bottom without any scroll event — recompute the
    // arrow from the settled position.
    const ro = new ResizeObserver(() => onSettle())
    if (contentRef.current) ro.observe(contentRef.current)
    return () => {
      if (supportsScrollEnd) el.removeEventListener('scrollend', onSettle)
      else el.removeEventListener('scroll', onScrollFallback)
      if (settleTimer) clearTimeout(settleTimer)
      ro.disconnect()
    }
  }, [])

  // onScroll: only used for scroll button visibility when NOT streaming.
  // During streaming, the wheel/touch handlers + settle detection above manage
  // everything (content growth fires spurious scroll events here).
  const handleScroll = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    if (streamingRef.current) return  // Skip — gesture + settle handlers own streaming
    const atBottom = Math.abs(el.scrollTop) < NEAR_BOTTOM_IDLE_PX
    if (atBottom) {
      userScrolledUp.current = false
      setShowScrollBtn(false)
    } else {
      setShowScrollBtn(true)
    }
  }, [])

  const scrollToBottom = useCallback(() => {
    userScrolledUp.current = false
    upIntentRef.current = 0
    // An older page still loading must not restore the reading position
    // over the bottom.
    prependAnchorRef.current = null
    oldEdgeRef.current = null
    setShowScrollBtn(false)
    if (containerRef.current) {
      // flex-col-reverse: scrollTop=0 = bottom (newest messages)
      containerRef.current.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }, [])

  // The page's own send: back to the bottom even while scrolled up to read.
  useImperativeHandle(ref, () => ({
    pinToBottom: scrollToBottom,
  }), [scrollToBottom])

  // Lazy scroll-back: when the top sentinel comes into view, request the next older
  // page. flex-col-reverse is bottom-anchored, so prepending older rows SHOULD
  // preserve the viewport natively — but not every engine scroll-anchors
  // column-reverse (Firefox, some Android WebViews: the page visibly jumped
  // when a page landed — operator report 2026-07-12), so the position is
  // pinned explicitly: capture the bottom-referenced scrollTop when the page
  // is requested (refreshed on user gestures while it loads), then restore it
  // pre-paint the moment the oldest row changes. On engines that anchored
  // correctly the restore writes the value scrollTop already has — a no-op.
  // Refs keep the observer stable (no re-attach churn); the guard reads the
  // latest hasMore/loading.
  const canLoadOlderRef = useRef(false)
  const onLoadOlderRef = useRef(onLoadOlder)
  useLayoutEffect(() => {
    // An empty list has nothing to page from: a chat just switched to, its
    // history not painted yet (the left chat's cursor is still set).
    canLoadOlderRef.current = !!hasMoreOlder && !loadingOlder && messages.length > 0
    onLoadOlderRef.current = onLoadOlder
  })
  // A page asked for and not settled yet (one at a time); the list as it
  // was committed (a stale request compares it); a fill owed to the next
  // rows that come.
  const requestingRef = useRef(false)
  const committedRef = useRef(messages)
  useLayoutEffect(() => { committedRef.current = messages })
  const refillRef = useRef(false)
  const oldestId = messages[0]?.id
  const prevOldestIdRef = useRef(oldestId)
  const requestOlder = useCallback(() => {
    const root = containerRef.current
    const content = contentRef.current
    if (!root || !content || requestingRef.current || !onLoadOlderRef.current) return
    requestingRef.current = true
    prependAnchorRef.current = root.scrollTop
    const first = content.querySelector<HTMLElement>('[data-msg-id]')
    oldEdgeRef.current = first ? content.getBoundingClientRect().bottom - first.getBoundingClientRect().top : null
    const asked = committedRef.current
    void Promise.resolve(onLoadOlderRef.current()).then((page) => {
      requestingRef.current = false
      if (page === 'landed' || page === undefined) return
      // Nothing landed: the anchor is void.
      prependAnchorRef.current = null
      oldEdgeRef.current = null
      if (page === 'nothing') {
        // An empty page or a failure: the fill rests until the next gesture.
        fillPagesRef.current = FILL_MAX_PAGES
        return
      }
      // Stale (the rows were reset meanwhile, or wait for their live
      // state): the fill starts again with the next rows, now when they
      // are on screen already, else when they come.
      fillPagesRef.current = 0
      if (committedRef.current !== asked) fillStepRef.current()
      else refillRef.current = true
    })
  }, [])
  useEffect(() => {
    const root = containerRef.current
    const target = olderSentinelRef.current
    if (!root || !target) return
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting) && canLoadOlderRef.current) {
          fillPagesRef.current = 0
          requestOlder()
        }
      },
      { root, rootMargin: `${OLDER_PREFETCH_PX}px 0px 0px 0px` },
    )
    io.observe(target)
    return () => io.disconnect()
  }, [requestOlder])

  // The prepend anchor: armed at request time, consumed exactly once when the
  // oldest loaded row changes (the page landed). useLayoutEffect runs after
  // the DOM mutation but before paint, so a mis-anchored engine never shows
  // the jumped frame. The bubbles that landed wholly above the old top fade
  // in (one the page merged with straddles it: it was on screen).
  useLayoutEffect(() => {
    const prev = prevOldestIdRef.current
    prevOldestIdRef.current = oldestId
    if (oldestId === prev) return
    if (prependAnchorRef.current === null) {
      fillPagesRef.current = 0  // a new window of history, not a page
      return
    }
    const el = containerRef.current
    if (el) el.scrollTop = prependAnchorRef.current
    prependAnchorRef.current = null
    const edge = oldEdgeRef.current
    oldEdgeRef.current = null
    const content = contentRef.current
    if (edge === null || !content) return
    const limit = content.getBoundingClientRect().bottom - edge
    for (const r of content.querySelectorAll<HTMLElement>('[data-msg-id]')) {
      const box = r.getBoundingClientRect()
      if (box.top >= limit - 1) break
      if (box.bottom > limit + 1) continue
      r.classList.add('chat-older-in')
      r.addEventListener('animationend', () => r.classList.remove('chat-older-in'), { once: true })
    }
  }, [oldestId])

  // A page that landed rows but changed no bubble (rows the list hides, the
  // tail of the oldest bubble's turn already drawn) never moves the oldest
  // id: its anchor is released when the loading ends, after the effect
  // above had its chance, so a later history window is not read as a page.
  const prevLoadingRef = useRef(loadingOlder)
  useLayoutEffect(() => {
    const was = prevLoadingRef.current
    prevLoadingRef.current = loadingOlder
    if (!was || loadingOlder) return
    prependAnchorRef.current = null
    oldEdgeRef.current = null
  }, [loadingOlder])

  // The fill pass: the observer fires on a change of intersection only, so a
  // page that leaves the top of the history within reach (one that does not
  // fill the view, the first one included: it lands before `hasMoreOlder`
  // is known) would end the scroll-back. Each time more history is known and
  // nothing loads, and at each gesture, the sentinel's place is measured and
  // the next page asked for while it is in reach, FILL_MAX_PAGES in a row at
  // most.
  const fillStep = useCallback(() => {
    const root = containerRef.current
    const target = olderSentinelRef.current
    if (!root || !target || !canLoadOlderRef.current || fillPagesRef.current >= FILL_MAX_PAGES) return
    if (target.getBoundingClientRect().bottom < root.getBoundingClientRect().top - OLDER_PREFETCH_PX) return
    fillPagesRef.current += 1
    requestOlder()
  }, [requestOlder])
  useLayoutEffect(() => { fillStepRef.current = fillStep })
  useEffect(() => {
    fillStep()
  }, [hasMoreOlder, loadingOlder, oldestId, fillStep])
  useEffect(() => {
    if (!refillRef.current) return
    refillRef.current = false
    fillStep()
  }, [messages, fillStep])

  return (
    <div className="flex-1 relative min-h-0">
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="h-full overflow-y-auto flex flex-col-reverse px-4"
    >
      {/* Single child wrapper — column-reverse anchors this to the visual bottom.
          scrollTop=0 = newest messages visible. No scrollIntoView needed. */}
      <div ref={contentRef} className="max-w-4xl mx-auto w-full space-y-6 pt-14 pb-4">
        {/* Lazy scroll-back trigger (visual top = oldest loaded). The observer
            fires onLoadOlder as it nears the viewport; older rows prepend with no
            jump (column-reverse is bottom-anchored). */}
        {onLoadOlder && <div ref={olderSentinelRef} className="h-px" aria-hidden />}
        {/* Its height held while more history exists: nothing moves when the
            words come and go. */}
        {(hasMoreOlder || loadingOlder) && (
          <div className="h-8 flex items-center justify-center text-xs text-tertiary" data-testid="older-loading">
            {loadingOlder && <span role="status">Loading older messages…</span>}
          </div>
        )}
        {messages.map((msg, msgIdx) => {
          const hasText = msg.blocks.some(b => b.type === 'text')

          if (msg.role === 'user' && !msg.agentSlug) {
            return (
              // Per-message crash isolation: one corrupt block must not
              // unmount the whole app (blank page) — see ErrorBoundary.
              <ErrorBoundary key={msg.id} variant="inline" label="This message">
              <div className="flex flex-col items-end group/msg" data-msg-id={msg.id}>
                {/* Timestamp above bubble */}
                <span className="text-2xs text-tertiary mb-1 mr-1">
                  {formatMessageTime(msg.createdAt)}
                </span>
                {/* Bubble */}
                <div
                  className="max-w-[85%] rounded-card px-4 py-3 bg-action text-action-fg overflow-hidden"
                  style={{ overflowWrap: 'break-word', wordBreak: 'break-word' }}
                >
                  <div className="space-y-3">
                    {msg.blocks.map((block, i) => {
                      if (block.type === 'metadata') return null
                      return (
                        <BlockRenderer
                          key={i}
                          block={block}
                          blockId={`${msg.id}-b${i}`}
                          blockOrder={msgIdx * 1000 + i}
                          isUserMessage
                          chatId={chatId}
                          agentName={agentName}
                          onPermissionRespond={onPermissionRespond}
                          onPlanReviewResponse={onPlanReviewResponse}
                          onImplementPlan={onImplementPlan}
                          onImplementPlanCodex={onImplementPlanCodex}
                          onQuestionAnswer={onQuestionAnswer}
                          onQuestionAnswerStructured={onQuestionAnswerStructured}
                          dialogsInTerminal={dialogsInTerminal}
                          onSendMessage={onSendMessage}
                          onSendAgain={onSendAgain && !streaming ? () => onSendAgain(msg.id) : undefined}
                          sendAgainKind={sendAgainKind(messages, msg)}
                          onPlanFetched={onPlanFetched}
                          onArtifactInteraction={onArtifactInteraction}
                        />
                      )
                    })}
                  </div>
                </div>
                {/* Copy below bubble, right-aligned */}
                {hasText && (
                  <div className="mt-1 mr-1 transition-opacity duration-150 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover/msg:opacity-100 focus-within:opacity-100">
                    <CopyMessageButton text={extractPlainText(msg.blocks)} />
                  </div>
                )}
              </div>
              </ErrorBoundary>
            )
          }

          // Assistant message — full width, no bubble
          const meta = msg.blocks.find((b): b is Extract<MessageBlock, {type:'metadata'}> => b.type === 'metadata')
          const hasContent = msg.blocks.some(b => b.type !== 'metadata')
          const asides = asidesByMsg.get(msg.id) ?? new Set<number>()
          // Fold each background command's Bash tool block into its bgcommand
          // pill (one expandable card per command instead of two stacked ones).
          const { hiddenToolIdx, bgPairs } = pairBgCommandBlocks(msg.blocks)
          const msgSlug = msg.agentSlug || agentName
          const displayName = msg.agentDisplayName
            || (msgSlug === agentName ? agentDisplayName : undefined)
            || (msgSlug ? msgSlug.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : 'Assistant')
          const initial = (msgSlug || 'A').charAt(0).toUpperCase()
          const avatarBg = msg.agentColor || agentColor || ''

          // Subtle separator between consecutive assistant messages; a reply
          // that goes on from the bubble above joins it instead (no header,
          // no separator), still its own bubble with its own footer.
          const prevMsg = msgIdx > 0 ? messages[msgIdx - 1] : undefined
          const continues = !!msg.continuation && continuesAbove(prevMsg, msg)
          const showSeparator = !continues && prevMsg && (prevMsg.role === 'assistant' || prevMsg.agentSlug)

          // Header: avatar + agent name + badge + timestamp
          const header = (
            <>
              <span
                className={`flex items-center justify-center w-6 h-6 rounded-full text-on-solid text-xs font-semibold shrink-0 ${avatarBg ? '' : 'bg-accent-agent-fill'}`}
                style={avatarBg ? { backgroundColor: avatarBg } : undefined}
              >
                {initial}
              </span>
              <span className="text-sm font-semibold text-primary">{displayName}</span>
              {msg.badge && (
                <span className={`px-1.5 py-0.5 rounded-sm text-3xs font-medium ${
                  msg.badge === REPORT_MISSING_BADGE
                    ? 'bg-status-warning-bg text-status-warning-fg'
                    : 'bg-accent-agent-bg text-accent-agent-fg'
                }`}>
                  {msg.badge}
                </span>
              )}
              <span className="text-2xs text-tertiary">
                {formatMessageTime(msg.createdAt)}
              </span>
            </>
          )
          const card = delegateCardOf(msg)
          const cardKey = card ? (cardKeys.get(msg.id) ?? card.key) : ''

          const body = (
            <>
              {/* Content blocks */}
              <div className="space-y-3">
                {msg.blocks.map((block, i) => {
                  if (block.type === 'metadata') return null
                  // Compact activity view: a run's start index renders the
                  // whole span as one chip; the rest of the span is skipped
                  // (composes with the hiddenToolIdx skip below — hidden
                  // indices inside dropped runs still fall through to it).
                  const runInfo = activityRunsByMsg.get(msg.id)
                  const run = runInfo?.byStart.get(i)
                  if (run) {
                    return (
                      <ActivityGroup
                        key={`${msg.id}:${run.start}`}
                        run={run}
                        blocks={msg.blocks}
                        msgId={msg.id}
                        msgIdx={msgIdx}
                        hiddenToolIdx={hiddenToolIdx}
                        bgPairs={bgPairs}
                        chatId={chatId}
                        agentName={msgSlug || agentName}
                        onPermissionRespond={onPermissionRespond}
                      />
                    )
                  }
                  if (runInfo?.grouped.has(i)) return null
                  if (hiddenToolIdx.has(i)) return null
                  // What woke a background review: one faint line, the
                  // reply's time on hover.
                  const reviewNote = nudgeNote(block)
                  if (reviewNote) {
                    return (
                      <p key={i} data-testid="review-note" title={formatMessageTime(msg.createdAt)}
                         className="my-0.5 text-xs leading-relaxed text-tertiary">
                        {reviewNote}
                      </p>
                    )
                  }
                  return (
                    <BlockRenderer
                      key={i}
                      block={block}
                      blockId={`${msg.id}-b${i}`}
                      blockOrder={msgIdx * 1000 + i}
                      isUserMessage={false}
                      chatId={chatId}
                      agentName={msgSlug || agentName}
                      onPermissionRespond={onPermissionRespond}
                      onPlanReviewResponse={onPlanReviewResponse}
                      onImplementPlan={onImplementPlan}
                      onImplementPlanCodex={onImplementPlanCodex}
                      onQuestionAnswer={onQuestionAnswer}
                      onQuestionAnswerStructured={onQuestionAnswerStructured}
                      dialogsInTerminal={dialogsInTerminal}
                      onSendMessage={onSendMessage}
                      onSendAgain={onSendAgain && !streaming ? () => onSendAgain(msg.id) : undefined}
                      sendAgainKind={sendAgainKind(messages, msg)}
                      onPlanFetched={onPlanFetched}
                      onArtifactInteraction={onArtifactInteraction}
                      bgPair={bgPairs.get(i)}
                      noteAside={asides.has(i)}
                      uiSuperseded={supersededUi.has(`${msgIdx}:${i}`)}
                      uiTitle={block.type === 'ui' && !block.title && block.path ? uiTitles.get(block.path) : undefined}
                    />
                  )
                })}
              </div>

              {/* Typing indicator — shows when assistant has no content yet */}
              {!hasContent && (
                <div className="flex items-center gap-1 py-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-status-info-solid animate-bounce" style={{ animationDelay: '0ms' }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-status-info-solid animate-bounce" style={{ animationDelay: '150ms' }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-status-info-solid animate-bounce" style={{ animationDelay: '300ms' }} />
                </div>
              )}
              {/* The open turn's newest bubble, quiet with nothing running and
                  nothing asked of the person: the turn still works. */}
              {hasContent && msgIdx === messages.length - 1 && openBubbles.has(msg.id)
                && msg.badge !== 'meeting' && !dialogsInTerminal
                && !turnBusy(msg.blocks) && !asksPerson(msg.blocks) && (
                <TurnWorking blocks={msg.blocks} />
              )}

              {/* Footer: copy + badges, all left-aligned */}
              {(hasText || meta) && (
                <div className="flex items-center gap-2 mt-2 text-2xs">
                  {meta && (
                    <>
                      {meta.durationMs != null && (
                        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-surface-muted text-secondary">
                          <svg className="w-2.5 h-2.5 text-tertiary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          {(meta.durationMs / 1000).toFixed(1)}s
                        </span>
                      )}
                      {/* A subscription / local-model turn (costBilled false) keeps
                          its duration but hides the cost — an activity estimate,
                          not money. Missing flag (older rows) = shown. */}
                      {meta.costUsd != null && meta.costBilled !== false && (
                        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-surface-muted text-secondary">
                          <svg className="w-2.5 h-2.5 text-tertiary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          ${meta.costUsd.toFixed(2)}
                        </span>
                      )}
                    </>
                  )}
                  {hasText && (
                    <div className="flex items-center gap-1 transition-opacity duration-150 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover/msg:opacity-100 focus-within:opacity-100">
                      <CopyMessageButton text={extractPlainText(msg.blocks, asides)} />
                      {/* Sound icon only once the message is complete — `meta` is
                          the completion signal; `!streaming` covers older rows
                          (never read half-streamed text). */}
                      {(!!meta || !streaming) && <SoundIcon text={extractPlainText(msg.blocks, asides)} />}
                      {/* Report: the same complete-reply guard, an agent's
                          reply (never a person's message shown as one), a
                          chat that exists. */}
                      {(!!meta || !streaming) && isAgentReply(msg) && chatId && (
                        <ReportReplyButton chatId={chatId} messages={messages} index={msgIdx} />
                      )}
                    </div>
                  )}
                </div>
              )}
            </>
          )

          return (
            <ErrorBoundary key={msg.id} variant="inline" label="This message">
            <div className={continues ? 'group/msg -mt-3' : 'group/msg'} data-msg-id={msg.id}
                 data-continuation={continues || undefined}
                 style={{ overflowWrap: 'break-word', wordBreak: 'break-word' }}>
              {showSeparator && (
                <div className="border-t border-subtle mb-6" />
              )}
              {card ? (
                <DelegateCard
                  card={card}
                  blocks={msg.blocks}
                  open={openCards.has(cardKey)}
                  onToggle={() => onToggleCard(cardKey)}
                  header={header}
                >
                  {body}
                </DelegateCard>
              ) : (
                <>
                  {!continues && <div className="flex items-center gap-2 mb-3">{header}</div>}
                  {body}
                </>
              )}
            </div>
            </ErrorBoundary>
          )
        })}

        {/* Held steers: in the engine already, waiting for its next step —
            shown as the message they are, with no caption (the bubble alone
            says it was sent). */}
        {pendingSteers && pendingSteers.length > 0 && (
          <div className="space-y-2" data-testid="pending-steers">
            {pendingSteers.map((item, i) => (
              <div key={`s-${i}`} className="flex justify-end">
                <div className="max-w-[85%] rounded-card px-4 py-3 bg-action-subtle text-primary border border-dashed border-action">
                  <QueuedAttachments item={item} agentName={agentName} />
                  {item.text && <p className="text-sm whitespace-pre-wrap">{item.text}</p>}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Queued messages */}
        {queuedMessages && queuedMessages.length > 0 && (
          <div className="space-y-2">
            {queuedMessages.map((item, i) => (
              <div key={item.queueId ?? `q-${i}`} className="flex justify-end">
                <div className="max-w-[85%] rounded-card px-4 py-3 bg-action-subtle text-primary border border-dashed border-action relative group">
                  <QueuedAttachments item={item} agentName={agentName} />
                  {item.text && <p className="text-sm whitespace-pre-wrap">{item.text}</p>}
                  <span className="text-xs text-secondary">queued</span>
                  {item.waiting && (
                    <p className="text-xs text-secondary mt-1" data-testid="queued-waiting">
                      {item.waiting === QUEUE_WAITING.TERMINAL
                        ? "A teammate's terminal is open on this chat. Your message goes out when it closes."
                        : item.waiting === QUEUE_WAITING.MACHINE
                          ? 'The machine running this chat is not answering. Your message goes out when it does.'
                          : 'The machine running this chat is reconnecting. Your message goes out when it is back.'}
                    </p>
                  )}
                  {onCancelQueued && (mayCancelQueued?.(item) ?? true) && (
                    // Shown on a touch screen (no hover there), on hover
                    // with a mouse; a 32 px target.
                    <button
                      onClick={() => onCancelQueued(i)}
                      aria-label="Cancel queued message"
                      title="Cancel"
                      data-testid="cancel-queued"
                      className="absolute -top-3 -right-3 w-8 h-8 rounded-full bg-status-danger-fill text-on-solid text-xs
                                 flex items-center justify-center transition-opacity
                                 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100
                                 focus-visible:opacity-100"
                    >
                      &#10005;
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

      </div>
    </div>

    {/* Scroll-to-bottom button — positioned over the scroll area, outside the scroll container */}
    {showScrollBtn && (
      <div className="absolute bottom-3 left-0 right-0 flex justify-center pointer-events-none z-10">
        <div className="pointer-events-auto">
          <ScrollToBottomButton onClick={scrollToBottom} />
        </div>
      </div>
    )}
    </div>
  )
}
