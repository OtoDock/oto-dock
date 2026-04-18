// Per-chat lifecycle state, keyed by chat_id. Survives chat navigation so
// a warmup on chat A keeps progressing in the sidebar badge while the user
// reads chat B. Also tracks which chats are streaming so the WS reconnect
// handler can send resume_chat for each instead of only the displayed one.
//
// Scope: chat-level lifecycle (status) + minimal session metadata. MCP
// install progress lives in installStore — keyed by (machine_id, agent)
// because installs are shared across chats. See store/installStore.ts.

import { create } from 'zustand'
import { persist, createJSONStorage, type StateStorage } from 'zustand/middleware'

import type { PendingImage, PendingFile } from './types'

export type ChatStreamPhase = 'idle' | 'warming' | 'ready' | 'streaming' | 'failed'

export interface ChatSlice {
  chatId: string
  status: ChatStreamPhase
  agent: string
  executionPath: string
  executionTarget: string
  fallbackReason: string | null
  warmupStartedAt: number | null
  warmupError: string | null
  lastEventAt: number
  // Persisted (localStorage via partialize). Draft text the user
  // typed but hasn't sent. Survives chat-to-chat nav, browser reload,
  // app background. Cleared on successful send.
  draftInput: string
  // Messages the user typed during streaming that the backend queued
  // for the next turn. Mirrors `pump.message_queue` on the proxy via
  // onQueued / onQueueRemoved / onQueueSent deltas + a queue_snapshot
  // emitted by the backend on resume_chat (reconciles against any
  // localStorage drift). Persisted across reloads.
  queuedMessages: string[]
  // Pending attachments — in-memory only. Survive chat-to-chat nav (the
  // per-chat slice is keyed by chat_id) but NOT a full reload (base64
  // images are large; PendingFile carries non-serializable File +
  // AbortController references).
  pendingImages: PendingImage[]
  pendingFiles: PendingFile[]
  // Sidebar unread indicator. Tri-state: undefined = defer to the chats
  // API row's `unread` (server truth), true/false = a live WS flip
  // (chat_status ready on a background chat / chat_read echo) that wins
  // until the next list refetch confirms it.
  unread?: boolean
}

// New-chat draft slice key. Real chat_ids are UUIDs so this prefix can't
// collide. Used for drafts on the new-chat page where chat_id hasn't been
// minted yet; transferred to the real chat_id when warmup_started arrives.
export const NEW_CHAT_PREFIX = '__new__:'
export const newChatKey = (agentSlug: string): string =>
  `${NEW_CHAT_PREFIX}${agentSlug}`

interface ChatStoreState {
  byChat: Record<string, ChatSlice>

  // ─── mutators (called from WS event dispatcher + UI components) ─────────
  beginWarmup: (
    chatId: string,
    data: { agent: string; execution_path?: string; execution_target?: string },
  ) => void
  touchHeartbeat: (chatId: string) => void
  finishWarmup: (
    chatId: string,
    data: {
      execution_path?: string
      execution_target?: string
      fallback_reason?: string | null
      // Interactive re-attach only: the session's live turn state at
      // warmup_ready — true forces 'streaming', false forces 'ready',
      // absent (headless / fresh spawn) keeps the no-downgrade guard.
      turn_open?: boolean
    },
  ) => void
  failWarmup: (chatId: string, error: string) => void
  setStreaming: (chatId: string) => void
  setReady: (chatId: string) => void
  setUnread: (chatId: string, unread: boolean) => void
  setDraftInput: (chatId: string, text: string) => void
  clearDraft: (chatId: string) => void
  // Queue mutators — mirror backend pump deltas.
  setQueuedMessages: (chatId: string, messages: string[]) => void
  addQueuedMessage: (chatId: string, index: number, text: string) => void
  removeQueuedMessageByIndex: (chatId: string, index: number) => void
  clearQueuedMessages: (chatId: string) => void
  // Pending image mutators.
  setPendingImages: (chatId: string, images: PendingImage[]) => void
  addPendingImages: (chatId: string, images: PendingImage[]) => void
  removePendingImage: (chatId: string, id: string) => void
  // Pending file mutators — `updatePendingFile` is the lifecycle setter
  // for the upload-completion path (`uploading: false` + `uploadedPath` /
  // `error` patch on the matching entry).
  setPendingFiles: (chatId: string, files: PendingFile[]) => void
  addPendingFiles: (chatId: string, files: PendingFile[]) => void
  removePendingFile: (chatId: string, id: string) => void
  updatePendingFile: (chatId: string, id: string, patch: Partial<PendingFile>) => void
  // Migrate the new-chat draft to the freshly-minted chat_id. Called from
  // the WS dispatcher when warmup_started arrives — must fire BEFORE
  // beginWarmup so the warmup mutator's MERGE preserves the transferred
  // draftInput / queuedMessages / pending attachments rather than
  // overwriting an empty slice.
  transferNewChatToChat: (agentSlug: string, newChatId: string) => void
  clear: (chatId: string) => void
}

const _emptySlice = (chatId: string): ChatSlice => ({
  chatId,
  status: 'idle',
  agent: '',
  executionPath: '',
  executionTarget: '',
  fallbackReason: null,
  warmupStartedAt: null,
  warmupError: null,
  lastEventAt: 0,
  draftInput: '',
  queuedMessages: [],
  pendingImages: [],
  pendingFiles: [],
})

// 300ms-debounced wrapper around localStorage. The persist middleware writes
// synchronously on every state mutation — without debouncing, each textarea
// keystroke triggers a JSON.stringify + localStorage.setItem (~1KB) which
// adds latency on slower devices. Risk: a 300ms window where a tab crash
// loses the latest keystroke. Acceptable for draft text.
function debouncedLocalStorage(): StateStorage {
  let pending: ReturnType<typeof setTimeout> | null = null
  let queued: Record<string, string> = {}
  return {
    getItem: (key) => localStorage.getItem(key),
    setItem: (key, value) => {
      queued[key] = value
      if (pending) clearTimeout(pending)
      pending = setTimeout(() => {
        for (const [k, v] of Object.entries(queued)) localStorage.setItem(k, v)
        queued = {}
        pending = null
      }, 300)
    },
    removeItem: (key) => {
      delete queued[key]
      localStorage.removeItem(key)
    },
  }
}

export const useChatStore = create<ChatStoreState>()(persist((set) => ({
  byChat: {},

  beginWarmup: (chatId, data) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      // Merge — preserves any fields the new-chat-to-chat transfer wrote
      // before warmup_started fired (notably draftInput). The previous
      // implementation replaced the slice with _emptySlice + warming, which
      // dropped transferred drafts.
      // Never downgrade 'streaming': visiting a MID-TURN interactive chat
      // re-fires warmup_started (idempotent re-warm) — clobbering to
      // 'warming' here killed the sidebar live-dot for good (broadcasts are
      // transition-only, so nothing re-lit it). finishWarmup has the same
      // guard; warmup_ready's turn_open field is the authoritative reset.
      return {
        byChat: {
          ...s.byChat,
          [chatId]: {
            ...prev,
            chatId,
            status: prev.status === 'streaming' ? 'streaming' : 'warming',
            agent: data.agent,
            executionPath: data.execution_path ?? prev.executionPath,
            executionTarget: data.execution_target ?? prev.executionTarget,
            warmupStartedAt: Date.now(),
            lastEventAt: Date.now(),
          },
        },
      }
    }),

  touchHeartbeat: (chatId) =>
    set((s) => {
      const prev = s.byChat[chatId]
      if (!prev) return s
      return { byChat: { ...s.byChat, [chatId]: { ...prev, lastEventAt: Date.now() } } }
    }),

  finishWarmup: (chatId, data) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      return {
        byChat: {
          ...s.byChat,
          [chatId]: {
            ...prev,
            // Never DOWNGRADE a streaming slice: on the dead-session resend
            // path (abort → send again), sendMessage sets 'streaming', the
            // backend auto-rewarms and emits warmup_ready, THEN runs the
            // turn — an unconditional 'ready' here hid the stop button +
            // timer for the entire turn. warmup_ready means "session ready",
            // never "turn over"; only done/aborted/chat_status end a
            // streaming turn. EXCEPTION: an interactive re-attach carries the
            // session's live turn_open — that is server truth in both
            // directions (lights a dot this client never saw start, clears a
            // stale one).
            status:
              data.turn_open === true
                ? 'streaming'
                : data.turn_open === false
                  ? 'ready'
                  : prev.status === 'streaming'
                    ? 'streaming'
                    : 'ready',
            executionPath: data.execution_path ?? prev.executionPath,
            executionTarget: data.execution_target ?? prev.executionTarget,
            fallbackReason: data.fallback_reason ?? null,
            lastEventAt: Date.now(),
          },
        },
      }
    }),

  failWarmup: (chatId, error) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      return {
        byChat: {
          ...s.byChat,
          [chatId]: {
            ...prev,
            status: 'failed',
            warmupError: error,
            lastEventAt: Date.now(),
          },
        },
      }
    }),

  setStreaming: (chatId) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, status: 'streaming', lastEventAt: Date.now() } },
      }
    }),

  setReady: (chatId) =>
    set((s) => {
      const prev = s.byChat[chatId]
      if (!prev) return s
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, status: 'ready', lastEventAt: Date.now() } },
      }
    }),

  setUnread: (chatId, unread) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      if (prev.unread === unread) return s
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, unread } },
      }
    }),

  setDraftInput: (chatId, text) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      // Avoid creating an empty slice for an unchanged empty draft.
      if (!prev.draftInput && !text) return s
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, draftInput: text } },
      }
    }),

  clearDraft: (chatId) =>
    set((s) => {
      const prev = s.byChat[chatId]
      if (!prev || !prev.draftInput) return s
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, draftInput: '' } },
      }
    }),

  setQueuedMessages: (chatId, messages) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, queuedMessages: messages } },
      }
    }),

  addQueuedMessage: (chatId, index, text) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      // Backend sends a 0-based index; pad with empty strings if it arrives
      // out of order (rare but possible across WS reconnect).
      const next = prev.queuedMessages.slice()
      while (next.length <= index) next.push('')
      next[index] = text
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, queuedMessages: next } },
      }
    }),

  removeQueuedMessageByIndex: (chatId, index) =>
    set((s) => {
      const prev = s.byChat[chatId]
      if (!prev) return s
      const next = prev.queuedMessages.filter((_, i) => i !== index)
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, queuedMessages: next } },
      }
    }),

  clearQueuedMessages: (chatId) =>
    set((s) => {
      const prev = s.byChat[chatId]
      if (!prev || prev.queuedMessages.length === 0) return s
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, queuedMessages: [] } },
      }
    }),

  setPendingImages: (chatId, images) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      if (prev.pendingImages.length === 0 && images.length === 0) return s
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, pendingImages: images } },
      }
    }),

  addPendingImages: (chatId, images) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      return {
        byChat: {
          ...s.byChat,
          [chatId]: { ...prev, pendingImages: [...prev.pendingImages, ...images] },
        },
      }
    }),

  removePendingImage: (chatId, id) =>
    set((s) => {
      const prev = s.byChat[chatId]
      if (!prev) return s
      return {
        byChat: {
          ...s.byChat,
          [chatId]: { ...prev, pendingImages: prev.pendingImages.filter((i) => i.id !== id) },
        },
      }
    }),

  setPendingFiles: (chatId, files) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      if (prev.pendingFiles.length === 0 && files.length === 0) return s
      return {
        byChat: { ...s.byChat, [chatId]: { ...prev, pendingFiles: files } },
      }
    }),

  addPendingFiles: (chatId, files) =>
    set((s) => {
      const prev = s.byChat[chatId] ?? _emptySlice(chatId)
      return {
        byChat: {
          ...s.byChat,
          [chatId]: { ...prev, pendingFiles: [...prev.pendingFiles, ...files] },
        },
      }
    }),

  removePendingFile: (chatId, id) =>
    set((s) => {
      const prev = s.byChat[chatId]
      if (!prev) return s
      return {
        byChat: {
          ...s.byChat,
          [chatId]: { ...prev, pendingFiles: prev.pendingFiles.filter((f) => f.id !== id) },
        },
      }
    }),

  updatePendingFile: (chatId, id, patch) =>
    set((s) => {
      const prev = s.byChat[chatId]
      if (!prev) return s
      return {
        byChat: {
          ...s.byChat,
          [chatId]: {
            ...prev,
            pendingFiles: prev.pendingFiles.map((f) =>
              f.id === id ? { ...f, ...patch } : f,
            ),
          },
        },
      }
    }),

  transferNewChatToChat: (agentSlug, newChatId) =>
    set((s) => {
      const fromKey = newChatKey(agentSlug)
      const fromSlice = s.byChat[fromKey]
      if (!fromSlice) return s
      const carries =
        fromSlice.draftInput.length > 0 ||
        fromSlice.queuedMessages.length > 0 ||
        fromSlice.pendingImages.length > 0 ||
        fromSlice.pendingFiles.length > 0
      if (!carries) return s
      const existing = s.byChat[newChatId] ?? _emptySlice(newChatId)
      const { [fromKey]: _, ...rest } = s.byChat
      return {
        byChat: {
          ...rest,
          [newChatId]: {
            ...existing,
            draftInput: fromSlice.draftInput || existing.draftInput,
            queuedMessages: fromSlice.queuedMessages.length
              ? fromSlice.queuedMessages
              : existing.queuedMessages,
            pendingImages: fromSlice.pendingImages.length
              ? fromSlice.pendingImages
              : existing.pendingImages,
            pendingFiles: fromSlice.pendingFiles.length
              ? fromSlice.pendingFiles
              : existing.pendingFiles,
          },
        },
      }
    }),

  clear: (chatId) =>
    set((s) => {
      if (!(chatId in s.byChat)) return s
      const { [chatId]: _, ...rest } = s.byChat
      return { byChat: rest }
    }),
}), {
  name: 'oto-dock-chat-store',
  storage: createJSONStorage(debouncedLocalStorage),
  // Persist ONLY draftInput + queuedMessages (text-only, small). Live
  // session metadata is in-memory; pending attachments contain
  // non-serializable File / AbortController refs and base64 blobs too
  // big for localStorage. Backend resends a queue_snapshot on resume_chat
  // so any persisted queuedMessages drift gets reconciled within ~200ms
  // of reconnect (see proxy/ws/dashboard.py::_handle_resume_chat).
  partialize: (state) => ({
    byChat: Object.fromEntries(
      Object.entries(state.byChat)
        .filter(([_, slice]) =>
          (slice.draftInput && slice.draftInput.length > 0) ||
          slice.queuedMessages.length > 0,
        )
        .map(([chatId, slice]) => [
          chatId,
          {
            ..._emptySlice(chatId),
            draftInput: slice.draftInput,
            queuedMessages: slice.queuedMessages,
          },
        ]),
    ),
  }) as Partial<ChatStoreState>,
}))

// ─── selector hooks ───────────────────────────────────────────────────────

export const useChatSlice = (chatId: string | null | undefined): ChatSlice | undefined =>
  useChatStore((s) => (chatId ? s.byChat[chatId] : undefined))

// Non-hook accessor for WS dispatch callbacks.
export const getActiveChatIds = (): string[] => {
  const byChat = useChatStore.getState().byChat
  return Object.keys(byChat).filter((cid) => {
    const status = byChat[cid]?.status
    return status === 'warming' || status === 'streaming'
  })
}
