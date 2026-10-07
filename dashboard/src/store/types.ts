// Shared types referenced by both UI components (ChatInput) and the
// per-chat state store (chatStore). Lives here so chatStore doesn't have
// to import from a component file.

export interface PendingImage {
  id: string
  /** A fresh photo: the data URL to send. Absent for a photo the chat's
   * scope already holds (one handed back from a cancelled queued message),
   * which is re-sent by `path` and shown through the agent files URL. */
  base64?: string
  /** Agent-relative saved path of an already-saved photo. */
  path?: string
  name: string
}

export interface PendingFile {
  id: string
  name: string
  size: number
  /** The picked File while it uploads; absent for a file handed back from
   * a cancelled queued message (already uploaded — `uploadedPath` is set). */
  file?: File
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

/** A photo as the server names it after saving it (`{name, path}`). */
export interface AttachedImageMeta {
  name: string
  path?: string
}

/** An uploaded file as the server validated it (`{path, name}`). */
export interface AttachedFileMeta {
  path: string
  name: string
}

/** One message waiting behind a live turn (or held by a steer): the text
 * plus the attachment meta the `queued` / `steered` / `queue_snapshot`
 * frames carry, so the bubble shows the chips and a cancel can hand the
 * attachments back to the composer. */
/** One waiting message of a chat's queue (the chat owns it, the proxy's
 *  `chat_input_queue`): the chip the composer shows. `queueId` names it on
 *  the wire (cancel, accept); `authorSub` is who typed it (a shared chat
 *  shows a teammate's chips, only the author or an admin may cancel). A
 *  1.7.0 proxy sends neither: the index is the fallback key. */
export interface QueuedMessage {
  text: string
  images?: AttachedImageMeta[]
  files?: AttachedFileMeta[]
  queueId?: string
  authorSub?: string
  /** What it waits for besides a free turn (`QUEUE_WAITING`). */
  waiting?: string
}

/** A queue entry as the wire or the persisted store carries it: a frame
 *  (`queue_id`, `author_sub`), a v2 item, or a v1 bare text. */
export function toQueuedMessage(m: unknown): QueuedMessage {
  if (typeof m === 'string') return { text: m }
  if (m && typeof m === 'object') {
    const o = m as Record<string, unknown>
    const out: QueuedMessage = { text: typeof o.text === 'string' ? o.text : '' }
    if (Array.isArray(o.images) && o.images.length) out.images = o.images as AttachedImageMeta[]
    if (Array.isArray(o.files) && o.files.length) out.files = o.files as AttachedFileMeta[]
    const queueId = o.queue_id ?? o.queueId
    if (typeof queueId === 'string' && queueId) out.queueId = queueId
    const authorSub = o.author_sub ?? o.authorSub
    if (typeof authorSub === 'string' && authorSub) out.authorSub = authorSub
    if (typeof o.waiting === 'string' && o.waiting) out.waiting = o.waiting
    return out
  }
  return { text: '' }
}
