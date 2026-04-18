import { useState, useRef, useCallback } from 'react'
import type { MutableRefObject } from 'react'
import type { DisplayMessage, MessageBlock } from '../components/chat/types'
import { dbMessagesToDisplay } from '../lib/messageBlocks'
import { fetchOlderMessages } from '../api/chats'

/**
 * Owns the chat message list, the lazy chat-history pagination state, and the
 * message-assembly helpers. Extracted verbatim from useChatStream — every
 * helper keeps its original useCallback deps and byte-identical body.
 *
 * `currentMsgRef` is created HERE and returned so useChatStream shares the same
 * ref object. `meetingSpeakerRef` and `chatIdRef` are created in the main hook
 * (also read by handlers that stay there) and passed in. The pagination
 * internals (rawRowsRef / oldestLoadedIdRef / loadingOlderRef / setHasMoreOlder
 * / setLoadingOlder) are returned because onChatHistory — which stays in the
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
  const rawRowsRef = useRef<any[]>([])
  const oldestLoadedIdRef = useRef<number | null>(null)
  const loadingOlderRef = useRef(false)
  const [hasMoreOlder, setHasMoreOlder] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const currentMsgRef = useRef<DisplayMessage | null>(null)

  // --- Message assembly helpers ---

  const appendBlock = useCallback((block: MessageBlock) => {
    setMessages((prev) => {
      const last = prev[prev.length - 1]
      if (last && last.role === 'assistant' && last === currentMsgRef.current) {
        const updated = { ...last, blocks: [...last.blocks, block] }
        currentMsgRef.current = updated
        return [...prev.slice(0, -1), updated]
      }
      return prev
    })
  }, [])

  // Seed the rendered view + pagination state from a REST snapshot (the rich-view
  // toggle) — the same paged contract as onChatHistory, so scroll-back lazy-loads
  // afterwards and nothing is capped.
  const seedDbHistory = useCallback((rows: any[], hasMore: boolean) => {
    rawRowsRef.current = rows
    oldestLoadedIdRef.current = rows.length ? rows[0].id : null
    loadingOlderRef.current = false
    setLoadingOlder(false)
    setHasMoreOlder(hasMore)
    setMessages(dbMessagesToDisplay(rows, agents))
  }, [agents])

  // Lazy scroll-back: fetch the next older page and re-render the DB portion from
  // the full merged raw rows, preserving the live/optimistic tail (non-`db-` ids).
  const loadOlder = useCallback(async () => {
    const cid = chatIdRef.current
    const beforeId = oldestLoadedIdRef.current
    if (!cid || beforeId == null || loadingOlderRef.current) return
    loadingOlderRef.current = true
    setLoadingOlder(true)
    try {
      const { messages: older, has_more } = await fetchOlderMessages(cid, beforeId)
      if (chatIdRef.current !== cid) return  // switched chats mid-fetch
      if (older.length) {
        rawRowsRef.current = [...older, ...rawRowsRef.current]
        oldestLoadedIdRef.current = rawRowsRef.current[0]?.id ?? beforeId
        setMessages((prev) => {
          const tail = prev.filter((m) => !m.id.startsWith('db-'))
          return [...dbMessagesToDisplay(rawRowsRef.current, agents), ...tail]
        })
      }
      setHasMoreOlder(has_more)
    } catch {
      // transient — leave hasMoreOlder set so a later scroll retries
    } finally {
      loadingOlderRef.current = false
      setLoadingOlder(false)
    }
  }, [agents])

  // Drop the transient media_processing skeleton (transcode done/failed).
  const removeMediaProcessing = useCallback(() => {
    setMessages((prev) => {
      const last = prev[prev.length - 1]
      if (!last || last.role !== 'assistant') return prev
      const blocks = last.blocks.filter((b) => b.type !== 'media_processing')
      if (blocks.length === last.blocks.length) return prev
      const updated = { ...last, blocks }
      currentMsgRef.current = updated
      return [...prev.slice(0, -1), updated]
    })
  }, [])

  const appendToLastTextBlock = useCallback((content: string) => {
    setMessages((prev) => {
      const last = prev[prev.length - 1]
      if (last && last.role === 'assistant' && last === currentMsgRef.current) {
        const blocks = [...last.blocks]
        const lastBlock = blocks[blocks.length - 1]
        if (lastBlock && lastBlock.type === 'text') {
          blocks[blocks.length - 1] = { ...lastBlock, content: lastBlock.content + content }
        } else {
          blocks.push({ type: 'text', content })
        }
        const updated = { ...last, blocks }
        currentMsgRef.current = updated
        return [...prev.slice(0, -1), updated]
      }
      return prev
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
          const updatedMsg = { ...msg, blocks }
          if (msg === currentMsgRef.current) currentMsgRef.current = updatedMsg
          return updatedMsg
        })
        return found ? updated : prev
      })
    },
    [],
  )

  // Update tool block by name — find most recent running tool with matching name
  const updateToolBlockByName = useCallback(
    (name: string, updates: Partial<{ summary: string; status: 'running' | 'done'; toolInput: any; toolResult: string; resultSummary: string }>) => {
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
          const updatedMsg = { ...msg, blocks }
          if (msg === currentMsgRef.current) currentMsgRef.current = updatedMsg
          return updatedMsg
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
        const updated = {
          ...msg,
          blocks: msg.blocks.map((b) =>
            b.type === 'permission' && b.requestId === requestId
              ? { ...b, resolved: true, approved }
              : b,
          ),
        }
        // Keep currentMsgRef in sync so appendToLastTextBlock works
        if (msg === currentMsgRef.current) currentMsgRef.current = updated
        return updated
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
          (b) => b.type === 'subagent' && (b as any)._toolId === toolId,
        )
        if (!hasMatch) return msg
        found = true
        const blocks = msg.blocks.map((b) =>
          b.type === 'subagent' && (b as any)._toolId === toolId
            ? { ...b, isActive }
            : b,
        )
        const updatedMsg = { ...msg, blocks }
        if (msg === currentMsgRef.current) currentMsgRef.current = updatedMsg
        return updatedMsg
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
          (b) => b.type === 'bgcommand' && (b as any)._toolId === toolId,
        )
        if (!hasMatch) return msg
        found = true
        const blocks = msg.blocks.map((b) =>
          b.type === 'bgcommand' && (b as any)._toolId === toolId
            ? { ...b, isActive }
            : b,
        )
        const updatedMsg = { ...msg, blocks }
        if (msg === currentMsgRef.current) currentMsgRef.current = updatedMsg
        return updatedMsg
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
    messages, setMessages, currentMsgRef,
    rawRowsRef, oldestLoadedIdRef, loadingOlderRef,
    hasMoreOlder, loadingOlder, setHasMoreOlder, setLoadingOlder,
    seedDbHistory, loadOlder,
    appendBlock, removeMediaProcessing, appendToLastTextBlock,
    updateToolBlock, updateToolBlockByName, resolvePermission,
    updateSubagentActive, updateCommandActive, ensureAssistantMsg,
  }
}
