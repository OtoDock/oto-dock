import { createContext, useContext, useMemo, type ReactNode } from 'react'

/**
 * Chat identity for the clickable file-path chips in `MarkdownContent`.
 *
 * Chips resolve against `POST /v1/chats/{chatId}/resolve-path`, which is
 * chat-scoped (the chat's agent + execution target drive the resolution) —
 * so chips can only resolve when this context is provided. Every other
 * surface that renders `MarkdownContent` (meetings, previews, …) has no
 * provider and keeps today's inert chip markup.
 */
export interface ChatFileContextValue {
  chatId: string
  agent: string
}

export const ChatFileContext = createContext<ChatFileContextValue | null>(null)

/** Provider around the chat message list. Tolerates the new-chat mount
 * (no chat id yet) by providing `null` — chips stay inert until a real
 * chat exists to resolve against. */
export function ChatFileProvider({
  chatId,
  agent,
  children,
}: {
  chatId?: string
  agent?: string
  children: ReactNode
}) {
  const value = useMemo(
    () => (chatId && agent ? { chatId, agent } : null),
    [chatId, agent],
  )
  return <ChatFileContext.Provider value={value}>{children}</ChatFileContext.Provider>
}

export function useChatFileContext(): ChatFileContextValue | null {
  return useContext(ChatFileContext)
}
