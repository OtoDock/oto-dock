import { useMemo } from 'react'
import { useChatDocuments, type ChatDocument } from '@/api/documents'
import {
  useDocumentPaneStore, useDocumentPushStore, type KnownDocument, type PaneChat, type PushedDocument,
} from '@/store/documentPaneStore'

/** One tab of a chat's document pane. */
export interface DocumentTab {
  fileId: string
  filename: string
  downloadUrl: string
  /** The newest push of the file, epoch ms. */
  generation: number
  /** The listing's entry (versions), absent until the listing has the file. */
  doc?: ChatDocument
}

const NO_PUSHED: Record<string, PushedDocument> = {}
const NO_KNOWN: Record<string, KnownDocument> = {}
const NO_PANE: PaneChat = {
  open: false, minimized: false, active: null, closed: [], fresh: [], view: {}, seen: 0, touched: 0,
}

/** The pane's tabs for a chat: every document pushed into it (the listing,
 * plus pushes and cards the listing has not caught up with), newest first,
 * minus the ones the person closed. */
export function useDocumentTabs(chatId: string | null | undefined) {
  const { data: docs, isError } = useChatDocuments(chatId)
  const pushed = useDocumentPushStore((s) => (chatId ? s.pushed[chatId] : undefined) ?? NO_PUSHED)
  const known = useDocumentPushStore((s) => (chatId ? s.known[chatId] : undefined) ?? NO_KNOWN)
  const pane = useDocumentPaneStore((s) => (chatId ? s.byChat[chatId] : undefined) ?? NO_PANE)

  const all = useMemo(() => {
    const byFile = new Map<string, DocumentTab>()
    for (const doc of docs ?? []) {
      byFile.set(doc.file_id, {
        fileId: doc.file_id, filename: doc.filename, downloadUrl: doc.download_url,
        generation: doc.generation, doc,
      })
    }
    const extras: KnownDocument[] = [...Object.values(known), ...Object.values(pushed)]
    for (const extra of extras) {
      const cur = byFile.get(extra.fileId)
      if (!cur) {
        byFile.set(extra.fileId, {
          fileId: extra.fileId, filename: extra.filename, downloadUrl: extra.downloadUrl,
          generation: extra.generation,
        })
      } else if (extra.generation > cur.generation) {
        byFile.set(extra.fileId, {
          ...cur, generation: extra.generation,
          filename: extra.filename || cur.filename, downloadUrl: extra.downloadUrl || cur.downloadUrl,
        })
      }
    }
    return [...byFile.values()].sort((a, b) => b.generation - a.generation)
  }, [docs, known, pushed])

  const tabs = useMemo(() => all.filter((t) => !pane.closed.includes(t.fileId)), [all, pane.closed])
  const active = tabs.find((t) => t.fileId === pane.active) ?? tabs[0] ?? null
  return { tabs, all, active, pane, docs, listingFailed: isError }
}

export function extensionBadge(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const labels: Record<string, string> = {
    pdf: 'PDF', docx: 'DOCX', doc: 'DOC', xlsx: 'XLSX', xls: 'XLS',
    pptx: 'PPTX', ppt: 'PPT', odt: 'ODT', ods: 'ODS', odp: 'ODP',
    csv: 'CSV', txt: 'TXT', html: 'HTML', rtf: 'RTF',
  }
  return labels[ext] || ext.toUpperCase()
}

/** A push time for a card or a version row: the time today, else the date
 * and the time. */
export function pushTime(generation: number): string {
  if (!generation) return ''
  const d = new Date(generation)
  const today = new Date()
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === today.toDateString()) return time
  return `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })}, ${time}`
}
