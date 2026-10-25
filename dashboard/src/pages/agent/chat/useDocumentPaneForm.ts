/**
 * The form the chat's document pane takes (ChatDocumentPane.tsx): docked as
 * the right half of the chat page with room for two halves and a mouse, a
 * window over the chat's slot on tablets, phones and terminal chats.
 */
import { useState, useSyncExternalStore } from 'react'
import { useDocumentPushStore } from '../../../store/documentPaneStore'

export type DocumentPaneForm = 'docked' | 'floating'

/** Read on the first render, so a phone never paints a docked pane first. */
function formNow(terminal: boolean): DocumentPaneForm {
  if (terminal || typeof window === 'undefined') return 'floating'
  const coarse = window.matchMedia?.('(pointer: coarse)').matches ?? false
  return !coarse && window.innerWidth >= 1024 ? 'docked' : 'floating'
}

// The viewport facts the form reads: the window's width and the pointer.
function subscribeViewport(notify: () => void): () => void {
  window.addEventListener('resize', notify)
  const pointer = window.matchMedia?.('(pointer: coarse)')
  pointer?.addEventListener?.('change', notify)
  return () => {
    window.removeEventListener('resize', notify)
    pointer?.removeEventListener?.('change', notify)
  }
}

export function useDocumentPaneForm(terminal: boolean, chatId: string | null): DocumentPaneForm {
  const live = useSyncExternalStore(subscribeViewport, () => formNow(terminal), () => 'floating' as const)
  // A change of form remounts the editor: a document with unsaved edits
  // holds the form it has until it is saved.
  const dirty = useDocumentPushStore((s) => !!(chatId && s.dirty[chatId]))
  const [held, setHeld] = useState(live)
  if (!dirty && held !== live) setHeld(live)
  return held
}
