import { useChatDocuments } from '../../../api/documents'
import { leaveDocumentPane, useDocumentPaneStore, useDocumentPushStore } from '../../../store/documentPaneStore'
import { extensionBadge, pushTime } from './documentTabs'

interface Props {
  chatId?: string
  fileId: string
  filename: string
  downloadUrl: string
  snapshotId?: string
  dbMessageId?: number
  /** The number stamped on the push (the listing's number wins). */
  version?: number
  /** The push time, epoch ms. */
  generation?: number
}

/**
 * A document pushed in this turn, as the chat shows it: the file, its
 * version number and the push time. The file's newest card opens the pane
 * on the live file; an older one opens its version read-only; an older
 * version whose copy is gone reads "no longer available".
 */
export default function DocumentCard({
  chatId, fileId, filename, downloadUrl, snapshotId, dbMessageId, version, generation,
}: Props) {
  const { data: docs } = useChatDocuments(chatId)
  const doc = docs?.find((d) => d.file_id === fileId)
  const versions = doc?.versions ?? []
  const mine = versions.find((v) => (snapshotId && v.snapshot_id === snapshotId)
    || (dbMessageId != null && v.message_id === dbMessageId))
  // Not in the listing yet (a push this turn) or the listing's newest: Live.
  const newest = !mine || mine === versions[0]
  const number = mine?.version ?? version
  const gone = !newest && !!mine && !mine.available

  const open = () => {
    if (!chatId || gone) return
    const go = () => {
      useDocumentPushStore.getState().noteKnown(chatId, {
        fileId, filename, downloadUrl, generation: generation ?? 0,
      })
      useDocumentPushStore.getState().setAttended(chatId, true)
      useDocumentPaneStore.getState().openFile(chatId, fileId, newest ? null : mine!.snapshot_id)
    }
    // The document on show may hold unsaved edits: a refused leave brings
    // the pane back with its note.
    void leaveDocumentPane(chatId).then((ok) => (ok ? go() : useDocumentPaneStore.getState().restore(chatId)))
  }

  const label = (
    <>
      <svg className="h-5 w-5 shrink-0 text-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
      </svg>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          <span className="truncate text-sm font-medium text-primary">{filename || 'Document'}</span>
          <span className="shrink-0 rounded-sm bg-surface-muted px-1.5 py-0.5 text-3xs font-semibold text-secondary">
            {extensionBadge(filename)}
          </span>
        </span>
        <span className="block text-2xs text-tertiary">
          {number ? `Version ${number}` : 'Document'}
          {gone ? ', no longer available' : ''}
          {generation ? ` · ${pushTime(generation)}` : ''}
        </span>
      </span>
    </>
  )

  if (gone || !chatId) {
    return (
      <div className="my-1 flex w-full max-w-sm items-center gap-3 rounded-card border border-subtle bg-surface-muted/50 px-3 py-2 opacity-70">
        {label}
      </div>
    )
  }
  return (
    <button
      type="button"
      onClick={open}
      title={newest ? 'Open the document' : 'Open this version, read-only'}
      className="my-1 flex w-full max-w-sm items-center gap-3 rounded-card bg-surface-card shadow-elevation-1 px-3 py-2 text-left transition-colors hover:bg-surface-hover"
    >
      {label}
    </button>
  )
}
