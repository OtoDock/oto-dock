/**
 * useChatAttachments — AgentChat's eager file-upload handlers for the
 * composer's attachment chips: handleAddFiles / handleAddFolder /
 * handleRemoveFile / handleRetryFile over the global sequential upload
 * queue (lib/chatUploadQueue) and the chatStore pending-files slice.
 */
import { useCallback } from 'react'
import type { PendingFile } from '../../../components/chat/ChatInput'
import { fmtSize } from '../../../components/common/TransferBar'
import { useFlag } from '../../../hooks/useFlag'
import { enqueueChatUpload, dequeueChatUpload, patchPendingFile } from '../../../lib/chatUploadQueue'
import { FOLDER_UPLOAD_MAX_FILES, leftOutNote, type CollectedItems } from '../../../lib/droppedItems'
import { useChatStore } from '../../../store/chatStore'

/** Why a collected folder cannot be attached, or '' when it can. */
export function folderRefusal(got: CollectedItems, uploadCap: number): string {
  if (got.tooMany) return `More than ${FOLDER_UPLOAD_MAX_FILES} files: zip it, or attach part of it`
  if (got.files.length === 0) {
    if (got.unreadable.length) {
      const n = got.unreadable.length
      return `${n} item${n === 1 ? '' : 's'} can't be read: nothing to attach`
    }
    const left = leftOutNote(got)
    return left ? `Nothing to attach: ${left}` : 'An empty folder: nothing to attach'
  }
  const over = got.files.find((f) => f.file.size > uploadCap)
  if (over) {
    const rel = over.relDir ? `${over.relDir}/${over.file.name}` : over.file.name
    return `${rel}: over the ${fmtSize(uploadCap)} upload limit`
  }
  return ''
}

/** What a good folder chip notes: items it could not read, folders left out. */
export function folderNote(got: CollectedItems): string {
  const n = got.unreadable.length
  return [
    n ? `${n} item${n === 1 ? '' : 's'} can't be read` : '',
    leftOutNote(got),
  ].filter(Boolean).join(' · ')
}

export function useChatAttachments({ agentName, draftKey }: {
  agentName: string | undefined
  draftKey: string
}) {
  // The per-install upload cap, the same one the composer checks a file by.
  const uploadCap = useFlag('upload_max_bytes')
  // Files upload eagerly on pick (not on Send — send stays near-instant),
  // through the GLOBAL sequential queue (lib/chatUploadQueue): one wire chain
  // for any number of picked files, XHR byte progress on each chip, and
  // updates that survive chat navigation + the draft→chat re-key.
  const handleAddFiles = useCallback((files: PendingFile[]) => {
    if (!draftKey) return
    // Pre-errored entries (oversize — ChatInput tags them) render as error
    // chips only: no upload, no abort controller. Everything else queues as
    // uploading (queued files count as uploading so the send gate holds —
    // a Send while file 3/3 waits would silently drop it from the message).
    const tagged = files.map(f => f.error ? f : ({
      ...f,
      uploading: true as const,
      queued: true as const,
      abortController: new AbortController(),
    }))
    useChatStore.getState().addPendingFiles(draftKey, tagged)
    for (const f of tagged) {
      if (f.error || !f.abortController || !f.file) continue
      enqueueChatUpload({
        fileId: f.id, file: f.file, agent: agentName || '',
        abort: f.abortController,
      })
    }
  }, [agentName, draftKey])

  const handleRemoveFile = useCallback((id: string) => {
    if (!draftKey) return
    // Still queued → drop from the chain; in-flight → abort BEFORE the
    // mutator removes the entry (the mutator only removes by id).
    dequeueChatUpload(id)
    const target = useChatStore.getState().byChat[draftKey]?.pendingFiles.find(f => f.id === id)
    target?.abortController?.abort()
    useChatStore.getState().removePendingFile(draftKey, id)
  }, [draftKey])

  // Re-queue a failed upload from its error chip (ChatInput only offers the
  // affordance for retryable failures — oversize chips stay remove-only). A
  // folder resumes at its first file not landed, in the folder reserved for
  // it before.
  const handleRetryFile = useCallback((id: string) => {
    if (!draftKey) return
    const target = useChatStore.getState().byChat[draftKey]?.pendingFiles.find(f => f.id === id)
    if (!target?.file && !target?.folderFiles) return
    const abort = new AbortController()
    useChatStore.getState().updatePendingFile(draftKey, id, {
      error: undefined,
      uploading: true, queued: true,
      abortController: abort,
      ...(target.file ? { uploadSent: 0, uploadTotal: target.size } : {}),
    })
    enqueueChatUpload(target.folderFiles
      ? {
          fileId: id, agent: agentName || '', abort,
          folder: {
            name: target.name, files: target.folderFiles,
            path: target.folderPath, done: target.filesDone,
          },
        }
      : { fileId: id, file: target.file, agent: agentName || '', abort })
  }, [agentName, draftKey])

  // A folder attached in chat: one chip for all its files, uploaded through
  // the same queue into a fresh folder of the chat's upload folder. The chip
  // shows at once (uploading, so Send waits) while a dropped folder is still
  // being read; nothing is reserved until every file is known to fit.
  const handleAddFolder = useCallback((name: string, items: CollectedItems | Promise<CollectedItems>) => {
    if (!draftKey) return
    const id = Math.random().toString(36).slice(2, 10)
    const abort = new AbortController()
    useChatStore.getState().addPendingFiles(draftKey, [{
      id, name, size: 0, folder: true,
      uploading: true, queued: true, abortController: abort,
    }])
    const fail = (error: string, extra: Partial<PendingFile> = {}) => patchPendingFile(id, {
      ...extra, uploading: false, queued: false, abortController: undefined, error,
    })
    void Promise.resolve(items).then((got) => {
      if (abort.signal.aborted) return // the chip was removed meanwhile
      const size = got.files.reduce((a, f) => a + f.file.size, 0)
      const counted = { size, fileCount: got.files.length }
      const refused = folderRefusal(got, uploadCap)
      if (refused) {
        fail(refused, counted)
        return
      }
      patchPendingFile(id, { ...counted, folderFiles: got.files, note: folderNote(got) || undefined })
      enqueueChatUpload({ fileId: id, folder: { name, files: got.files }, agent: agentName || '', abort })
    }, () => {
      if (!abort.signal.aborted) fail("This folder can't be read")
    })
  }, [agentName, draftKey, uploadCap])

  return { handleAddFiles, handleAddFolder, handleRemoveFile, handleRetryFile }
}
