/**
 * ConversationView — lightweight viewer for agent conversations.
 *
 * Navigates the user to the regular chat page with the conversation's chatId.
 * The chat page handles all rendering (messages, tools, thinking, etc.).
 *
 * This component exists as a redirect from /conversations/:chatId to /chat/:agent/:chatId,
 * fetching the chat metadata first to resolve the agent name.
 */
import { useEffect } from 'react'
import { useParams, useNavigate, Navigate } from 'react-router-dom'
import { useChatDetail, ForbiddenError } from '../api/chats'

export default function ConversationView() {
  const { chatId } = useParams<{ chatId: string }>()
  const navigate = useNavigate()
  const { data: chat, isLoading, error } = useChatDetail(chatId || null)

  useEffect(() => {
    if (chat?.agent && chatId) {
      navigate(`/chat/${chat.agent}/${chatId}`, { replace: true })
    }
  }, [chat, chatId, navigate])

  if (error instanceof ForbiddenError) {
    return <Navigate to="/" replace />
  }

  if (error) {
    return (
      <div className="h-screen-safe flex items-center justify-center bg-p-bg">
        <div className="text-center">
          <p className="text-sm text-p-error mb-4">
            {error instanceof Error ? error.message : 'Conversation not found.'}
          </p>
          <button
            onClick={() => navigate(-1)}
            className="px-4 py-2 text-sm bg-brand text-white rounded-lg hover:bg-brand-hover"
          >
            Go back
          </button>
        </div>
      </div>
    )
  }

  if (isLoading || !chat) {
    return (
      <div className="h-screen-safe flex items-center justify-center bg-p-bg">
        <div className="text-p-text-light text-sm">Loading conversation...</div>
      </div>
    )
  }

  return null // Redirect happens via useEffect
}
