// The chat a markdown file chip resolves against (ChatFileProvider supplies
// it around the message list).

import { createContext, useContext } from 'react'

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

export function useChatFileContext(): ChatFileContextValue | null {
  return useContext(ChatFileContext)
}
