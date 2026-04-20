import { useEffect } from 'react'
import type { FileNode } from '../../api/agents'
import type { WorkspaceSection } from './sections'
import type { useWorkspaceState } from '../../hooks/useWorkspaceState'

type WSState = ReturnType<typeof useWorkspaceState>['state']
type WSActions = Omit<ReturnType<typeof useWorkspaceState>, 'state'>

interface WorkspaceKeyboardShortcutsArgs {
  state: WSState
  actions: WSActions
  tree: FileNode[]
  findNode: (nodes: FileNode[], path: string) => FileNode | null
  queueDelete: (nodes: FileNode[]) => void
  children: FileNode[]
  activeSection: WorkspaceSection | null
  handlePaste: (destDir: string) => Promise<void>
}

export function useWorkspaceKeyboardShortcuts({
  state,
  actions,
  tree,
  findNode,
  queueDelete,
  children,
  activeSection,
  handlePaste,
}: WorkspaceKeyboardShortcutsArgs) {
  useEffect(() => {
    if (!state.open) return
    const isEditable = () => {
      const el = document.activeElement as HTMLElement | null
      if (!el) return false
      const tag = el.tagName
      return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable
    }
    const handler = (e: KeyboardEvent) => {
      if (isEditable()) return
      const meta = e.ctrlKey || e.metaKey
      const sel = state.selected
      const cb = actions.clipboard

      // Delete / Backspace — backspace also fires browser-back on some
      // setups, but only when focus is on a non-editable element; React
      // Router takes precedence on routes anyway.
      if ((e.key === 'Delete' || e.key === 'Backspace') && sel.length > 0) {
        e.preventDefault()
        const nodes = sel
          .map((p) => findNode(tree, p))
          .filter((n): n is FileNode => n !== null)
        if (nodes.length > 0) queueDelete(nodes)
        return
      }

      if (!meta) return

      // Ctrl/Cmd+A — select all visible items in current folder (grid view).
      // For tree view, "visible" means the flat expanded order; we don't have
      // that here without coupling, so we fall back to the same `children`
      // list — good enough for v1 since most multi-select happens in grid.
      if (e.key === 'a' || e.key === 'A') {
        if (children.length === 0) return
        e.preventDefault()
        const allPaths = children.map((n) => n.path)
        actions.setSelection(allPaths, children[children.length - 1].path)
        return
      }

      // Ctrl/Cmd+X — cut
      if ((e.key === 'x' || e.key === 'X') && sel.length > 0 && activeSection?.canWrite) {
        e.preventDefault()
        actions.setClipboard('cut', sel, activeSection.key)
        return
      }

      // Ctrl/Cmd+C — copy
      if ((e.key === 'c' || e.key === 'C') && sel.length > 0) {
        e.preventDefault()
        actions.setClipboard('copy', sel, activeSection?.key ?? '')
        return
      }

      // Ctrl/Cmd+V — paste into the current folder
      if ((e.key === 'v' || e.key === 'V') && cb && cb.paths.length > 0 && activeSection?.canWrite) {
        e.preventDefault()
        const dest = state.path || activeSection.pathPrefix
        void handlePaste(dest)
        return
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [
    state.open, state.selected, state.path, children, tree,
    activeSection, actions, queueDelete, handlePaste,
  ])
}
