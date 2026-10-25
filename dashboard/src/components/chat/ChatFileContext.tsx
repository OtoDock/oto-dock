import { useMemo, type ReactNode } from 'react'
import { ChatFileContext } from '../../lib/chat/chatFileContext'

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
