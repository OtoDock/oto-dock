// Shared types referenced by both UI components (ChatInput) and the
// per-chat state store (chatStore). Lives here so chatStore doesn't have
// to import from a component file.

export interface PendingImage {
  id: string
  base64: string
  name: string
}

export interface PendingFile {
  id: string
  name: string
  size: number
  file: File
  uploading?: boolean
  /** Waiting in the sequential upload queue. `uploading` stays true while
   * queued so the send gate still holds — a Send while a file waits in the
   * queue would silently drop it (handleSend keeps only uploadedPath). */
  queued?: boolean
  uploadedPath?: string
  error?: string
  abortController?: AbortController
  /** Live byte progress while the XHR runs (throttled writes). */
  uploadSent?: number
  uploadTotal?: number
  /** Server transfer id from the upload response — joins the chip to the
   * phase-2 remote-machine sync progress in transferStore. */
  transferId?: string
  remotePush?: boolean
}
