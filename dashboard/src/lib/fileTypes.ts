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
// Icon hints: a VS-Code-inspired letter or glyph, coloured by what the file
// is for, never by a hue per type (one accent per meaning): media reads the
// media accent, documents the primary text, code and config the secondary,
// archives and the unknown the tertiary. Shared by FileTree and FileGrid.
// ---------------------------------------------------------------------------

export interface FileIconStyle {
  icon: string
  color: string
}

const MEDIA = 'text-accent-media-fg'
const DOCUMENT = 'text-primary'
const CODE = 'text-secondary'
const QUIET = 'text-tertiary'

const ICON_STYLES: Record<string, FileIconStyle> = {
  // Markdown / docs
  '.md':    { icon: 'M',  color: DOCUMENT },
  '.txt':   { icon: 'T',  color: CODE },
  '.log':   { icon: 'L',  color: QUIET },
  // Code
  '.py':    { icon: 'Py', color: CODE },
  '.js':    { icon: 'JS', color: CODE },
  '.ts':    { icon: 'TS', color: CODE },
  '.tsx':   { icon: 'TX', color: CODE },
  '.sh':    { icon: '$',  color: CODE },
  // Data / config
  '.json':  { icon: '{}', color: CODE },
  '.yaml':  { icon: 'Y',  color: CODE },
  '.yml':   { icon: 'Y',  color: CODE },
  '.toml':  { icon: 'C',  color: CODE },
  '.ini':   { icon: 'C',  color: CODE },
  '.conf':  { icon: 'C',  color: CODE },
  '.cfg':   { icon: 'C',  color: CODE },
  '.env':   { icon: 'E',  color: CODE },
  '.xml':   { icon: '<>', color: CODE },
  '.html':  { icon: '<>', color: CODE },
  '.css':   { icon: '#',  color: CODE },
  '.csv':   { icon: ',',  color: DOCUMENT },
  // Documents
  '.pdf':   { icon: 'P',  color: DOCUMENT },
  '.docx':  { icon: 'W',  color: DOCUMENT },
  '.xlsx':  { icon: 'X',  color: DOCUMENT },
  '.pptx':  { icon: 'S',  color: DOCUMENT },
  // Images
  '.png':   { icon: 'I',  color: MEDIA },
  '.jpg':   { icon: 'I',  color: MEDIA },
  '.jpeg':  { icon: 'I',  color: MEDIA },
  '.gif':   { icon: 'I',  color: MEDIA },
  '.svg':   { icon: 'S',  color: MEDIA },
  '.webp':  { icon: 'I',  color: MEDIA },
  '.bmp':   { icon: 'I',  color: MEDIA },
  // Video
  '.mp4':   { icon: 'V',  color: MEDIA },
  '.m4v':   { icon: 'V',  color: MEDIA },
  '.mov':   { icon: 'V',  color: MEDIA },
  '.webm':  { icon: 'V',  color: MEDIA },
  '.mkv':   { icon: 'V',  color: MEDIA },
  '.avi':   { icon: 'V',  color: MEDIA },
  // Audio
  '.mp3':   { icon: 'A',  color: MEDIA },
  '.m4a':   { icon: 'A',  color: MEDIA },
  '.aac':   { icon: 'A',  color: MEDIA },
  '.wav':   { icon: 'A',  color: MEDIA },
  '.ogg':   { icon: 'A',  color: MEDIA },
  '.opus':  { icon: 'A',  color: MEDIA },
  '.flac':  { icon: 'A',  color: MEDIA },
  // Archives
  '.zip':   { icon: 'Z',  color: QUIET },
  '.tar':   { icon: 'Z',  color: QUIET },
  '.gz':    { icon: 'Z',  color: QUIET },
}

const DEFAULT_STYLE: FileIconStyle = { icon: 'F', color: QUIET }

export function getFileIconStyle(name: string): FileIconStyle {
  return ICON_STYLES[getFileExtension(name)] ?? DEFAULT_STYLE
}

export function formatFileSize(bytes: number): string {
  if (bytes === 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
