import type { GalleryImage } from './media/ImageGallery'

// --- Types ---

export type MessageBlock =
  | { type: 'text'; content: string }
  | { type: 'thinking'; content: string; collapsed: boolean; done?: boolean; tokens?: number }
  | { type: 'tool'; name: string; toolId: string; summary: string; status: 'running' | 'done' | 'failed'; toolInput?: any; toolResult?: string; resultSummary?: string }
  | { type: 'subagent'; description: string; subagentType: string; isActive?: boolean; failed?: boolean; _toolId?: string | null; _background?: boolean; toolInput?: any; toolResult?: string }
  | { type: 'delegate'; taskName: string; agent: string; promptPreview: string; status: 'running' | 'completed' | 'failed' | 'cancelled' | 'user_interrupted'; _taskId?: string; prompt?: string; workerChatId?: string }
  | { type: 'schedulewake'; prompt: string }
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
  | { type: 'question'; toolName: string; toolInput: any; answered?: boolean; requestId?: string }
  | { type: 'plan'; action: 'enter' | 'exit'; toolInput?: any; superseded?: boolean }
  | { type: 'plan_review'; requestId: string; plan: string; toolInput: any; filename?: string; resolved?: boolean; action?: string }
  | { type: 'system'; subtype: string; agentName?: string; agentColor?: string; message?: string }
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
  | { type: 'document_preview'; wopiUrl: string; filename: string; fileId: string; downloadUrl: string; dbMessageId?: number }
  | { type: 'metadata'; costUsd: number; durationMs: number }

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
