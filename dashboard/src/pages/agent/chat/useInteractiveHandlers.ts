/**
 * useInteractiveHandlers — AgentChat's interactive-CLI handlers (the per-chat
 * toggle with its live kill+rewarm confirm, terminal exit, the live
 * terminal ⇄ DB rich-view toggle) and the queue handlers (cancel one queued
 * message / pull them all back into the composer). Called where the page
 * declared handleInteractiveToggle, so the React hook order is unchanged.
 */
import { useCallback, useLayoutEffect, useRef } from 'react'
import type { useChatStream } from '../../../hooks/useChatStream'
import type { useInteractiveChat } from '../../../hooks/useInteractiveChat'
import { fetchChatPage } from '../../../api/chats'
import { useChatStore } from '../../../store/chatStore'
import type { QueuedMessage } from '../../../store/types'
import { useAgentPrefsStore } from '../../../store/agentPrefsStore'
import { useDialogs } from '../../../components/ui/dialogs'

type Stream = ReturnType<typeof useChatStream>

export function useInteractiveHandlers({
  agentName, sessionId, chatId, interactive, viewedStreaming, ws, seedDbHistory, draftKey, queuedMessages, setEditText,
}: {
  agentName: string | undefined
  sessionId: Stream['sessionId']
  chatId: Stream['chatId']
  interactive: ReturnType<typeof useInteractiveChat>
  viewedStreaming: boolean
  ws: Stream['ws']
  seedDbHistory: Stream['seedDbHistory']
  draftKey: string
  queuedMessages: QueuedMessage[]
  setEditText: Stream['setEditText']
}) {
  // The members the handlers call (the bundle is rebuilt every render).
  const { toggle, performSwitch, setSessionInteractive, showRichView, setShowRichView } = interactive
  const dialogs = useDialogs()
  // What the switch reads after its confirm: the turn may have ended, or the
  // page moved to another chat, while the dialog was open.
  const liveRef = useRef({ streaming: ws.streaming, chatId, sessionId })
  useLayoutEffect(() => { liveRef.current = { streaming: ws.streaming, chatId, sessionId } })
  // Interactive CLI toggle. No live session:
  // set the per-chat intent + persist it (the next send spawns the chosen mode).
  // Live session: kill+rewarm in the target mode (deferred to turn-end if a -p
  // turn streams); a turn in flight confirms first — the switch kills it.
  const handleInteractiveToggle = useCallback(async (next: boolean) => {
    // Sticky for the same agent's next new chat (the interactive twin of the
    // model/mode stickiness). Persist the EXPLICIT choice both ways so
    // turning OFF overrides an interactive agent default on the next new chat.
    if (agentName) {
      useAgentPrefsStore.getState().setLastInteractive(agentName, next ? 'interactive' : '-p')
    }
    if (!sessionId || !chatId) {
      toggle(next, chatId)
      return
    }
    // A live/streaming turn dies with the switch (switch_execution_mode kills
    // the running process) — confirm before destroying a response in flight.
    // Gate on the VIEWED chat's per-chat slice OR the connection-global flag
    // so a turn started in another tab still warns. An IDLE live session
    // switches without a prompt: the restart is lossless (the conversation is
    // kept and reloaded).
    if (viewedStreaming || ws.streaming) {
      if (!(await dialogs.confirm({
        title: 'Switch anyway?',
        body: 'Switching the mode stops the current response.',
        confirmLabel: 'Switch',
        tone: 'danger',
      }))) return
      const live = liveRef.current
      // The page moved to another chat: the answer was for this one.
      if (live.chatId !== chatId) return
      // The session ended while the dialog was open: the no-session path.
      if (!live.sessionId) toggle(next, chatId)
      else performSwitch(next, chatId, live.streaming)
      return
    }
    performSwitch(next, chatId, ws.streaming)
  }, [toggle, performSwitch, sessionId, chatId, ws.streaming, viewedStreaming, agentName, dialogs])

  // Interactive PTY died (the user quit the TUI with Ctrl+C/Ctrl+D, or it was
  // reaped). Leave the dead terminal for the DB rich view — reload chat_history
  // (the message list never synced the terminal's turns) — and the lazy
  // warmup_ready clears sessionId so the next send re-warms + RESUMES the session
  // (routeSend cold-start).
  const handleTerminalExit = useCallback(() => {
    if (!chatId) return
    setSessionInteractive(false)
    ws.resumeChat(chatId)
  }, [chatId, setSessionInteractive, ws])

  // View toggle: flip the live terminal ⇄ the DB rich
  // conversation history WITHOUT touching the session. Turning ON fetches a
  // FRESH snapshot — GET /v1/chats/{id} reads the DB and never touches the live
  // PTY (unlike resume_chat) — then maps it through the shared history mapper;
  // the terminal stays mounted (hidden) and keeps streaming underneath. OFF
  // re-shows it. The page `messages` only back the rich view here (the terminal
  // is the live surface), so overwriting them is safe.
  const handleToggleRichView = useCallback(async () => {
    if (!chatId) return
    if (showRichView) {
      setShowRichView(false)
      return
    }
    try {
      const { messages: rows, has_more } = await fetchChatPage(chatId, 50)
      seedDbHistory(rows, has_more)  // newest page + lazy scroll-back, same as resume
      setShowRichView(true)
    } catch { /* network error — stay on the terminal */ }
  }, [chatId, showRichView, setShowRichView, seedDbHistory])

  // A chip leaves on the proxy's queue_removed, not before (a teammate's
  // chip the proxy refuses stays). A chip a 1.7.0 proxy minted has no id:
  // it is removed here and cancelled by its index, as that proxy expects.
  // A chip of a task chat viewed through a sibling run names the queue's
  // own chat (`queueChatId`), so the proxy finds the message in the right
  // queue between turns.
  const handleCancelQueued = useCallback(
    (i: number) => {
      const item = queuedMessages[i]
      ws.cancelQueued({ queueId: item?.queueId, index: i }, item?.queueChatId ?? chatId ?? undefined)
      if (!item?.queueId && draftKey) {
        useChatStore.getState().removeQueuedMessage(draftKey, { index: i })
      }
    },
    [ws, draftKey, chatId, queuedMessages],
  )

  // Pull this person's queued messages back to input for editing. The proxy
  // returns them, text and attachments, on the author's queue_cleared (the
  // chat's queue is the proxy's). A 1.7.0 proxy's chips carry no id: their
  // texts and attachments are joined here, as that proxy expects.
  const handleEditQueued = useCallback(
    () => {
      if (queuedMessages.length === 0) return
      if (queuedMessages.some(q => q.queueId)) {
        const queueChats = new Set(queuedMessages.map(q => q.queueChatId).filter(Boolean))
        const [only] = queueChats
        ws.cancelAllQueued((queueChats.size === 1 ? only : undefined) ?? chatId ?? undefined)
        return
      }
      const combined = queuedMessages.map(q => q.text).join('\n\n')
      ws.cancelAllQueued()
      if (draftKey) {
        const st = useChatStore.getState()
        st.clearQueuedMessages(draftKey)
        const images = queuedMessages.flatMap(q => q.images ?? []).filter(i => i.path)
        const files = queuedMessages.flatMap(q => q.files ?? [])
        if (images.length) {
          st.addPendingImages(draftKey, images.map(i => ({ id: `img-${i.path}`, path: i.path, name: i.name })))
        }
        if (files.length) {
          st.addPendingFiles(draftKey, files.map(f => ({
            id: `file-${f.path}`, name: f.name, size: 0, uploadedPath: f.path,
            ...(f.folder ? { folder: true } : {}),
          })))
        }
      }
      setEditText(combined)
    },
    [queuedMessages, ws, draftKey, chatId, setEditText],
  )

  return { handleInteractiveToggle, handleTerminalExit, handleToggleRichView, handleCancelQueued, handleEditQueued }
}
