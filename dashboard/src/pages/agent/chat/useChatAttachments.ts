/**
 * useChatAttachments — AgentChat's eager file-upload handlers for the
 * composer's attachment chips: handleAddFiles / handleRemoveFile /
 * handleRetryFile over the global sequential upload queue
 * (lib/chatUploadQueue) and the chatStore pending-files slice.
 */
import { useCallback } from 'react'
import type { PendingFile } from '../../../components/chat/ChatInput'
import { enqueueChatUpload, dequeueChatUpload } from '../../../lib/chatUploadQueue'
import { useChatStore } from '../../../store/chatStore'

export function useChatAttachments({ agentName, draftKey }: {
  agentName: string | undefined
  draftKey: string
}) {
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
  // affordance for retryable failures — oversize chips stay remove-only).
  const handleRetryFile = useCallback((id: string) => {
    if (!draftKey) return
    const target = useChatStore.getState().byChat[draftKey]?.pendingFiles.find(f => f.id === id)
    if (!target?.file) return
    const abort = new AbortController()
    useChatStore.getState().updatePendingFile(draftKey, id, {
      error: undefined,
      uploading: true, queued: true,
      uploadSent: 0, uploadTotal: target.size,
      abortController: abort,
    })
    enqueueChatUpload({
      fileId: id, file: target.file, agent: agentName || '', abort,
    })
  }, [agentName, draftKey])

  return { handleAddFiles, handleRemoveFile, handleRetryFile }
}
