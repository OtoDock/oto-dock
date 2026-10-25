// The queue and the steers of the chat stream (split from useChatStream):
// the markers of a send that already drew its bubble, the steers the engine
// accepted mid-turn and holds until a block boundary, the composer's edit
// text a cancelled message comes back as, and the queue frames.
import { useCallback, useRef, useState } from 'react'
import { useChatStore } from '@/store/chatStore'
import { toQueuedMessage, type QueuedMessage } from '@/store/types'
import { WIRE, type SteeredFrame } from '../../api/wireEvents'
import type { WsCallbacks } from '../useDashboardWs.types'
import type { DisplayMessage } from '../../components/chat/types'
import { attachmentBlocks, delegatePromptCard } from '../../lib/messageBlocks'
import type { StreamCore } from './core'

/** A steer accepted by the engine, waiting for the block boundary where it
 * lands: the text, the attachment meta and (a delegated steer) the
 * delegating agent's identity. */
type HeldSteer = {
  text: string
  images?: SteeredFrame['images']
  files?: SteeredFrame['files']
  meta?: SteeredFrame['event_data']
}

function steeredUserMessage(h: HeldSteer, id: string): DisplayMessage {
  // A continue steered into a running lane is the worker's next prompt: a
  // card, keyed like its stored row (`db-<id>`) when the frame names it.
  const promptCard = h.meta?.agent_slug ? delegatePromptCard(h.meta.badge, id) : undefined
  return {
    id,
    role: 'user',
    blocks: [...attachmentBlocks(h.images, h.files), { type: 'text', content: h.text }],
    createdAt: new Date().toISOString(),
    agentSlug: h.meta?.agent_slug,
    agentDisplayName: h.meta?.agent_display_name,
    agentColor: h.meta?.agent_color,
    badge: h.meta?.badge,
    steered: true,
    ...(promptCard ? { delegateCard: promptCard } : {}),
  }
}

/** The steers held for one chat: a steer belongs to the chat it was sent
 * in, so the chat just left never renders one into the new chat's turn. */
type HeldSteers = { chatId: string | null; held: HeldSteer[] }
const NO_HELD: HeldSteers = { chatId: null, held: [] }
const NO_STEERS: QueuedMessage[] = []

export function useQueueAndSteer(core: StreamCore, meetingActive: boolean) {
  const { options, chatId, chatIdRef, discardingRef, setMessages, currentMsgRef, setTurnStartTime } = core

  // Queue editing state
  const [editText, setEditText] = useState<string | null>(null)
  // The TEXT of the last send that already added a user bubble. If the
  // backend unexpectedly queues THAT SAME message (stale pump from WS
  // reconnect), onQueued/onQueueSent skip the duplicate bubble/queue entry —
  // matched by text, because a boolean here suppressed every queued chip for
  // the rest of the turn: a mid-turn send (claude's queue fallback) arrived
  // while the flag was still armed from the turn-opening send and its chip
  // silently vanished until the post-turn drain.
  const sentWithBubbleRef = useRef<string | null>(null)
  // That send's queue id (``sendMessage`` mints it): a `queued` frame naming
  // it is that message whatever its text, and a teammate's message with the
  // same text is not.
  const sentWithBubbleIdRef = useRef<string | null>(null)
  // Steers accepted mid-turn, held until the next block boundary. Codex
  // consumes an accepted steer at the next sampling-round boundary — while
  // the current text block is still streaming, the model has NOT read it
  // yet. Splitting immediately cut sentences in half; instead the user
  // bubble + fresh assistant header render at the boundary where the steer
  // actually lands: the next tool/thinking/subagent block, or turn end.
  const pendingSteerRef = useRef<HeldSteers>(NO_HELD)
  // The held steers, mirrored into state so the page can SHOW them while
  // they wait for the boundary (a long tool call otherwise hides a steered
  // message for minutes). Display only: the message already sits in the
  // engine, so nothing here is editable or cancellable. The list shown is
  // the viewed chat's.
  const [heldSteers, setHeldSteers] = useState<{ chatId: string | null; items: QueuedMessage[] }>(
    { chatId: null, items: NO_STEERS },
  )
  const pendingSteers = heldSteers.chatId === chatId ? heldSteers.items : NO_STEERS
  const holdSteer = (h: HeldSteer) => {
    const cid = chatIdRef.current
    const cur = pendingSteerRef.current
    const held = [...(cur.chatId === cid ? cur.held : []), h]
    pendingSteerRef.current = { chatId: cid, held }
    setHeldSteers({ chatId: cid, items: held.map(toQueuedMessage) })
  }
  /** A history load drops the steers held for the view it replaces. */
  const resetSteers = () => {
    pendingSteerRef.current = NO_HELD
    setHeldSteers((cur) => (cur.items.length ? { chatId: null, items: NO_STEERS } : cur))
  }
  /** A turn's end, whichever way, forgets the send that drew its bubble. */
  const clearSentMarkers = () => {
    sentWithBubbleRef.current = null
    sentWithBubbleIdRef.current = null
  }

  /** Render any held steers: user bubble(s) in accept order, then (while the
   * turn keeps streaming) a fresh assistant continuation that subsequent
   * blocks land in. `withContinuation=false` at turn end/abort — nothing
   * streams after, so only the bubbles are owed. No-op when nothing is held. */
  const flushPendingSteer = useCallback((withContinuation = true) => {
    const cur = pendingSteerRef.current
    if (!cur.held.length) return
    const held = cur.chatId === chatIdRef.current ? cur.held : []
    pendingSteerRef.current = NO_HELD
    setHeldSteers({ chatId: null, items: NO_STEERS })
    if (!held.length) return
    const now = Date.now()
    const userMsgs: DisplayMessage[] = held.map((h, i) => steeredUserMessage(h, `user-${now}-${i}`))
    if (!withContinuation) {
      setMessages((prev) => [...prev, ...userMsgs])
      return
    }
    const continuation: DisplayMessage = {
      id: `stream-${now}`,
      role: 'assistant',
      blocks: [],
      createdAt: new Date().toISOString(),
    }
    currentMsgRef.current = continuation
    setMessages((prev) => [...prev, ...userMsgs, continuation])
  }, [chatIdRef, currentMsgRef, setMessages])

  const callbacks: Pick<WsCallbacks, 'onQueued' | 'onQueueRemoved' | 'onQueueSent' | 'onSteered' | 'onQueueEditReturn'> = {
    onQueued: (data) => {
      if (discardingRef.current) return
      if (data.queue_id) {
        // The chip is in the store (the socket hook files it by chat). A
        // send the page showed as a new turn that the proxy queued instead
        // (another turn drives the chat): its bubble becomes the chip.
        const mine = sentWithBubbleIdRef.current
          ? sentWithBubbleIdRef.current === data.queue_id
          : sentWithBubbleRef.current === data.text
        if (mine) {
          sentWithBubbleRef.current = null
          sentWithBubbleIdRef.current = null
          // The placeholder the send minted is the bubble: it goes with the
          // pair, and the ref with it. A frame of the other turn that landed
          // in it in the few ms before this ack keeps the pair and the ref,
          // so that reply goes on there. The ref is dropped inside the
          // updater on purpose: the decision needs the list as it is, and
          // both StrictMode runs take it the same way (the same prev, the
          // same null).
          setMessages((prev) => {
            const n = prev.length
            if (n >= 2 && prev[n - 1].role === 'assistant' && prev[n - 1].blocks.length === 0
                && prev[n - 2].role === 'user') {
              if (currentMsgRef.current?.id === prev[n - 1].id) currentMsgRef.current = null
              return prev.slice(0, -2)
            }
            return prev
          })
        }
        return
      }
      // A 1.7.0 proxy: skip the chip ONLY when this exact message already
      // has a bubble (the reconnect/stale-pump dedup).
      if (sentWithBubbleRef.current === data.text) return
      options.queue.addQueued(data.index, toQueuedMessage(data))
    },
    onQueueRemoved: (_msg) => {
      // The chip left the store in the socket hook (by its id); a 1.7.0
      // proxy's chip was removed by the page's cancel handler. The author's
      // copy hands the text back (onQueueEditReturn).
    },
    onQueueSent: (data) => {
      if (discardingRef.current) return
      // A 1.7.0 proxy names no message: every chip goes.
      if (!data.queue_ids) options.queue.clearQueued()
      // If the page already added a user bubble + placeholder for THIS message,
      // don't duplicate — just refresh the timer for the actual streaming start.
      if (sentWithBubbleRef.current === data.text) {
        sentWithBubbleRef.current = null
        sentWithBubbleIdRef.current = null
        setTurnStartTime(Date.now())
        return
      }
      if (sentWithBubbleRef.current !== null && data.queue_ids?.length) {
        // A fresh send took this person's waiting messages ahead of itself
        // (one turn, their rows first): their bubble goes before the one the
        // page just added.
        const fresh = sentWithBubbleRef.current
        const drained: DisplayMessage = {
          id: `user-${Date.now()}`,
          role: 'user',
          blocks: [...attachmentBlocks(data.images, data.files), { type: 'text', content: data.text }],
          createdAt: new Date().toISOString(),
        }
        setMessages((prev) => {
          const at = prev.length - 2
          const optimistic = prev[at]
          if (at >= 0 && optimistic.role === 'user'
              && optimistic.blocks.some((b) => b.type === WIRE.TEXT && b.content === fresh)) {
            return [...prev.slice(0, at), drained, ...prev.slice(at)]
          }
          return [...prev, drained]
        })
        return
      }
      // Queue processed — add user bubble (chips + text) + assistant placeholder.
      const userMsg: DisplayMessage = {
        id: `user-${Date.now()}`,
        role: 'user',
        blocks: [...attachmentBlocks(data.images, data.files), { type: 'text', content: data.text }],
        createdAt: new Date().toISOString(),
      }
      // During meetings, skip the assistant placeholder — meeting agents create
      // their own messages. An empty placeholder would persist with loading dots
      // since no agent fills it.
      if (meetingActive) {
        setMessages((prev) => [...prev, userMsg])
        return
      }
      const placeholder: DisplayMessage = {
        id: `stream-${Date.now()}`,
        role: 'assistant',
        blocks: [],
        createdAt: new Date().toISOString(),
      }
      currentMsgRef.current = placeholder
      setMessages((prev) => [...prev, userMsg, placeholder])
      setTurnStartTime(Date.now())
    },

    onSteered: (data) => {
      if (discardingRef.current) return
      // Mid-turn steer accepted: the message went INTO the running turn (so
      // no queue entry and no timer reset). A frame naming its row
      // (`message_id`) comes at the point of the stream where the proxy saved
      // the turn so far and wrote the steer's row after it: the bubble
      // renders here and the answer continues below it, as a reload shows
      // it. A 1.7.0 proxy's frame names no row and arrives before the engine
      // read it: the bubble waits for the next block boundary (the
      // flushPendingSteer calls in the block handlers + turn end).
      const held: HeldSteer = {
        text: data.text, images: data.images, files: data.files, meta: data.event_data,
      }
      if (currentMsgRef.current && !data.message_id) {
        holdSteer(held)
        return
      }
      // Idle position (between blocks), or the proxy's own place for it:
      // render at the live position and continue the assistant's response
      // in a NEW message below it.
      const userMsg = steeredUserMessage(
        held, data.message_id ? `db-${data.message_id}` : `user-${Date.now()}`)
      const continuation: DisplayMessage = {
        id: `stream-${Date.now()}`,
        role: 'assistant',
        blocks: [],
        createdAt: new Date().toISOString(),
      }
      currentMsgRef.current = continuation
      setMessages((prev) => [...prev, userMsg, continuation])
    },

    onQueueEditReturn: (data) => {
      // Pull the text back into the composer for editing, and the
      // attachments the queued message carried with it. A draft the person
      // is typing stays, the returned text after it (a failed turn or a
      // Stop returns messages they did not ask back). An attachment-only
      // message returns no text: the composer's draft stays as it is.
      if (data.text) {
        const cid = chatIdRef.current
        const draft = cid ? (useChatStore.getState().byChat[cid]?.draftInput ?? '') : ''
        setEditText(draft.trim() ? `${draft}\n\n${data.text}` : data.text)
      }
      if (data.images?.length || data.files?.length) {
        options.queue.restoreAttachments?.(data.images ?? [], data.files ?? [])
      }
    },

  }

  return {
    callbacks, pendingSteers, editText, setEditText, sentWithBubbleRef, sentWithBubbleIdRef,
    flushPendingSteer, clearSentMarkers, resetSteers,
  }
}
