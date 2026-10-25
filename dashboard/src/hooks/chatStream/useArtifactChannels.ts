// The artifact channels of the chat stream (split from useChatStream):
// the display_ui pages and the check verdicts on the socket's registry,
// the display_ui backchannel (an interaction and its ack) and the pinned
// apps' send_prompt actions (an action and its ack), each a turn the chat
// shows as a chip. Subscribed through the socket, so called after it.
import { useCallback, useEffect, useEffectEvent, useRef } from 'react'
import { useChatStore } from '@/store/chatStore'
import type { DisplayMessage } from '../../components/chat/types'
import { eventToBlock } from '../../lib/messageBlocks'
import { WIRE, type UiFrame, type CheckVerdictFrame, type ArtifactInteractionFrame, type AppActionFrame } from '../../api/wireEvents'
import type { useDashboardWs } from '../useDashboardWs'
import type { StreamCore } from './core'

export function useArtifactChannels(core: StreamCore, ws: ReturnType<typeof useDashboardWs>) {
  const {
    chatIdRef, discardingRef, abortedRef, setMessages, currentMsgRef, ensureAssistantMsg, appendBlock,
    setTurnStartTime,
  } = core

  // display_ui artifacts ride the generic frame registry rather than a
  // useDashboardWs switch case — the registry fires for EVERY frame type, so
  // a self-contained feature needs no dispatcher growth. `ui` is not in
  // PER_CHAT_FRAMES, so the handler self-gates by chat_id (background chats'
  // frames must not render here). An effect event, so the once-registered
  // subscriber calls the newest closure.
  const onUiFrame = useEffectEvent((msg: UiFrame | CheckVerdictFrame) => {
    if (discardingRef.current) return
    if (msg.chat_id && chatIdRef.current && msg.chat_id !== chatIdRef.current) return
    const block = eventToBlock(msg)
    if (!block) return
    ensureAssistantMsg()
    appendBlock(block)
  })
  const wsSubscribe = ws.subscribe
  useEffect(() => wsSubscribe(WIRE.UI, (msg) => {
    if (msg.type === WIRE.UI) onUiFrame(msg)
  }), [wsSubscribe])
  // A check's verdict card (CHECKS.md) rides the same path: the evaluator
  // pushes the `check_verdict` event onto the chat's pump after the turn,
  // and history replays the persisted event row through eventToBlock.
  useEffect(() => wsSubscribe(WIRE.CHECK_VERDICT, (msg) => {
    if (msg.type === WIRE.CHECK_VERDICT) onUiFrame(msg)
  }), [wsSubscribe])

  // display_ui backchannel. Two disjoint chip-append sources: the
  // `artifact_interaction` frame renders QUEUED interactions at boundary
  // delivery (pump drain), the `sent` ack below renders the idle path — the
  // server only ever emits one of the two per send, so no dedupe is needed.
  const appendArtifactChipTurn = useCallback((token: string, title: string | undefined, payload: unknown) => {
    const chipMsg: DisplayMessage = {
      id: `artifact-int-${Date.now()}`,
      role: 'assistant',
      blocks: [{ type: 'artifact_interaction', token, title, payload }],
      createdAt: new Date().toISOString(),
    }
    const placeholder: DisplayMessage = {
      id: `stream-${Date.now()}`,
      role: 'assistant',
      blocks: [],
      createdAt: new Date().toISOString(),
    }
    currentMsgRef.current = placeholder
    setMessages((prev) => [...prev, chipMsg, placeholder])
    setTurnStartTime(Date.now())
  }, [currentMsgRef, setMessages, setTurnStartTime])
  const onArtifactFrame = useEffectEvent((msg: ArtifactInteractionFrame) => {
    if (discardingRef.current) return
    if (msg.chat_id && chatIdRef.current && msg.chat_id !== chatIdRef.current) return
    appendArtifactChipTurn(msg.token || '', msg.title || undefined, msg.payload)
  })
  useEffect(() => wsSubscribe(WIRE.ARTIFACT_INTERACTION, (msg) => {
    if (msg.type === WIRE.ARTIFACT_INTERACTION) onArtifactFrame(msg)
  }), [wsSubscribe])

  // In-flight send acks, keyed by token (the server's ≥1s per-token
  // min-interval guarantees one in-flight send per token).
  const artifactAckWaiters = useRef<Map<string, (ack: { status: string; reason?: string }) => void>>(new Map())
  useEffect(() => wsSubscribe(WIRE.ARTIFACT_ACK, (msg) => {
    if (msg.type !== WIRE.ARTIFACT_ACK) return
    const waiter = artifactAckWaiters.current.get(msg.token || '')
    if (waiter) {
      artifactAckWaiters.current.delete(msg.token || '')
      waiter({ status: msg.status || 'denied', reason: msg.reason })
    }
  }), [wsSubscribe])

  const sendArtifactInteraction = useCallback(
    (token: string, title: string, payload: unknown): Promise<{ status: string; reason?: string }> => {
      const cid = chatIdRef.current
      if (!cid || !ws.connected) {
        return Promise.resolve({ status: 'unavailable', reason: 'no live chat' })
      }
      return new Promise((resolve) => {
        const timer = setTimeout(() => {
          artifactAckWaiters.current.delete(token)
          resolve({ status: 'denied', reason: 'timeout' })
        }, 10000)
        artifactAckWaiters.current.set(token, (ack) => {
          clearTimeout(timer)
          if (ack.status === 'sent') {
            // The interaction started a real turn — render its chip + a fresh
            // placeholder and flip the same streaming state a user send flips.
            abortedRef.current = false
            appendArtifactChipTurn(token, title || undefined, payload)
            ws.setStreaming(true)
            useChatStore.getState().setStreaming(cid)
          }
          resolve(ack)
        })
        ws.sendArtifactInteraction(cid, token, title, payload)
      })
    },
    [ws, appendArtifactChipTurn, abortedRef, chatIdRef],
  )

  // Pinned app send_prompt actions — the app_action twin of the artifact
  // backchannel above: same chip-turn semantics (queued interactions render
  // via the `app_action` frame at boundary drain; the `sent` ack renders the
  // idle path), acks keyed by app+action.
  const appendAppActionChipTurn = useCallback((msg: { app_id?: string; slug?: string; title?: string; action_id?: string; label?: string; prompt?: string }) => {
    const chipMsg: DisplayMessage = {
      id: `app-action-${Date.now()}`,
      role: 'assistant',
      blocks: [{
        type: 'app_action', appId: msg.app_id || '', slug: msg.slug || undefined,
        title: msg.title || undefined, actionId: msg.action_id || '',
        label: msg.label || undefined, prompt: msg.prompt || undefined,
      }],
      createdAt: new Date().toISOString(),
    }
    const placeholder: DisplayMessage = {
      id: `stream-${Date.now()}`,
      role: 'assistant',
      blocks: [],
      createdAt: new Date().toISOString(),
    }
    currentMsgRef.current = placeholder
    setMessages((prev) => [...prev, chipMsg, placeholder])
    setTurnStartTime(Date.now())
  }, [currentMsgRef, setMessages, setTurnStartTime])
  const onAppActionFrame = useEffectEvent((msg: AppActionFrame) => {
    if (discardingRef.current) return
    if (msg.chat_id && chatIdRef.current && msg.chat_id !== chatIdRef.current) return
    appendAppActionChipTurn(msg)
  })
  useEffect(() => wsSubscribe(WIRE.APP_ACTION, (msg) => {
    if (msg.type === WIRE.APP_ACTION) onAppActionFrame(msg)
  }), [wsSubscribe])

  const appActionAckWaiters = useRef<Map<string, (ack: { status: string; reason?: string }) => void>>(new Map())
  useEffect(() => wsSubscribe(WIRE.APP_ACTION_ACK, (msg) => {
    if (msg.type !== WIRE.APP_ACTION_ACK) return
    const key = `${msg.app_id || ''}:${msg.action_id || ''}`
    const waiter = appActionAckWaiters.current.get(key)
    if (waiter) {
      appActionAckWaiters.current.delete(key)
      waiter({ status: msg.status || 'denied', reason: msg.reason })
    }
  }), [wsSubscribe])

  const sendAppAction = useCallback(
    (app: { id: string; slug?: string; title?: string }, actionId: string, label: string, prompt: string, args: unknown): Promise<{ status: string; reason?: string }> => {
      const cid = chatIdRef.current
      if (!cid || !ws.connected) {
        return Promise.resolve({ status: 'unavailable', reason: 'no live chat' })
      }
      const key = `${app.id}:${actionId}`
      return new Promise((resolve) => {
        const timer = setTimeout(() => {
          appActionAckWaiters.current.delete(key)
          resolve({ status: 'denied', reason: 'timeout' })
        }, 10000)
        appActionAckWaiters.current.set(key, (ack) => {
          clearTimeout(timer)
          if (ack.status === 'sent') {
            abortedRef.current = false
            appendAppActionChipTurn({
              app_id: app.id, slug: app.slug, title: app.title,
              action_id: actionId, label, prompt,
            })
            ws.setStreaming(true)
            useChatStore.getState().setStreaming(cid)
          }
          resolve(ack)
        })
        ws.sendAppAction(cid, app.id, actionId, args)
      })
    },
    [ws, appendAppActionChipTurn, abortedRef, chatIdRef],
  )

  return { sendArtifactInteraction, sendAppAction }
}
