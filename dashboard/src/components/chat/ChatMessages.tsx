import { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo } from 'react'
import { SoundIcon } from './SoundIcon'
import { useSearch } from '../../contexts/SearchContext'
import type { DisplayMessage, MessageBlock } from './types'
import { pairBgCommandBlocks, previewChainModes, supersededUiBlocks, uiTitlesByPath } from '../../lib/messageBlocks'
import BlockRenderer from './ChatBlockRenderer'
import ErrorBoundary from '../ErrorBoundary'

interface Props {
  messages: DisplayMessage[]
  agentName?: string
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
  onDismissPreview?: (fileId: string, key?: { snapshotId?: string; dbMessageId?: number }) => void
  onArtifactInteraction?: (token: string, title: string, payload: unknown) => Promise<{ status: string; reason?: string }>
  streaming?: boolean
  queuedMessages?: string[]
  onCancelQueued?: (index: number) => void
  // Lazy chat-history scroll-back (loads older turns when the top comes into view).
  onLoadOlder?: () => void
  hasMoreOlder?: boolean
  loadingOlder?: boolean
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

// Extract plain text from message blocks for the copy button
function extractPlainText(blocks: MessageBlock[]): string {
  return blocks
    .filter((b): b is { type: 'text'; content: string } => b.type === 'text')
    .map(b => b.content)
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
      className="copy-msg-btn p-1 rounded-sm hover:bg-black/5 dark:hover:bg-white/10 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300"
    >
      {copied ? (
        <svg className="w-3.5 h-3.5 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
                 bg-white dark:bg-p-surface border border-p-border-light shadow-md text-brand hover:text-brand-hover hover:shadow-lg
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
  agentColor,
  chatId,
  onPermissionRespond,
  onPlanReviewResponse,
  onImplementPlan,
  onImplementPlanCodex,
  onQuestionAnswer,
  onQuestionAnswerStructured,
  onSendMessage,
  onPlanFetched,
  onDismissPreview,
  onArtifactInteraction,
  streaming,
  queuedMessages,
  onCancelQueued,
  onLoadOlder,
  hasMoreOlder,
  loadingOlder,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const olderSentinelRef = useRef<HTMLDivElement>(null)
  const userScrolledUp = useRef(false)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const { query: searchQuery, currentMatch } = useSearch()
  const streamingRef = useRef(streaming)
  streamingRef.current = streaming

  // A re-displayed ui artifact renders only at its newest position — older
  // instances of the same workspace file collapse to a chip (see UiArtifact) —
  // and an html-less re-display (no title on the wire) inherits the latest
  // title known for its path.
  const supersededUi = useMemo(() => supersededUiBlocks(messages), [messages])
  const uiTitles = useMemo(() => uiTitlesByPath(messages), [messages])
  // Document previews: render-time live → frozen ("previous version") → chip
  // chain per fileId over the loaded block list (each block applies/defers
  // its own transition — see DocumentPreview).
  const previewModes = useMemo(() => previewChainModes(messages), [messages])

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
      accumulateUpIntent(-e.deltaY)
      refreshPrependAnchor()
      checkPosition()
    }
    const handleTouchStart = (e: TouchEvent) => {
      if (e.touches.length === 1) lastTouchYRef.current = e.touches[0].clientY
    }
    const handleTouchMove = (e: TouchEvent) => {
      lastGestureAtRef.current = Date.now()
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
    setShowScrollBtn(false)
    if (containerRef.current) {
      // flex-col-reverse: scrollTop=0 = bottom (newest messages)
      containerRef.current.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }, [])

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
  canLoadOlderRef.current = !!hasMoreOlder && !loadingOlder
  const onLoadOlderRef = useRef(onLoadOlder)
  onLoadOlderRef.current = onLoadOlder
  useEffect(() => {
    const root = containerRef.current
    const target = olderSentinelRef.current
    if (!root || !target) return
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting) && canLoadOlderRef.current) {
          prependAnchorRef.current = root.scrollTop
          onLoadOlderRef.current?.()
        }
      },
      { root, rootMargin: '200px 0px 0px 0px' },
    )
    io.observe(target)
    return () => io.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The prepend anchor: armed at request time, consumed exactly once when the
  // oldest loaded row changes (the page landed). useLayoutEffect runs after
  // the DOM mutation but before paint, so a mis-anchored engine never shows
  // the jumped frame.
  const prependAnchorRef = useRef<number | null>(null)
  const oldestId = messages[0]?.id
  const prevOldestIdRef = useRef(oldestId)
  useLayoutEffect(() => {
    const prev = prevOldestIdRef.current
    prevOldestIdRef.current = oldestId
    if (prependAnchorRef.current === null || oldestId === prev) return
    const el = containerRef.current
    if (el) el.scrollTop = prependAnchorRef.current
    prependAnchorRef.current = null
  }, [oldestId])

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
        {loadingOlder && (
          <div className="text-center text-xs text-p-text-light py-2">Loading older messages…</div>
        )}
        {messages.map((msg, msgIdx) => {
          const hasText = msg.blocks.some(b => b.type === 'text')

          if (msg.role === 'user' && !msg.agentSlug) {
            return (
              // Per-message crash isolation: one corrupt block must not
              // unmount the whole app (blank page) — see ErrorBoundary.
              <ErrorBoundary key={msg.id} variant="inline" label="This message">
              <div className="flex flex-col items-end group/msg">
                {/* Timestamp above bubble */}
                <span className="text-[11px] text-p-text-light mb-1 mr-1">
                  {formatMessageTime(msg.createdAt)}
                </span>
                {/* Bubble */}
                <div
                  className="max-w-[85%] rounded-xl px-4 py-3 bg-brand text-white overflow-hidden"
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
                          onSendMessage={onSendMessage}
                          onDismissPreview={onDismissPreview}
                          onPlanFetched={onPlanFetched}
                          onArtifactInteraction={onArtifactInteraction}
                        />
                      )
                    })}
                  </div>
                </div>
                {/* Copy below bubble, right-aligned */}
                {hasText && (
                  <div className="mt-1 mr-1 opacity-100 md:opacity-0 md:group-hover/msg:opacity-100 transition-opacity duration-150">
                    <CopyMessageButton text={extractPlainText(msg.blocks)} />
                  </div>
                )}
              </div>
              </ErrorBoundary>
            )
          }

          // Assistant message — full width, no bubble
          const meta = msg.blocks.find((b): b is Extract<MessageBlock, {type:'metadata'}> => b.type === 'metadata')
          // Fold each background command's Bash tool block into its bgcommand
          // pill (one expandable card per command instead of two stacked ones).
          const { hiddenToolIdx, bgPairs } = pairBgCommandBlocks(msg.blocks)
          const msgSlug = msg.agentSlug || agentName
          const displayName = msg.agentDisplayName
            || (msgSlug ? msgSlug.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : 'Assistant')
          const initial = (msgSlug || 'A').charAt(0).toUpperCase()
          const avatarBg = msg.agentColor || agentColor || ''

          // Subtle separator between consecutive assistant messages
          const prevMsg = msgIdx > 0 ? messages[msgIdx - 1] : null
          const showSeparator = prevMsg && (prevMsg.role === 'assistant' || prevMsg.agentSlug)

          return (
            <ErrorBoundary key={msg.id} variant="inline" label="This message">
            <div className="group/msg" style={{ overflowWrap: 'break-word', wordBreak: 'break-word' }}>
              {showSeparator && (
                <div className="border-t border-p-border-light/50 dark:border-gray-700/50 mb-6" />
              )}
              {/* Header: avatar + agent name + badge + timestamp */}
              <div className="flex items-center gap-2 mb-3">
                <span
                  className={`flex items-center justify-center w-6 h-6 rounded-full text-white text-xs font-semibold shrink-0 ${avatarBg ? '' : 'bg-brand'}`}
                  style={avatarBg ? { backgroundColor: avatarBg } : undefined}
                >
                  {initial}
                </span>
                <span className="text-sm font-semibold text-p-text">{displayName}</span>
                {msg.badge && (
                  <span className={`px-1.5 py-0.5 rounded-sm text-[10px] font-medium ${
                    msg.badge === 'meeting'
                      ? 'bg-[#0891b2]/10 text-[#0891b2]'
                      : 'bg-[#0d9488]/10 text-p-accent-teal'
                  }`}>
                    {msg.badge}
                  </span>
                )}
                <span className="text-[11px] text-p-text-light">
                  {formatMessageTime(msg.createdAt)}
                </span>
              </div>

              {/* Content blocks */}
              <div className="space-y-3">
                {msg.blocks.map((block, i) => {
                  if (block.type === 'metadata') return null
                  if (hiddenToolIdx.has(i)) return null
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
                      onSendMessage={onSendMessage}
                      onPlanFetched={onPlanFetched}
                      onDismissPreview={onDismissPreview}
                      onArtifactInteraction={onArtifactInteraction}
                      bgPair={bgPairs.get(i)}
                      uiSuperseded={supersededUi.has(`${msgIdx}:${i}`)}
                      uiTitle={block.type === 'ui' && !block.title && block.path ? uiTitles.get(block.path) : undefined}
                      previewMode={block.type === 'document_preview' ? previewModes.get(`${msgIdx}:${i}`) : undefined}
                    />
                  )
                })}
              </div>

              {/* Typing indicator — shows when assistant has no content yet */}
              {!msg.blocks.some(b => b.type !== 'metadata') && (
                <div className="flex items-center gap-1 py-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-p-text-light/50 animate-bounce" style={{ animationDelay: '0ms' }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-p-text-light/50 animate-bounce" style={{ animationDelay: '150ms' }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-p-text-light/50 animate-bounce" style={{ animationDelay: '300ms' }} />
                </div>
              )}

              {/* Footer: copy + badges, all left-aligned */}
              {(hasText || meta) && (
                <div className="flex items-center gap-2 mt-2 text-[11px]">
                  {meta && (
                    <>
                      {meta.durationMs != null && (
                        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-p-surface text-p-text-secondary">
                          <svg className="w-2.5 h-2.5 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          {(meta.durationMs / 1000).toFixed(1)}s
                        </span>
                      )}
                      {meta.costUsd != null && (
                        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-p-surface text-p-text-secondary">
                          <svg className="w-2.5 h-2.5 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          ${meta.costUsd.toFixed(2)}
                        </span>
                      )}
                    </>
                  )}
                  {hasText && (
                    <div className="flex items-center gap-1 opacity-100 md:opacity-0 md:group-hover/msg:opacity-100 transition-opacity duration-150">
                      <CopyMessageButton text={extractPlainText(msg.blocks)} />
                      {/* Sound icon only once the message is complete — `meta` is
                          the completion signal; `!streaming` covers older rows
                          (never read half-streamed text). */}
                      {(!!meta || !streaming) && <SoundIcon text={extractPlainText(msg.blocks)} />}
                    </div>
                  )}
                </div>
              )}
            </div>
            </ErrorBoundary>
          )
        })}

        {/* Queued messages */}
        {queuedMessages && queuedMessages.length > 0 && (
          <div className="space-y-2">
            {queuedMessages.map((text, i) => (
              <div key={`q-${i}`} className="flex justify-end">
                <div className="max-w-[85%] rounded-xl px-4 py-3 bg-brand/40 text-white opacity-60 relative group">
                  <p className="text-sm">{text}</p>
                  <span className="text-xs opacity-75">queued</span>
                  {onCancelQueued && (
                    <button
                      onClick={() => onCancelQueued(i)}
                      className="absolute -top-1 -right-1 w-5 h-5 rounded-full bg-red-500 text-white text-xs
                                 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center"
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
