// The minimized document pane's chips for the chat's left stack.

import { useDocumentTabs } from '../../../components/chat/media/documentTabs'
import { leaveDocumentPane, useDocumentPaneStore, useDocumentPushStore } from '../../../store/documentPaneStore'

/** The minimized pane's chips for the left stack: one per open document,
 * at most four, then a chip that restores the pane. */
export function useDocumentChips(chatId: string | null, hidden: boolean) {
  const { tabs, pane, active } = useDocumentTabs(chatId)
  if (!chatId || hidden || !pane.open || !pane.minimized) return null
  const store = useDocumentPaneStore.getState()
  const restore = (fileId?: string) => {
    useDocumentPushStore.getState().setAttended(chatId, true)
    if (!fileId || fileId === active?.fileId) { store.restore(chatId, fileId); return }
    void leaveDocumentPane(chatId).then((ok) => store.restore(chatId, ok ? fileId : undefined))
  }
  return {
    chips: tabs.slice(0, 4).map((t) => ({ fileId: t.fileId, title: t.filename || 'Document' })),
    more: Math.max(0, tabs.length - 4),
    onRestore: restore,
    onClose: (fileId: string) => {
      const close = () => store.closeFile(chatId, fileId, tabs.map((t) => t.fileId))
      if (fileId !== active?.fileId) { close(); return }
      // The document on show may hold unsaved edits: a refused leave brings
      // the pane back with its note.
      void leaveDocumentPane(chatId).then((ok) => (ok ? close() : restore(fileId)))
    },
  }
}
