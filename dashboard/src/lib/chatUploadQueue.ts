// Sequential upload queue for CHAT attachments — a module-level singleton.
//
// Why not component state or a per-call loop: chat uploads are eager-on-pick
// and can outlive the page that started them (chat navigation unmounts
// AgentChat; a warmup re-keys the draft slice under transferNewChatToChat),
// and a per-call loop would run one chain per pick — adding 3×300MB files in
// one pick (or across picks/chats) must serialize on ONE wire chain, which is
// exactly the failure mode this replaces (three parallel fetches with no
// progress).
//
// State writes go through the chat slice that CURRENTLY holds the file id —
// resolved at write time, so the draft→chat re-key never strands a chip.

import { useChatStore } from '../store/chatStore'
import { useTransferStore } from '../store/transferStore'
import type { PendingFile } from '../store/types'

import { canRead, UNREADABLE_ITEM, type CollectedFile } from './droppedItems'
import { queryClient } from './queryClient'
import { reserveUploadFolder, uploadWithProgress } from './uploadWithProgress'

/** The workspace tree shows what landed at once; the server's own
 *  `file_updated` frame covers the other tabs and a teammate's view. */
function refreshTree(agent: string): void {
  queryClient.invalidateQueries({ queryKey: ['agent-files', agent] })
}

/** A folder's files behind one chip. A retry carries the folder reserved
 * before (`path`) and how many files landed in it (`done`). */
export interface ChatUploadFolder {
  name: string
  files: CollectedFile[]
  path?: string
  done?: number
}

export interface ChatUploadEntry {
  fileId: string
  /** One file, or (`folder`) a folder's files behind one chip. */
  file?: File
  folder?: ChatUploadFolder
  agent: string
  abort: AbortController
}

const queue: ChatUploadEntry[] = []
let running = false

// Store writes for a many-minutes 300MB upload would otherwise re-render the
// whole chat tree per XHR tick — cap chip updates to whole-percent steps at
// most every PROGRESS_MIN_INTERVAL_MS.
const PROGRESS_MIN_INTERVAL_MS = 250

/** The chat slice currently holding this pending file (null once removed). */
function locateChatId(fileId: string): string | null {
  const byChat = useChatStore.getState().byChat
  for (const [chatId, slice] of Object.entries(byChat)) {
    if (slice.pendingFiles.some((f) => f.id === fileId)) return chatId
  }
  return null
}

function patchFile(fileId: string, patch: Partial<PendingFile>): void {
  const chatId = locateChatId(fileId)
  if (chatId) useChatStore.getState().updatePendingFile(chatId, fileId, patch)
}

/** Patch a pending file in the chat slice that holds it now (a no-op once
 * it is removed): the composer's folder intake uses it while a walk runs. */
export { patchFile as patchPendingFile }

// transferStore.prune() normally runs from the workspace TransferPopup's
// open-while interval; chat-originated transfers must not depend on that
// popup ever opening, or a finished upload pins the toolbar's "active
// transfers" badge forever. Tick while anything is tracked, stop when idle.
let pruneTimer: ReturnType<typeof setInterval> | null = null
function ensurePruneTick(): void {
  if (pruneTimer != null) return
  pruneTimer = setInterval(() => {
    const ts = useTransferStore.getState()
    ts.prune()
    if (
      Object.keys(useTransferStore.getState().byId).length === 0 &&
      queue.length === 0 && !running && pruneTimer != null
    ) {
      clearInterval(pruneTimer)
      pruneTimer = null
    }
  }, 5000)
}

/** Add a file to the global chain (uploads start in pick order, one at a time). */
export function enqueueChatUpload(entry: ChatUploadEntry): void {
  queue.push(entry)
  ensurePruneTick()
  void pump()
}

/** Drop a still-queued entry (removal of an in-flight one aborts instead). */
export function dequeueChatUpload(fileId: string): boolean {
  const idx = queue.findIndex((e) => e.fileId === fileId)
  if (idx < 0) return false
  queue.splice(idx, 1)
  return true
}

/** Test hook: pending entry count (the running one is not pending). */
export function chatUploadQueueSize(): number {
  return queue.length
}

/** An upload is running or waiting: work a page reload would destroy. */
export function chatUploadActive(): boolean {
  return running || queue.length > 0
}

async function pump(): Promise<void> {
  if (running) return
  running = true
  try {
    while (queue.length > 0) {
      const entry = queue.shift()!
      if (entry.abort.signal.aborted) continue
      if (!locateChatId(entry.fileId)) continue // removed while queued
      if (entry.folder) await uploadFolder(entry, entry.folder)
      else if (entry.file) await uploadOne(entry, entry.file)
    }
  } finally {
    running = false
  }
}

async function uploadOne(entry: ChatUploadEntry, file: File): Promise<void> {
  const ts = useTransferStore.getState()
  const clientId = `local:chat-${entry.fileId}`
  patchFile(entry.fileId, {
    queued: false, uploading: true,
    uploadSent: 0, uploadTotal: file.size,
  })
  // Chat uploads land in the default target (uploads/files) — targetDir ''.
  // Pre-link the relPath is the bare filename → no workspace section, which
  // is fine: the chip is the phase-1 surface; linkUpload swaps in the server
  // path and the workspace popup picks it up for phase 2.
  ts.beginLocalUpload({
    clientId, agent: entry.agent, targetDir: '',
    filename: file.name, bytesTotal: file.size,
  })

  let lastPct = -1
  let lastWrite = 0
  try {
    const resp = await uploadWithProgress(
      file, entry.agent, '',
      (sent, total) => {
        useTransferStore.getState().updateLocalUpload(clientId, sent)
        const pctNow = total > 0 ? Math.floor((sent / total) * 100) : 0
        const now = Date.now()
        if (pctNow !== lastPct && now - lastWrite >= PROGRESS_MIN_INTERVAL_MS) {
          lastPct = pctNow
          lastWrite = now
          patchFile(entry.fileId, { uploadSent: sent, uploadTotal: total })
        }
      },
      entry.abort.signal,
    )
    patchFile(entry.fileId, {
      uploading: false, queued: false,
      uploadedPath: resp.path,
      name: resp.filename || file.name,
      uploadSent: resp.size, uploadTotal: resp.size,
      transferId: resp.transfer_id, remotePush: resp.remote_push,
    })
    useTransferStore.getState().linkUpload(clientId, {
      transferId: resp.transfer_id,
      relPath: resp.path,
      remotePush: resp.remote_push,
    })
    refreshTree(entry.agent)
  } catch (e: any) {
    useTransferStore.getState().failLocalUpload(clientId)
    if (e?.name === 'AbortError') return // user removed the file — chip is gone
    patchFile(entry.fileId, {
      uploading: false, queued: false,
      error: e?.message || 'Upload failed',
    })
  }
}

/** A folder behind one chip: a fresh folder reserved in the chat's upload
 * folder (or the one a retry carries), then its files one after another
 * into their places in it, from the first not landed yet. The chip's
 * progress is the folder's bytes; the first file that fails fails the chip
 * and keeps the reservation and the count, so a Retry resumes there (the
 * files already up stay in the workspace, as they do when the chip is
 * removed). */
async function uploadFolder(entry: ChatUploadEntry, folder: ChatUploadFolder): Promise<void> {
  const total = folder.files.reduce((a, f) => a + f.file.size, 0)
  let index = folder.done ?? 0
  let done = folder.files.slice(0, index).reduce((a, f) => a + f.file.size, 0)
  patchFile(entry.fileId, {
    queued: false, uploading: true, uploadSent: done, uploadTotal: total,
  })
  let current = ''
  let lastWrite = 0
  // The tree refreshes once the folder exists on the server (empty or not)
  // and again when the last file lands or the chip fails after some did.
  let landed = false
  try {
    let path = folder.path
    if (!path) {
      const reserved = await reserveUploadFolder(entry.agent, folder.name, entry.abort.signal)
      path = reserved.path
      patchFile(entry.fileId, { folderPath: path, name: reserved.name })
      landed = true
    }
    for (; index < folder.files.length; index++) {
      const { file, relDir } = folder.files[index]
      current = relDir ? `${relDir}/${file.name}` : file.name
      if (!(await canRead(file))) throw new Error(UNREADABLE_ITEM)
      await uploadWithProgress(
        file, entry.agent, relDir ? `${path}/${relDir}` : path,
        (sent) => {
          const now = Date.now()
          if (now - lastWrite >= PROGRESS_MIN_INTERVAL_MS) {
            lastWrite = now
            patchFile(entry.fileId, { uploadSent: done + sent, uploadTotal: total })
          }
        },
        entry.abort.signal,
      )
      done += file.size
      landed = true
      patchFile(entry.fileId, { filesDone: index + 1 })
    }
    patchFile(entry.fileId, {
      uploading: false, queued: false,
      uploadedPath: path,
      uploadSent: total, uploadTotal: total,
    })
  } catch (e: unknown) {
    if (e instanceof DOMException && e.name === 'AbortError') return // user removed the chip
    const why = (e instanceof Error && e.message) || 'Upload failed'
    patchFile(entry.fileId, {
      uploading: false, queued: false,
      error: current ? `${current}: ${why}` : why,
    })
  } finally {
    if (landed) refreshTree(entry.agent)
  }
}
