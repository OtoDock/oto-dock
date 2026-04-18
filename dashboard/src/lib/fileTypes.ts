/**
 * Centralized file-type metadata: extension sets, kind classification, and
 * icon hints. Replaces the scattered sets that used to live in ChatInput,
 * FileTree, FileEditor, and the agent workspace views.
 */

export const TEXT_EXTENSIONS = new Set([
  '.md', '.json', '.txt', '.py', '.yaml', '.yml', '.sh',
  '.conf', '.cfg', '.ini', '.toml', '.env', '.log',
  '.xml', '.html', '.css', '.csv',
])

export const IMAGE_EXTENSIONS = new Set([
  '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.bmp', '.tiff', '.tif',
])

export const VIDEO_EXTENSIONS = new Set([
  '.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi',
])

export const AUDIO_EXTENSIONS = new Set([
  '.mp3', '.m4a', '.aac', '.wav', '.ogg', '.oga', '.opus', '.flac',
])

/** Files that open in Collabora via the WOPI flow. */
export const DOCUMENT_EXTENSIONS = new Set([
  '.pdf', '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt',
  '.odt', '.ods', '.odp', '.rtf',
])

export const ARCHIVE_EXTENSIONS = new Set(['.zip', '.tar', '.gz'])

// Uploads accept ANY file type — agents work in full dev environments, so
// there is no extension allowlist (mirrors `proxy/api/media/uploads.py`: the
// platform never executes uploads and the serving routes force non-inert
// types to attachment + nosniff). Only the size caps differ: audio/video
// (AUDIO_EXTENSIONS/VIDEO_EXTENSIONS) get the larger media cap.

export type FileKind = 'text' | 'image' | 'video' | 'audio' | 'document' | 'archive' | 'other'

export function getFileExtension(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot).toLowerCase() : ''
}

export function getFileKind(name: string): FileKind {
  const ext = getFileExtension(name)
  if (TEXT_EXTENSIONS.has(ext)) return 'text'
  if (IMAGE_EXTENSIONS.has(ext)) return 'image'
  if (VIDEO_EXTENSIONS.has(ext)) return 'video'
  if (AUDIO_EXTENSIONS.has(ext)) return 'audio'
  if (DOCUMENT_EXTENSIONS.has(ext)) return 'document'
  if (ARCHIVE_EXTENSIONS.has(ext)) return 'archive'
  return 'other'
}

// MIME → canonical extension for media downloads (mirror of the proxy's
// services/media/media_pipeline.py::_CANONICAL_EXT). Used to give a download filename
// a correct extension when the agent-supplied caption/title has none.
const MEDIA_MIME_EXT: Record<string, string> = {
  'video/mp4': '.mp4',
  'video/quicktime': '.mov',
  'video/webm': '.webm',
  'video/x-matroska': '.mkv',
  'video/ogg': '.ogv',
  'video/x-msvideo': '.avi',
  'video/x-ms-wmv': '.wmv',
  'audio/mpeg': '.mp3',
  'audio/mp4': '.m4a',
  'audio/aac': '.aac',
  'audio/wav': '.wav',
  'audio/ogg': '.ogg',
  'audio/flac': '.flac',
  'audio/webm': '.weba',
  'audio/aiff': '.aiff',
}

/**
 * Ensure a media download filename carries a correct extension. Returns `base`
 * unchanged when it already ends in a known media extension; otherwise appends
 * one derived from `mime`, falling back to the `src` URL's path suffix. Needed
 * because a same-origin `<a download="caption">` (no extension) overrides the
 * server's Content-Disposition, so the extension must be set client-side too.
 */
export function ensureMediaDownloadName(base: string, mime?: string, src?: string): string {
  const name = base || 'media'
  const existing = getFileExtension(name)
  if (VIDEO_EXTENSIONS.has(existing) || AUDIO_EXTENSIONS.has(existing)) return name
  let ext = mime ? MEDIA_MIME_EXT[mime.split(';')[0].trim().toLowerCase()] || '' : ''
  if (!ext && src) {
    try {
      const e = getFileExtension(new URL(src, 'http://x').pathname)
      if (VIDEO_EXTENSIONS.has(e) || AUDIO_EXTENSIONS.has(e)) ext = e
    } catch {
      /* not a parseable URL — leave ext empty */
    }
  }
  return ext ? name + ext : name
}

// ---------------------------------------------------------------------------
// Icon hints — VS-Code-inspired letter/glyph + tailwind color class.
// Shared by FileTree (tree mode) and FileGrid (grid mode).
// ---------------------------------------------------------------------------

export interface FileIconStyle {
  icon: string
  color: string
}

const ICON_STYLES: Record<string, FileIconStyle> = {
  // Markdown / docs
  '.md':    { icon: 'M',  color: 'text-blue-500' },
  '.txt':   { icon: 'T',  color: 'text-gray-500' },
  '.log':   { icon: 'L',  color: 'text-gray-400' },
  // Code
  '.py':    { icon: 'Py', color: 'text-yellow-500' },
  '.js':    { icon: 'JS', color: 'text-yellow-400' },
  '.ts':    { icon: 'TS', color: 'text-blue-400' },
  '.tsx':   { icon: 'TX', color: 'text-blue-400' },
  '.sh':    { icon: '$',  color: 'text-green-500' },
  // Data / config
  '.json':  { icon: '{}', color: 'text-yellow-600' },
  '.yaml':  { icon: 'Y',  color: 'text-purple-500' },
  '.yml':   { icon: 'Y',  color: 'text-purple-500' },
  '.toml':  { icon: 'C',  color: 'text-gray-500' },
  '.ini':   { icon: 'C',  color: 'text-gray-500' },
  '.conf':  { icon: 'C',  color: 'text-gray-500' },
  '.cfg':   { icon: 'C',  color: 'text-gray-500' },
  '.env':   { icon: 'E',  color: 'text-yellow-700' },
  '.xml':   { icon: '<>', color: 'text-orange-500' },
  '.html':  { icon: '<>', color: 'text-orange-400' },
  '.css':   { icon: '#',  color: 'text-blue-300' },
  '.csv':   { icon: ',',  color: 'text-green-600' },
  // Documents
  '.pdf':   { icon: 'P',  color: 'text-red-500' },
  '.docx':  { icon: 'W',  color: 'text-blue-600' },
  '.xlsx':  { icon: 'X',  color: 'text-green-600' },
  '.pptx':  { icon: 'S',  color: 'text-orange-500' },
  // Images
  '.png':   { icon: 'I',  color: 'text-pink-500' },
  '.jpg':   { icon: 'I',  color: 'text-pink-500' },
  '.jpeg':  { icon: 'I',  color: 'text-pink-500' },
  '.gif':   { icon: 'I',  color: 'text-pink-400' },
  '.svg':   { icon: 'S',  color: 'text-pink-400' },
  '.webp':  { icon: 'I',  color: 'text-pink-500' },
  '.bmp':   { icon: 'I',  color: 'text-pink-500' },
  // Video
  '.mp4':   { icon: 'V',  color: 'text-purple-500' },
  '.m4v':   { icon: 'V',  color: 'text-purple-500' },
  '.mov':   { icon: 'V',  color: 'text-purple-500' },
  '.webm':  { icon: 'V',  color: 'text-purple-500' },
  '.mkv':   { icon: 'V',  color: 'text-purple-500' },
  '.avi':   { icon: 'V',  color: 'text-purple-500' },
  // Audio
  '.mp3':   { icon: 'A',  color: 'text-teal-500' },
  '.m4a':   { icon: 'A',  color: 'text-teal-500' },
  '.aac':   { icon: 'A',  color: 'text-teal-500' },
  '.wav':   { icon: 'A',  color: 'text-teal-500' },
  '.ogg':   { icon: 'A',  color: 'text-teal-500' },
  '.opus':  { icon: 'A',  color: 'text-teal-500' },
  '.flac':  { icon: 'A',  color: 'text-teal-500' },
  // Archives
  '.zip':   { icon: 'Z',  color: 'text-amber-600' },
  '.tar':   { icon: 'Z',  color: 'text-amber-600' },
  '.gz':    { icon: 'Z',  color: 'text-amber-600' },
}

const DEFAULT_STYLE: FileIconStyle = { icon: 'F', color: 'text-gray-400' }

export function getFileIconStyle(name: string): FileIconStyle {
  return ICON_STYLES[getFileExtension(name)] ?? DEFAULT_STYLE
}

export function formatFileSize(bytes: number): string {
  if (bytes === 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
