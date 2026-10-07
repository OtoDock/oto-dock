import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './auth'

/** One version of a document in a chat: a push's pinned copy. */
export interface DocumentVersion {
  snapshot_id: string
  message_id: number
  /** The push's 1-based position among the file's pushes in the chat. */
  version: number
  /** The push time, epoch ms. */
  generation: number
  /** The person's messages up to the push (at least 1). */
  turn: number
  /** The copy still exists (the newest 30 per file are kept). */
  available: boolean
}

/** A document pushed into a chat, with its versions newest first. */
export interface ChatDocument {
  file_id: string
  filename: string
  download_url: string
  generation: number
  versions: DocumentVersion[]
}

export const chatDocumentsKey = (chatId: string) => ['chat-documents', chatId] as const

export async function fetchChatDocuments(chatId: string): Promise<ChatDocument[]> {
  const res = await apiFetch(`/v1/documents/chat-documents?chat_id=${encodeURIComponent(chatId)}`)
  if (!res.ok) throw new Error(`chat documents: ${res.status}`)
  const body = await res.json()
  return Array.isArray(body?.documents) ? body.documents : []
}

/** The documents pushed into a chat (`GET /v1/documents/chat-documents`):
 * the pane's tabs and Versions menu, the cards' numbers. */
export function useChatDocuments(chatId: string | null | undefined) {
  return useQuery({
    queryKey: chatDocumentsKey(chatId || ''),
    queryFn: () => fetchChatDocuments(chatId!),
    enabled: !!chatId,
    staleTime: 30_000,
  })
}
