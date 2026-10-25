import type { GalleryImage } from './media/ImageGallery'
import type { DelegateBlockStatus } from '../../lib/status/run'

// --- Types ---

export type MessageBlock =
  // `note`: a progress note, the agent's words between its steps (a Codex
  // preamble, a Claude narration block): text for every reader, drawn faint
  // in the steps group.
  | { type: 'text'; content: string; note?: boolean }
  | { type: 'thinking'; content: string; collapsed: boolean; done?: boolean; tokens?: number }
  | { type: 'tool'; name: string; toolId: string; summary: string; status: 'running' | 'done' | 'failed'; toolInput?: unknown; toolResult?: string; resultSummary?: string }
  | { type: 'subagent'; description: string; subagentType: string; isActive?: boolean; failed?: boolean; _toolId?: string | null; _background?: boolean; toolInput?: unknown; toolResult?: string }
  | { type: 'delegate'; taskName: string; agent: string; promptPreview: string; status: DelegateBlockStatus; _taskId?: string; prompt?: string; workerChatId?: string }
  // The files a delegated worker attached to its result, as they landed in
  // THIS chat's agent tree (agent-relative paths the chat can open), and the
  // ones that did not land with the proxy's reason. Rides the worker's
  // response bubble; a files-only result shows the list alone.
  | { type: 'delegate_files'; files: Array<{ path: string; bytes: number }>; skipped: Array<{ path: string; reason: string }> }
  | { type: 'schedulewake'; prompt: string; source?: string }
  // A check's verdict on this turn (CHECKS.md): the compact card.
  | { type: 'checkverdict'; check: string; status: string; pass: boolean; score?: number | null; summary: string; findings: Array<{ location: string; severity: string; text: string }>; findingsTotal: number; round: number; rounds: number; ranOn: string; costUsd: number; verdictId?: string }
  | { type: 'bgcommand'; command: string; description?: string; isActive?: boolean; failed?: boolean; _toolId?: string | null }
  | {
      type: 'permission'
      requestId: string
      toolName: string
      toolInput: unknown
      description?: string
      resolved?: boolean
      approved?: boolean
      meetingAgent?: { slug: string; displayName: string; color: string }
    }
  // `followedInTurn`: later blocks of the same assistant message follow the
  // card (an interactive terminal's continuation after the picker answer,
  // which writes no user row); the renderer treats it as answered on
  // interactive chats only, since a headless turn's closing metadata block
  // follows every card.
  | { type: 'question'; toolName: string; toolInput: unknown; answered?: boolean; requestId?: string; followedInTurn?: boolean }
  // `resolved`: a later message followed the approval card — an interactive
  // terminal's read-only hint stands down (`followedInTurn` as above).
  | { type: 'plan'; action: string; toolInput?: unknown; superseded?: boolean; resolved?: boolean; followedInTurn?: boolean }
  | { type: 'plan_review'; requestId: string; plan: string; toolInput: unknown; filename?: string; resolved?: boolean; action?: string; unanswered?: boolean }
  | { type: 'system'; subtype: string; agentName?: string; agentColor?: string; message?: string; reason?: string; returned?: boolean; count?: number }
  | { type: 'images'; images: GalleryImage[] }
  | { type: 'video'; srcKind: 'url' | 'token'; url?: string; mediaUrl?: string; token?: string; mime?: string; caption?: string; title?: string; poster?: string }
  | { type: 'audio'; srcKind: 'url' | 'token'; url?: string; mediaUrl?: string; token?: string; mime?: string; caption?: string; title?: string }
  | { type: 'media_processing'; mediaKind: 'video' | 'audio'; caption?: string }
  | { type: 'image_generating'; promptPreview: string; model: string }
  | { type: 'image_attachments'; images: string[]; paths?: (string | null)[] }
  | { type: 'file_attachments'; files: Array<{ name: string; path?: string; folder?: boolean }> }
  | { type: 'url'; url: string; title: string; description: string }
  | { type: 'file'; filename: string; downloadUrl: string; description: string }
  | { type: 'ui'; token: string; uiUrl: string; title?: string; height?: number; path?: string }
  | { type: 'artifact_interaction'; token: string; title?: string; payload?: unknown }
  | { type: 'app_action'; appId: string; slug?: string; title?: string; actionId: string; label?: string; prompt?: string }
  | { type: 'document_preview'; filename: string; fileId: string; downloadUrl: string; dbMessageId?: number; snapshotId?: string; generation?: number; version?: number }
  // costBilled false = the turn ran on a subscription or a local model: the cost
  // is an activity estimate and its badge is hidden. Missing = shown (rows
  // persisted before the flag existed).
  | { type: 'metadata'; costUsd: number; durationMs: number; costBilled?: boolean }

export interface DisplayMessage {
  id: string
  role: 'user' | 'assistant'
  blocks: MessageBlock[]
  createdAt: string
  agentSlug?: string
  agentDisplayName?: string
  agentColor?: string
  badge?: string
  /** A delegate's report (`result`) or a worker lane's incoming prompt
   *  (`prompt`): rendered as a collapsed card (`DelegateCard`). Set by the
   *  builders (`lib/messageBlocks.ts`, the live frame handlers), never by the
   *  renderer; `key` is the same for the live bubble and its stored row, so
   *  a card's open state survives the refetch that replaces one by the other. */
  delegateCard?: DelegateCardMark
  /** A person's message steered into the running turn (the turn went on, it
   *  did not start over): the open turn's bubbles span it. */
  steered?: boolean
  /** The reply goes on from the agent's bubble above it: the review turn its
   *  finished background work woke, or a turn's live part below its saved
   *  rows. Drawn joined to that bubble (no header), still its own bubble.
   *  Set by the builders (`lib/messageBlocks.ts`, the live handlers). */
  continuation?: boolean
}

/** What the page may ask of the message list (`ChatMessages`' ref). */
export interface ChatMessagesHandle {
  /** Back to the newest message, following the stream again: the page's own
   *  sends only (a frame from another tab or a delegate result never). */
  pinToBottom: () => void
}

export interface DelegateCardMark {
  kind: 'result' | 'prompt'
  key: string
}
