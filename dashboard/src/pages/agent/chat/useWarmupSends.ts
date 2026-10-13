/**
 * Sends typed while a chat gets ready ("Getting ready…"): every one is kept,
 * in order. Once the chat has its id (`warmup_started`), a send goes out at
 * once without starting a turn of its own (`queueMessage`): the proxy queues
 * it behind the chat's first message and its chip comes back as `queued`, so
 * it reaches the engine after that first message, as the next turn. Before
 * the id is known (a round trip on a new chat) the send is held here as a
 * local chip and flushed in order, with its queue id, when the id arrives.
 * A failed or stopped warmup hands what is still held back to the composer;
 * a chat switch in that window drops it, as the single slot this replaces
 * did.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { mintQueueId, type WireImage } from '../../../hooks/useDashboardWs'
import type { QueuedMessage } from '../../../store/types'

// `folder` rides along so a handed-back folder chip stays a folder.
type WireFile = { path: string; name: string; folder?: boolean }

export interface HeldSend {
  queueId: string
  text: string
  images?: WireImage[]
  files?: WireFile[]
}

interface QueueingSocket {
  connected: boolean
  queueMessage: (text: string, chatId: string, images?: WireImage[],
                 files?: WireFile[], queueId?: string) => string
}

export function useWarmupSends(
  ws: QueueingSocket, chatId: string | null, onHandBack: (held: HeldSend[]) => void,
) {
  const heldRef = useRef<HeldSend[]>([])
  const [held, setHeld] = useState<HeldSend[]>([])
  const handBackRef = useRef(onHandBack)
  handBackRef.current = onHandBack

  const take = useCallback((): HeldSend[] => {
    const items = heldRef.current
    heldRef.current = []
    setHeld([])
    return items
  }, [])

  /** A send during the warmup: out now when the chat has its id, else held. */
  const send = useCallback((text: string, images?: WireImage[], files?: WireFile[]) => {
    if (chatId && ws.connected && heldRef.current.length === 0) {
      ws.queueMessage(text, chatId, images, files)
      return
    }
    heldRef.current = [...heldRef.current, { queueId: mintQueueId(), text, images, files }]
    setHeld(heldRef.current)
  }, [ws, chatId])

  // The chat's id arrived (or the socket came back): the held sends go out
  // in order, each with the id its chip showed.
  useEffect(() => {
    if (!chatId || !ws.connected || heldRef.current.length === 0) return
    for (const it of take()) ws.queueMessage(it.text, chatId, it.images, it.files, it.queueId)
  }, [chatId, ws, ws.connected, take])

  /** The warmup failed or was stopped: the held sends go back to the composer. */
  const handBack = useCallback(() => {
    const items = take()
    if (items.length) handBackRef.current(items)
  }, [take])

  /** The page left the chat before its id arrived. */
  const discard = useCallback(() => { take() }, [take])

  const chips: QueuedMessage[] = useMemo(() => held.map((h) => ({
    text: h.text,
    queueId: h.queueId,
    images: h.images?.map((i) => ({ name: i.name, path: i.path })),
    files: h.files,
  })), [held])

  return { send, handBack, discard, chips }
}
