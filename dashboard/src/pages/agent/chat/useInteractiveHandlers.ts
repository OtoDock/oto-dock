/**
 * useInteractiveHandlers — AgentChat's interactive-CLI handlers (the per-chat
 * toggle with its live kill+rewarm confirm, terminal exit, the live
 * terminal ⇄ DB rich-view toggle) and the queue handlers (cancel one queued
 * message / pull them all back into the composer). Called where the page
 * declared handleInteractiveToggle, so the React hook order is unchanged.
 */
import { useCallback } from 'react'
import type { useChatStream } from '../../../hooks/useChatStream'
import type { useInteractiveChat } from '../../../hooks/useInteractiveChat'
import { fetchChatPage } from '../../../api/chats'
import { useChatStore } from '../../../store/chatStore'
import { useAgentPrefsStore } from '../../../store/agentPrefsStore'

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
  queuedMessages: string[]
  setEditText: Stream['setEditText']
}) {
  // Interactive CLI toggle. No live session:
  // set the per-chat intent + persist it (the next send spawns the chosen mode).
  // Live session: kill+rewarm in the target mode (deferred to turn-end if a -p
  // turn streams); a turn in flight confirms first — the switch kills it.
  const handleInteractiveToggle = useCallback((next: boolean) => {
    // Sticky for the same agent's next new chat (the interactive twin of the
    // model/mode stickiness). Persist the EXPLICIT choice both ways so
    // turning OFF overrides an interactive agent default on the next new chat.
    if (agentName) {
      useAgentPrefsStore.getState().setLastInteractive(agentName, next ? 'interactive' : '-p')
    }
    if (!sessionId || !chatId) {
      interactive.toggle(next, chatId)
      return
    }
    // A live/streaming turn dies with the switch (switch_execution_mode kills
    // the running process) — confirm before destroying a response in flight.
    // Gate on the VIEWED chat's per-chat slice OR the connection-global flag
    // so a turn started in another tab still warns. An IDLE live session
    // switches without a prompt: the restart is lossless (the conversation is
    // kept and reloaded). window.confirm is this page's existing confirm idiom.
    if (viewedStreaming || ws.streaming) {
      if (!window.confirm('Switching mode stops the current response. Switch anyway?')) return
    }
    interactive.performSwitch(next, chatId, ws.streaming)
  }, [interactive.toggle, interactive.performSwitch, sessionId, chatId, ws.streaming, viewedStreaming, agentName])

  // Interactive PTY died (the user quit the TUI with Ctrl+C/Ctrl+D, or it was
  // reaped). Leave the dead terminal for the DB rich view — reload chat_history
  // (the message list never synced the terminal's turns) — and the lazy
  // warmup_ready clears sessionId so the next send re-warms + RESUMES the session
  // (routeSend cold-start).
  const handleTerminalExit = useCallback(() => {
    if (!chatId) return
    interactive.setSessionInteractive(false)
    ws.resumeChat(chatId)
  }, [chatId, interactive.setSessionInteractive, ws])

  // View toggle: flip the live terminal ⇄ the DB rich
  // conversation history WITHOUT touching the session. Turning ON fetches a
  // FRESH snapshot — GET /v1/chats/{id} reads the DB and never touches the live
  // PTY (unlike resume_chat) — then maps it through the shared history mapper;
  // the terminal stays mounted (hidden) and keeps streaming underneath. OFF
  // re-shows it. The page `messages` only back the rich view here (the terminal
  // is the live surface), so overwriting them is safe.
  const handleToggleRichView = useCallback(async () => {
    if (!chatId) return
    if (interactive.showRichView) {
      interactive.setShowRichView(false)
      return
    }
    try {
      const { messages: rows, has_more } = await fetchChatPage(chatId, 50)
      seedDbHistory(rows, has_more)  // newest page + lazy scroll-back, same as resume
      interactive.setShowRichView(true)
    } catch { /* network error — stay on the terminal */ }
  }, [chatId, interactive.showRichView, interactive.setShowRichView, seedDbHistory])

  const handleCancelQueued = useCallback(
    (i: number) => {
      ws.cancelQueued(i)
      if (draftKey) useChatStore.getState().removeQueuedMessageByIndex(draftKey, i)
    },
    [ws, draftKey],
  )

  // Pull ALL queued messages back to input for editing (they're combined on the backend)
  const handleEditQueued = useCallback(
    () => {
      if (queuedMessages.length === 0) return
      const combined = queuedMessages.join('\n\n')
      ws.cancelAllQueued()
      if (draftKey) useChatStore.getState().clearQueuedMessages(draftKey)
      setEditText(combined)
    },
    [queuedMessages, ws, draftKey],
  )

  return { handleInteractiveToggle, handleTerminalExit, handleToggleRichView, handleCancelQueued, handleEditQueued }
}
