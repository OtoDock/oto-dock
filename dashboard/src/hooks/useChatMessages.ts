import { useState, useRef, useCallback } from 'react'
import type { MutableRefObject } from 'react'
import type { DisplayMessage, MessageBlock } from '../components/chat/types'
import { dbMessagesToDisplay, withPlanRows } from '../lib/messageBlocks'
import { fetchOlderMessages } from '../api/chats'
import { WIRE, type DbMessageRow, type DbPlanRow } from '../api/wireEvents'

/** How a request for an older page settled: rows `landed`; `nothing` came
 *  (an empty page, a failure, no history to page); or the request is
 *  `stale` (the rows were reset, the chat switched or a history waits for
 *  its live state: the next window of rows pages again). */
export type OlderPage = 'landed' | 'nothing' | 'stale'

/**
 * Owns the chat message list, the lazy chat-history pagination state, and the
 * message-assembly helpers (split from useChatStream).
 *
 * `currentMsgRef` is created HERE and returned so useChatStream shares the same
 * ref object. It names the streaming bubble by the message as it was MINTED:
 * every updater below finds that bubble by its id in the list it is given and
 * never writes the ref, so an updater is pure and gives the same answer each
 * time React runs it (StrictMode runs each one twice; an updater that moved the
 * ref while it ran made the second run keep the old list, and the next chunk
 * was lost). A message's id and identity fields never change after its mint,
 * so the ref's object is as good as the newest one for them; whoever needs the
 * bubble's blocks reads the committed list (`messagesRef` in useChatStream).
 * `meetingSpeakerRef` and `chatIdRef` are created in the main hook (also read
 * by handlers that stay there) and passed in. The pagination internals
 * (rawRowsRef / oldestLoadedIdRef / loadingOlderRef / setHasMoreOlder /
 * setLoadingOlder) are returned because onChatHistory — which stays in the
 * main hook — resets them.
 */
export function useChatMessages(args: {
  agents: Array<{ name: string; display_name?: string; color?: string }> | undefined
  meetingSpeakerRef: MutableRefObject<{ slug: string; displayName: string; color: string } | null>
  chatIdRef: MutableRefObject<string | null>
}) {
  const { agents, meetingSpeakerRef, chatIdRef } = args

  const [messages, setMessages] = useState<DisplayMessage[]>([])
  // Lazy chat-history pagination: chat_history delivers the newest page; older
  // turns load on scroll-up. `rawRowsRef` holds ALL loaded DB rows (the source of
  // truth) so dbMessagesToDisplay can be re-run over the FULL merged set — it has
  // cross-row state (turn grouping, delegate pairing, dedup) that breaks if pages
  // are converted in isolation and concatenated.
  const rawRowsRef = useRef<DbMessageRow[]>([])
  // The chat's plan rows, which answer the plan reviews the rows saved
  // unanswered (withPlanRows): every rebuild of the rows reads them.
  const planRowsRef = useRef<DbPlanRow[]>([])
  const oldestLoadedIdRef = useRef<number | null>(null)
  const loadingOlderRef = useRef(false)
  // The window the cursor belongs to: the chat whose rows it pages and a
  // count every reset of the rows bumps (`resetWindow`). A page asked for in
  // another window (a chat switch, a history frame sent again meanwhile)
  // would prepend rows before an id of another window.
  const windowRef = useRef<{ chatId: string | null; epoch: number }>({ chatId: null, epoch: 0 })
  const [hasMoreOlder, setHasMoreOlder] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const currentMsgRef = useRef<DisplayMessage | null>(null)
  // The nudge of a background review the chat's socket took between turns
  // (its row is stored, then the frame, then the review turn starts): the
  // turn's start mints its bubble with the nudge's line, as the stored row
  // draws it. Dropped at every turn end, result and history frame.
  const reviewNudgeRef = useRef<{ subtype: string; count?: number } | null>(null)
  // A history frame's rows held for the live state that follows them
  // (useChatHistory): their paint would drop a page loaded meanwhile.
  const historyHeldRef = useRef(false)

  /** A new window of rows (a history frame, a seed): the cursor is this
   *  chat's from here, and a page still loading for the last one is void. */
  const resetWindow = useCallback((chatId?: string | null) => {
    windowRef.current = { chatId: chatId ?? chatIdRef.current, epoch: windowRef.current.epoch + 1 }
  }, [chatIdRef])

  /** The streaming bubble, when it is the last message of `prev`. */
  const streamTail = (prev: DisplayMessage[]): DisplayMessage | null => {
    const last = prev[prev.length - 1]
    const id = currentMsgRef.current?.id
    return last && id !== undefined && last.role === 'assistant' && last.id === id ? last : null
  }

  // --- Message assembly helpers ---

  const appendBlock = useCallback((block: MessageBlock) => {
    setMessages((prev) => {
      const last = streamTail(prev)
      if (!last) return prev
      return [...prev.slice(0, -1), { ...last, blocks: [...last.blocks, block] }]
    })
  }, [])

  // Seed the rendered view + pagination state from a REST snapshot (the rich-view
  // toggle) — the same paged contract as onChatHistory, so scroll-back lazy-loads
  // afterwards and nothing is capped.
  const seedDbHistory = useCallback((rows: DbMessageRow[], hasMore: boolean) => {
    resetWindow()
    rawRowsRef.current = rows
    oldestLoadedIdRef.current = rows.length ? rows[0].id : null
    loadingOlderRef.current = false
    setLoadingOlder(false)
    setHasMoreOlder(hasMore)
    setMessages(withPlanRows(dbMessagesToDisplay(rows, agents), planRowsRef.current))
  }, [agents, resetWindow])

  // Lazy scroll-back: fetch the next older page and re-render the DB portion from
  // the full merged raw rows, preserving the live/optimistic tail (non-`db-` ids).
  const loadOlder = useCallback(async (): Promise<OlderPage> => {
    const cid = chatIdRef.current
    const beforeId = oldestLoadedIdRef.current
    const asked = windowRef.current
    if (!cid || beforeId == null) return 'nothing'
    if (loadingOlderRef.current || asked.chatId !== cid || historyHeldRef.current) return 'stale'
    loadingOlderRef.current = true
    setLoadingOlder(true)
    try {
      const { messages: older, has_more } = await fetchOlderMessages(cid, beforeId)
      // Switched chats, or the rows were reset, mid-fetch.
      if (chatIdRef.current !== cid || windowRef.current !== asked) return 'stale'
      if (older.length) {
        rawRowsRef.current = [...older, ...rawRowsRef.current]
        oldestLoadedIdRef.current = rawRowsRef.current[0]?.id ?? beforeId
        setMessages((prev) => {
          const tail = prev.filter((m) => !m.id.startsWith('db-'))
          return [...withPlanRows(dbMessagesToDisplay(rawRowsRef.current, agents), planRowsRef.current), ...tail]
        })
      }
      setHasMoreOlder(has_more)
      return older.length > 0 ? 'landed' : 'nothing'
    } catch {
      // transient — leave hasMoreOlder set so a later scroll retries
      return 'nothing'
    } finally {
      // A reset meanwhile cleared the flag itself (and a page of its own
      // may be loading now).
      if (windowRef.current === asked) {
        loadingOlderRef.current = false
        setLoadingOlder(false)
      }
    }
  }, [agents, chatIdRef])

  // A document push's card: appended to the streaming bubble, or in place
  // of the card of the same file already there (a reconnect's live state
  // may have drawn it before the turn's flush delivered the frame).
  const upsertPreviewBlock = useCallback((block: Extract<MessageBlock, { type: 'document_preview' }>) => {
    setMessages((prev) => {
      const last = streamTail(prev)
      if (!last) return prev
      const at = last.blocks.findIndex((b) => b.type === 'document_preview' && b.fileId === block.fileId)
      const blocks = at >= 0
        ? last.blocks.map((b, i) => (i === at ? block : b))
        : [...last.blocks, block]
      return [...prev.slice(0, -1), { ...last, blocks }]
    })
  }, [])

  // Drop every transient placeholder of `kind` from the streaming bubble
  // (a transcode skeleton once the player or the failure arrives, a
  // generation placeholder once the generation fails); the kind comes from
  // the artifact table's `evicts` (lib/kinds/artifact.ts), null = nothing.
  const removePlaceholders = useCallback((kind: string | null) => {
    if (!kind) return
    setMessages((prev) => {
      const last = prev[prev.length - 1]
      if (!last || last.role !== 'assistant') return prev
      const blocks = last.blocks.filter((b) => b.type !== kind)
      if (blocks.length === last.blocks.length) return prev
      return [...prev.slice(0, -1), { ...last, blocks }]
    })
  }, [])

  // A progress note and the answer never share a block: the text joins the
  // last block only when it is text of the same kind. The note mark (a note
  // with no content: the text just streamed came right before a tool call)
  // turns the last block into a note when it is plain text, else nothing.
  const appendToLastTextBlock = useCallback((content: string, note = false) => {
    setMessages((prev) => {
      const last = streamTail(prev)
      if (!last) return prev
      const blocks = [...last.blocks]
      const lastBlock = blocks[blocks.length - 1]
      if (!content && note) {
        if (lastBlock?.type !== WIRE.TEXT || lastBlock.note) return prev
        blocks[blocks.length - 1] = { ...lastBlock, note: true }
      } else if (lastBlock && lastBlock.type === 'text' && !!lastBlock.note === note) {
        blocks[blocks.length - 1] = { ...lastBlock, content: lastBlock.content + content }
      } else {
        blocks.push(note ? { type: 'text', content, note: true } : { type: 'text', content })
      }
      return [...prev.slice(0, -1), { ...last, blocks }]
    })
  }, [])

  const updateToolBlock = useCallback(
    (toolId: string, updates: Partial<{ summary: string; status: 'running' | 'done' }>) => {
      setMessages((prev) => {
        // Search all messages (not just last) for the matching tool block
        let found = false
        const updated = prev.map((msg) => {
          if (found || msg.role !== 'assistant') return msg
          const hasMatch = msg.blocks.some((b) => b.type === 'tool' && b.toolId === toolId)
          if (!hasMatch) return msg
          found = true
          const blocks = msg.blocks.map((b) =>
            b.type === 'tool' && b.toolId === toolId ? { ...b, ...updates } : b,
          )
          return { ...msg, blocks }
        })
        return found ? updated : prev
      })
    },
    [],
  )

  // Update tool block by name — find most recent running tool with matching name
  const updateToolBlockByName = useCallback(
    (name: string, updates: Partial<{ summary: string; status: 'running' | 'done'; toolInput: unknown; toolResult: string; resultSummary: string }>) => {
      setMessages((prev) => {
        // Search backwards through all messages for the most recent running tool
        let found = false
        const reversed = [...prev].reverse()
        let targetMsgIdx = -1
        let targetBlockIdx = -1

        for (let mi = 0; mi < reversed.length && !found; mi++) {
          const msg = reversed[mi]
          if (msg.role !== 'assistant') continue
          for (let bi = msg.blocks.length - 1; bi >= 0; bi--) {
            const b = msg.blocks[bi]
            if (b.type === 'tool' && b.name === name && b.status === 'running') {
              targetMsgIdx = prev.length - 1 - mi
              targetBlockIdx = bi
              found = true
              break
            }
          }
        }

        if (!found) return prev

        return prev.map((msg, mi) => {
          if (mi !== targetMsgIdx) return msg
          const blocks = msg.blocks.map((b, bi) =>
            bi === targetBlockIdx ? { ...b, ...updates } as MessageBlock : b,
          )
          return { ...msg, blocks }
        })
      })
    },
    [],
  )

  const resolvePermission = useCallback((requestId: string, approved: boolean) => {
    setMessages((prev) =>
      prev.map((msg) => {
        const hasMatch = msg.blocks.some(
          (b) => b.type === 'permission' && b.requestId === requestId,
        )
        if (!hasMatch) return msg // keep same reference
        return {
          ...msg,
          blocks: msg.blocks.map((b) =>
            b.type === 'permission' && b.requestId === requestId
              ? { ...b, resolved: true, approved }
              : b,
          ),
        }
      }),
    )
  }, [])

  // Update subagent blocks' active state — search ALL assistant messages
  const updateSubagentActive = useCallback((toolId: string, isActive: boolean) => {
    setMessages((prev) => {
      let found = false
      const updated = prev.map((msg) => {
        if (found || msg.role !== 'assistant') return msg
        const hasMatch = msg.blocks.some(
          (b) => b.type === 'subagent' && b._toolId === toolId,
        )
        if (!hasMatch) return msg
        found = true
        const blocks = msg.blocks.map((b) =>
          b.type === 'subagent' && b._toolId === toolId
            ? { ...b, isActive }
            : b,
        )
        return { ...msg, blocks }
      })
      return found ? updated : prev
    })
  }, [])

  // Update bg-command blocks' active state by tool_use_id (mirror of
  // updateSubagentActive) — the badge + inline block derive from this flag.
  const updateCommandActive = useCallback((toolId: string, isActive: boolean) => {
    setMessages((prev) => {
      let found = false
      const updated = prev.map((msg) => {
        if (found || msg.role !== 'assistant') return msg
        const hasMatch = msg.blocks.some(
          (b) => b.type === 'bgcommand' && b._toolId === toolId,
        )
        if (!hasMatch) return msg
        found = true
        const blocks = msg.blocks.map((b) =>
          b.type === 'bgcommand' && b._toolId === toolId
            ? { ...b, isActive }
            : b,
        )
        return { ...msg, blocks }
      })
      return found ? updated : prev
    })
  }, [])

  function ensureAssistantMsg() {
    if (!currentMsgRef.current) {
      const speaker = meetingSpeakerRef.current
      const msg: DisplayMessage = {
        id: `stream-${Date.now()}`,
        role: 'assistant',
        blocks: [],
        createdAt: new Date().toISOString(),
        ...(speaker ? {
          agentSlug: speaker.slug,
          agentDisplayName: speaker.displayName,
          agentColor: speaker.color,
          badge: 'meeting',
        } : {}),
      }
      currentMsgRef.current = msg
      setMessages((prev) => [...prev, msg])
    }
  }

  return {
    messages, setMessages, currentMsgRef, reviewNudgeRef, historyHeldRef,
    rawRowsRef, planRowsRef, oldestLoadedIdRef, loadingOlderRef, resetWindow,
    hasMoreOlder, loadingOlder, setHasMoreOlder, setLoadingOlder,
    seedDbHistory, loadOlder,
    appendBlock, removePlaceholders, appendToLastTextBlock,
    updateToolBlock, updateToolBlockByName, resolvePermission,
    updateSubagentActive, updateCommandActive, ensureAssistantMsg,
    upsertPreviewBlock,
  }
}
