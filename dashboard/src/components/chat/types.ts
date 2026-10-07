import type { GalleryImage } from './media/ImageGallery'
import type { DelegateBlockStatus } from '../../lib/status/run'

// --- Types ---

export type MessageBlock =
  | { type: 'text'; content: string }
  | { type: 'thinking'; content: string; collapsed: boolean; done?: boolean; tokens?: number }
  | { type: 'tool'; name: string; toolId: string; summary: string; status: 'running' | 'done' | 'failed'; toolInput?: any; toolResult?: string; resultSummary?: string }
  | { type: 'subagent'; description: string; subagentType: string; isActive?: boolean; failed?: boolean; _toolId?: string | null; _background?: boolean; toolInput?: any; toolResult?: string }
  | { type: 'delegate'; taskName: string; agent: string; promptPreview: string; status: DelegateBlockStatus; _taskId?: string; prompt?: string; workerChatId?: string }
  // The files a delegated worker attached to its result, as they landed in
  // THIS chat's agent tree (agent-relative paths the chat can open), and the
  // ones that did not land with the proxy's reason. Rides the worker's
  // response bubble; a files-only result shows the list alone.
  | { type: 'delegate_files'; files: Array<{ path: string; bytes: number }>; skipped: Array<{ path: string; reason: string }> }
  | { type: 'schedulewake'; prompt: string }
  // A check's verdict on this turn (CHECKS.md): the compact card.
  | { type: 'checkverdict'; check: string; status: 'pass' | 'fail' | 'error' | 'skipped'; pass: boolean; score?: number | null; summary: string; findings: Array<{ location: string; severity: string; text: string }>; findingsTotal: number; round: number; rounds: number; ranOn: string; costUsd: number; verdictId?: string }
  | { type: 'bgcommand'; command: string; description?: string; isActive?: boolean; failed?: boolean; _toolId?: string | null }
  | {
      type: 'permission'
      requestId: string
      toolName: string
      toolInput: any
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
  | { type: 'question'; toolName: string; toolInput: any; answered?: boolean; requestId?: string; followedInTurn?: boolean }
  // `resolved`: a later message followed the approval card — an interactive
  // terminal's read-only hint stands down (`followedInTurn` as above).
  | { type: 'plan'; action: 'enter' | 'exit'; toolInput?: any; superseded?: boolean; resolved?: boolean; followedInTurn?: boolean }
  | { type: 'plan_review'; requestId: string; plan: string; toolInput: any; filename?: string; resolved?: boolean; action?: string }
  | { type: 'system'; subtype: string; agentName?: string; agentColor?: string; message?: string; reason?: string }
  | { type: 'images'; images: GalleryImage[] }
  | { type: 'video'; srcKind: 'url' | 'token'; url?: string; mediaUrl?: string; token?: string; mime?: string; caption?: string; title?: string; poster?: string }
  | { type: 'audio'; srcKind: 'url' | 'token'; url?: string; mediaUrl?: string; token?: string; mime?: string; caption?: string; title?: string }
  | { type: 'media_processing'; mediaKind: 'video' | 'audio'; caption?: string }
  | { type: 'image_generating'; promptPreview: string; model: string }
  | { type: 'image_attachments'; images: string[]; paths?: (string | null)[] }
  | { type: 'file_attachments'; files: Array<{ name: string; path?: string }> }
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
}
