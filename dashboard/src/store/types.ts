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
  uploadedPath?: string
  error?: string
  abortController?: AbortController
}
