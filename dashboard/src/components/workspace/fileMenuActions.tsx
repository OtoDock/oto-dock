import type { Dispatch, RefObject, SetStateAction } from 'react'
import type { FileNode } from '../../api/agents'
import { toSandboxVirtualPath } from '../../lib/paths'
import type { MenuAction } from './FileContextMenu'
import type { WorkspaceSection } from './sections'
import { Icon } from './workspaceIcons'
import type { useWorkspaceState } from '../../hooks/useWorkspaceState'

type WSState = ReturnType<typeof useWorkspaceState>['state']
type WSActions = Omit<ReturnType<typeof useWorkspaceState>, 'state'>

interface BuildActionsArgs {
  resolveTargetNodes: (clicked: FileNode) => FileNode[]
  actions: WSActions
  activeSection: WorkspaceSection | null
  handleOpen: (node: FileNode) => void
  setRenamingPath: Dispatch<SetStateAction<string | null>>
  handleDownload: (nodes: FileNode[]) => void
  handlePaste: (destDir: string) => Promise<void>
  queueDelete: (nodes: FileNode[]) => void
}

interface BuildEmptyActionsArgs {
  activeSection: WorkspaceSection | null
  state: WSState
  actions: WSActions
  handleNewFile: (ext: string) => Promise<void>
  handleNewFolder: () => Promise<void>
  handlePaste: (destDir: string) => Promise<void>
  emptyUploadInputRef: RefObject<HTMLInputElement | null>
}

export function buildActions(
  node: FileNode,
  {
    resolveTargetNodes,
    actions,
    activeSection,
    handleOpen,
    setRenamingPath,
    handleDownload,
    handlePaste,
    queueDelete,
  }: BuildActionsArgs,
): MenuAction[] {
  const acts: MenuAction[] = []
  const targets = resolveTargetNodes(node)
  const isBatch = targets.length > 1
  const cb = actions.clipboard
  const canPasteIntoFolder =
    node.type === 'dir' && activeSection?.canWrite && cb && cb.paths.length > 0

  // Open / Preview — single, always operates on the clicked node.
  acts.push({
    key: 'open',
    label: node.type === 'dir' ? 'Open' : 'Preview',
    icon: <Icon name="open" />,
    onClick: () => handleOpen(node),
  })

  // Rename — single only, hidden when batch-selected.
  if (activeSection?.canWrite && !isBatch) {
    acts.push({
      key: 'rename',
      label: 'Rename',
      icon: <Icon name="pencil" />,
      onClick: () => setRenamingPath(node.path),
    })
  }

  // Download — single file → direct, folder or multi → zip.
  const dlLabel = isBatch
    ? `Download ${targets.length} items`
    : node.type === 'dir'
      ? 'Download as zip'
      : 'Download'
  acts.push({
    key: 'download',
    label: dlLabel,
    icon: <Icon name="download" />,
    onClick: () => handleDownload(targets),
  })

  // Copy Path — single only (the clicked node's sandbox-virtual path).
  acts.push({
    key: 'copy-path',
    label: 'Copy Path',
    icon: <Icon name="link" />,
    onClick: () => {
      const virtual = toSandboxVirtualPath(node.path)
      navigator.clipboard?.writeText(virtual).catch(() => {})
    },
  })

  // Cut / Copy — batch-aware. Both require writability of the SOURCE
  // scope (the active section) so they can be moved/deleted from it.
  if (activeSection?.canWrite) {
    acts.push({
      key: 'cut',
      label: isBatch ? `Cut ${targets.length} items` : 'Cut',
      icon: <Icon name="scissors" />,
      onClick: () =>
        actions.setClipboard(
          'cut',
          targets.map((n) => n.path),
          activeSection.key,
        ),
    })
  }
  acts.push({
    key: 'copy',
    label: isBatch ? `Copy ${targets.length} items` : 'Copy',
    icon: <Icon name="copy" />,
    onClick: () =>
      actions.setClipboard(
        'copy',
        targets.map((n) => n.path),
        activeSection?.key ?? '',
      ),
  })

  // Paste into this folder (folder-only, when clipboard non-empty).
  if (canPasteIntoFolder) {
    acts.push({
      key: 'paste-into',
      label: `Paste ${cb!.paths.length} item${cb!.paths.length === 1 ? '' : 's'} here`,
      icon: <Icon name="clipboard" />,
      onClick: () => void handlePaste(node.path),
    })
  }

  // Delete — batch-aware. Confirm dialog shows the count + first names.
  if (activeSection?.canWrite) {
    acts.push({
      key: 'delete',
      label: isBatch ? `Delete ${targets.length} items` : 'Delete',
      tone: 'danger',
      icon: <Icon name="trash" />,
      onClick: () => queueDelete(targets),
    })
  }
  return acts
}

/** Menu actions for right-click on EMPTY grid/tree background. */
export function buildEmptyActions({
  activeSection,
  state,
  actions,
  handleNewFile,
  handleNewFolder,
  handlePaste,
  emptyUploadInputRef,
}: BuildEmptyActionsArgs): MenuAction[] {
  if (!activeSection) return []
  const dir = state.path || activeSection.pathPrefix
  const cb = actions.clipboard
  const acts: MenuAction[] = []
  if (activeSection.canWrite) {
    acts.push({
      key: 'new-md',
      label: 'New Markdown',
      icon: <Icon name="pencil" />,
      onClick: () => void handleNewFile('.md'),
    })
    acts.push({
      key: 'new-folder',
      label: 'New Folder',
      icon: <Icon name="folder" />,
      onClick: () => void handleNewFolder(),
    })
    acts.push({
      key: 'upload',
      label: 'Upload',
      icon: <Icon name="upload" />,
      onClick: () => emptyUploadInputRef.current?.click(),
    })
  }
  if (cb && cb.paths.length > 0 && activeSection.canWrite) {
    acts.push({
      key: 'paste-here',
      label: `Paste ${cb.paths.length} item${cb.paths.length === 1 ? '' : 's'} here`,
      icon: <Icon name="clipboard" />,
      onClick: () => void handlePaste(dir),
    })
  }
  return acts
}
