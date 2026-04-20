import { useEffect, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { pushEscHandler } from '../../lib/escStack'
import { getFileExtension } from '../../lib/fileTypes'

interface Props {
  filename: string
  onClose: () => void
  children: ReactNode
  /** Optional download URL — if set, a download button appears in the chrome. */
  downloadUrl?: string
  /** If set, a reload button appears; only meaningful for live (Collabora) content. */
  onReload?: () => void
  /** Inline header content rendered between the filename and the action buttons. */
  headerExtra?: ReactNode
  /** Custom body background. Defaults to opaque black for Collabora/images. */
  bodyBg?: string
}

function getExtBadge(filename: string): string {
  const ext = getFileExtension(filename).replace('.', '')
  if (!ext) return ''
  const labels: Record<string, string> = {
    pdf: 'PDF', docx: 'DOCX', doc: 'DOC', xlsx: 'XLSX', xls: 'XLS',
    pptx: 'PPTX', ppt: 'PPT', odt: 'ODT', ods: 'ODS', odp: 'ODP',
    csv: 'CSV', txt: 'TXT', html: 'HTML', rtf: 'RTF', md: 'MD',
    json: 'JSON', yaml: 'YAML', yml: 'YML',
  }
  return labels[ext] ?? ext.toUpperCase()
}

/**
 * Full-screen portal with a shared toolbar. Body is freeform — pass a Collabora
 * iframe, a markdown renderer, an `<img>`, or whatever fits the file kind. The
 * portal owns Esc handling (via the precedence stack) and body scroll lock; the
 * caller decides how to render the content.
 */
export default function FilePreviewPortal({
  filename,
  onClose,
  children,
  downloadUrl,
  onReload,
  headerExtra,
  bodyBg = 'bg-black/95',
}: Props) {
  useEffect(() => {
    const pop = pushEscHandler(onClose)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      pop()
      document.body.style.overflow = prev
    }
  }, [onClose])

  const extBadge = getExtBadge(filename)

  return createPortal(
    <div className={`fixed inset-0 z-50 flex flex-col ${bodyBg}`}>
      {/* Header bar */}
      <div className="flex items-center justify-between px-4 py-2 bg-black/80 border-b border-white/10">
        <div className="flex items-center gap-2 min-w-0">
          <svg
            className="w-5 h-5 text-white/70 shrink-0"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
          </svg>
          <span className="text-sm font-medium text-white truncate">{filename}</span>
          {extBadge && (
            <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-sm bg-white/10 text-white/80 shrink-0">
              {extBadge}
            </span>
          )}
          {headerExtra}
        </div>
        <div className="flex items-center gap-1 shrink-0 ml-2">
          {onReload && (
            <button
              onClick={onReload}
              title="Reload"
              className="w-8 h-8 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 text-white transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182M20.016 4.66v4.993" />
              </svg>
            </button>
          )}
          {downloadUrl && (
            <a
              href={downloadUrl}
              download={filename}
              title="Download"
              className="w-8 h-8 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 text-white transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
              </svg>
            </a>
          )}
          <button
            onClick={onClose}
            title="Close"
            className="w-8 h-8 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 text-white text-lg transition-colors"
          >
            &times;
          </button>
        </div>
      </div>
      {/* Body */}
      <div className="flex-1 min-h-0">{children}</div>
    </div>,
    document.body,
  )
}
