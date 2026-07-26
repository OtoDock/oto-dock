import { useEffect, useMemo, useState, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  useAgentFiles,
  useCreateAgentDir,
  useCreateAgentFile,
  useDeleteAgentPath,
  useRenameAgentPath,
  useMoveAgentPaths,
  useCopyAgentPaths,
  useZipAgentPaths,
  useRecoverBin,
  type FileNode,
} from '../../api/agents'
import { useAuth } from '../../contexts/AuthContext'
import { uploadWithProgress } from '../../lib/uploadWithProgress'
import { useTransferStore, useTransfersForSection, isTransferActive } from '../../store/transferStore'
import TransferPopup from './TransferPopup'
import { emptyTypeAhead, pushChar, findMatch } from '../../lib/typeAhead'
import { searchSection, pruneTree, expandedDirsOf } from '../../lib/workspaceSearch'
import { Icon } from './workspaceIcons'
import type { useWorkspaceState } from '../../hooks/useWorkspaceState'
import { pushEscHandler } from '../../lib/escStack'
import { parentDir, resolveActionTargets } from '../../lib/paths'
import FileTree from '../FileTree'
import FilePreviewBody from './FilePreviewBody'
import ScopeChips from './ScopeChips'
import WorkspaceBreadcrumb from './WorkspaceBreadcrumb'
import WorkspaceToolbar from './WorkspaceToolbar'
import FileGrid from './FileGrid'
import FileContextMenu, { type MenuAction } from './FileContextMenu'
import DeleteConfirmDialog from './DeleteConfirmDialog'
import RecoverBinModal from './RecoverBinModal'
import InlineRename from './InlineRename'
import SelectionModeBar from './SelectionModeBar'
import ClipboardIndicator from './ClipboardIndicator'
import { buildSections, listChildren, type ScopeKey, type WorkspaceSection } from './sections'
import { onFileUpdate } from '../../lib/fileUpdates'
import { buildActions as buildActionsImpl, buildEmptyActions as buildEmptyActionsImpl } from './fileMenuActions'
import { useWorkspaceKeyboardShortcuts } from './useWorkspaceKeyboardShortcuts'

/** Per-scope plain-text description rendered above the file area as a
 * dismissable-feeling info banner. Kept short so it fits in one line on
 * mobile but explanatory enough that a new user understands why each
 * scope exists. */
const SCOPE_INFO: Record<ScopeKey, string> = {
  'my-workspace':
    'Your personal workspace for day-to-day work with this agent. Files here are visible only to you.',
  'my-context':
    'Your personal context — Markdown / text files here are auto-loaded into every user scoped chat or task you run with this agent.',
  'agent-workspace':
    'The shared collaborative agent workspace where you can share files with other users of this agent. This is the operational working folder for this agent and is used for agent scoped tasks (triggers, schedules, internal-agent runs).',
  'agent-knowledge':
    'The agent’s reference library — docs, templates, and reference material curated by a manager. Not auto-loaded into context — the agent reads files here on demand. Universal (the same files in every session, user-scope or agent-scope).',
  'agent-config':
    'The agent’s configuration folder. agent.md (the persona) plus every .md file under context/ is auto-loaded into every session for this agent.',
}

type WSState = ReturnType<typeof useWorkspaceState>['state']
type WSActions = Omit<ReturnType<typeof useWorkspaceState>, 'state'>

interface Props {
  agent: string
  /** True if the user can manage this agent (admin or per-agent manager). */
  canManage: boolean
  /** True if the user can EDIT this agent (admin, manager, or per-agent
   * editor). Defaults to `canManage` when omitted, preserving the owner-only
   * behavior for callers that haven't been updated yet. */
  canEdit?: boolean
  state: WSState
  actions: WSActions
  /** Add padding above the chip row to clear the chat page's floating
   * TopBar. */
  topPadding?: boolean
  /** Initial scope when none is remembered for this agent. */
  defaultScope?: ScopeKey
  /** Restrict the visible chip set (used by task chats to hide "My"
   * scopes for agent-scope tasks and vice-versa). When omitted all
   * scopes returned by the backend tree filter are shown. */
  allowedScopes?: ScopeKey[]
  /** Deep-link: open the Recover bin modal on mount (a `?recover=1` arrival
   * from a file-conflict notification). Cleared via `onRecoverConsumed`. */
  initialRecover?: boolean
  onRecoverConsumed?: () => void
}

interface ContextMenuPayload {
  /** Null when opened on empty grid background (paste / new-file menu). */
  node: FileNode | null
  point: { clientX: number; clientY: number }
}

interface PendingDelete {
  nodes: FileNode[]
  totalDescendants: number
  /** Files in the selection too large for the Recover bin — their deletion
   * cannot be undone (server skips capture above recover_bin_max_bytes). */
  binSkippedCount: number
}

const IS_DESKTOP = typeof window === 'undefined' ? true : !window.matchMedia('(hover: none)').matches

/**
 * In-chat workspace overlay. Mounted in the message-area slot of AgentChat
 * Owns the file tree fetch, scope chip rendering, grid/tree
 * view, file preview portal, and all the file ops mutations.
 *
 * State (open/scope/path/view/selected/preview/dot) is owned by the parent
 * via `useWorkspaceState`; this component just routes user input through
 * the supplied action callbacks.
 */
export default function WorkspaceOverlay({
  agent,
  canManage,
  canEdit,
  state,
  actions,
  topPadding,
  defaultScope,
  allowedScopes,
  initialRecover,
  onRecoverConsumed,
}: Props) {
  // Editor + manager + admin can write to /workspace/. Default canEdit to
  // canManage when caller hasn't been updated (preserves the owner-only
  // behavior — workspace gated to manager).
  const effectiveCanEdit = canEdit ?? canManage
  const { user } = useAuth()
  const { data: tree = [] } = useAgentFiles(agent)
  const { data: recoverEntries = [] } = useRecoverBin(agent)
  const createDir = useCreateAgentDir()
  const createFile = useCreateAgentFile()
  const deletePath = useDeleteAgentPath()
  const renamePath = useRenameAgentPath()
  const movePaths = useMoveAgentPaths()
  const copyPaths = useCopyAgentPaths()
  const zipPaths = useZipAgentPaths()
  const qc = useQueryClient()

  // When another user changes a shared file for THIS agent
  // (a Collabora save or an agent/disk write), refresh the file tree so
  // new/edited files appear. Any open Collabora preview reloads independently
  // via useCollaboraLiveReload. Safe + non-destructive — just a refetch.
  useEffect(() => {
    return onFileUpdate((u) => {
      if (u.agent_slug === agent) {
        qc.invalidateQueries({ queryKey: ['agent-files', agent] })
      }
    })
  }, [agent, qc])

  const allSections = useMemo(
    () => buildSections(tree, canManage, effectiveCanEdit, user?.username),
    [tree, canManage, effectiveCanEdit, user?.username],
  )
  const sections = useMemo(
    () => (allowedScopes ? allSections.filter((s) => allowedScopes.includes(s.key)) : allSections),
    [allSections, allowedScopes],
  )

  // Pick the section to display: remembered scope > caller default > first.
  const activeSection: WorkspaceSection | null =
    sections.find((s) => s.key === state.scope) ??
    (defaultScope ? sections.find((s) => s.key === defaultScope) : null) ??
    sections[0] ??
    null

  // Sync state.scope to the active section. (Deferred via effect — cannot
  // dispatch during render.)
  useEffect(() => {
    if (!activeSection) return
    if (state.scope !== activeSection.key) {
      actions.setScope(activeSection.key)
    }
  }, [state.scope, activeSection, actions])

  // Folder children at the current path.
  const children = useMemo(() => {
    if (!activeSection) return []
    const path = state.path || activeSection.pathPrefix
    return listChildren(activeSection, path)
  }, [activeSection, state.path])

  // ---- Esc: selection-mode first, then preview, then workspace ----
  // Selection-mode handler is pushed on top of the workspace-close handler
  // so it fires first while the user is in selection mode (LIFO stack).
  useEffect(() => {
    if (!state.open || state.preview) return
    return pushEscHandler(() => actions.closeWorkspace())
  }, [state.open, state.preview, actions])
  useEffect(() => {
    if (!state.selectionMode) return
    return pushEscHandler(() => actions.exitSelectionMode())
  }, [state.selectionMode, actions])

  // ---- Android system back integration for selection mode ----
  // MainActivity reads these globals inside the JS evaluation it runs on
  // every back press; when selection mode is active it calls the exit
  // function and returns early instead of falling through to `history.back()`.
  useEffect(() => {
    const w = window as unknown as {
      __otodockWorkspaceSelectionActive?: boolean
      __otodockWorkspaceExitSelection?: () => void
    }
    if (state.selectionMode) {
      w.__otodockWorkspaceSelectionActive = true
      w.__otodockWorkspaceExitSelection = () => actions.exitSelectionMode()
    } else {
      w.__otodockWorkspaceSelectionActive = false
      delete w.__otodockWorkspaceExitSelection
    }
    return () => {
      w.__otodockWorkspaceSelectionActive = false
      delete w.__otodockWorkspaceExitSelection
    }
  }, [state.selectionMode, actions])

  // ---- Context menu state ----
  const [menu, setMenu] = useState<ContextMenuPayload | null>(null)
  const [renamingPath, setRenamingPath] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null)
  const [showRecover, setShowRecover] = useState(false)
  // ---- Per-section recursive search (Phase G) ----
  // Toggled by the toolbar's magnifier icon; closing ALWAYS clears the query
  // so the full listing returns. Query resets on section switch.
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const searchInputRef = useRef<HTMLInputElement>(null)
  const closeSearch = useCallback(() => {
    setSearchOpen(false)
    setSearchQuery('')
  }, [])
  useEffect(() => {
    closeSearch()
  }, [activeSection?.key, agent, closeSearch])
  const searchActive = searchOpen && searchQuery.trim().length > 0
  const searchResult = useMemo(
    () => (searchActive && activeSection
      ? searchSection(activeSection.nodes, activeSection.pathPrefix, searchQuery)
      : null),
    [searchActive, activeSection, searchQuery],
  )
  const prunedTreeNodes = useMemo(
    () => (searchActive && activeSection
      ? pruneTree(activeSection.nodes, searchQuery)
      : null),
    [searchActive, activeSection, searchQuery],
  )
  const searchExpandedDirs = useMemo(
    () => (prunedTreeNodes ? expandedDirsOf(prunedTreeNodes) : undefined),
    [prunedTreeNodes],
  )

  // ---- Explorer-style type-ahead selection (Phase G) ----
  // DOM-driven so one handler covers BOTH views: visible items carry
  // data-ta-name/path attributes (grid tiles + tree rows) in display order.
  const typeAheadRef = useRef(emptyTypeAhead())
  const fileAreaRef = useRef<HTMLDivElement>(null)

  // Transfer-progress popup (Feature E): anchored under the toolbar icon;
  // items are the CURRENT section's active uploads/machine-syncs.
  const [transferAnchor, setTransferAnchor] = useState<{ left: number; top: number } | null>(null)
  const transfers = useTransfersForSection(
    agent, activeSection?.key ?? '', user?.username,
  )
  // Close the popup when the last item prunes away or the agent changes.
  useEffect(() => {
    if (transfers.length === 0) setTransferAnchor(null)
  }, [transfers.length])
  useEffect(() => {
    setTransferAnchor(null)
  }, [agent])
  // Deep-link: a `?recover=1` arrival (file-conflict notification) opens the
  // Recover bin straight away. Consume the flag so it doesn't re-fire.
  useEffect(() => {
    if (initialRecover) {
      setShowRecover(true)
      onRecoverConsumed?.()
    }
  }, [initialRecover, onRecoverConsumed])
  // Hidden file input used by the empty-space context menu's "Upload" item.
  const emptyUploadInputRef = useRef<HTMLInputElement>(null)

  // ---- Helpers ----

  const handleScopeSelect = useCallback(
    (s: WorkspaceSection) => {
      actions.setScope(s.key)
      actions.setPath(s.pathPrefix)
    },
    [actions],
  )

  const handleNewFile = useCallback(
    async (ext: string) => {
      if (!activeSection || !activeSection.canWrite) return
      const dir = state.path || activeSection.pathPrefix
      const base = window.prompt(`Name for new ${ext.replace('.', '')} file:`)?.trim()
      if (!base) return
      const filename = base.endsWith(ext) ? base : base + ext
      const path = `${dir}/${filename}`
      await createFile.mutateAsync({ agent, path, fileType: ext })
    },
    [activeSection, state.path, agent, createFile],
  )

  const handleNewFolder = useCallback(async () => {
    if (!activeSection || !activeSection.canWrite) return
    const dir = state.path || activeSection.pathPrefix
    const name = window.prompt('Folder name:')?.trim()
    if (!name) return
    const path = `${dir}/${name}`
    await createDir.mutateAsync({ agent, path })
  }, [activeSection, state.path, agent, createDir])

  const handleUpload = useCallback(
    async (files: FileList) => {
      if (!activeSection || !activeSection.canWrite) return
      const dir = state.path || activeSection.pathPrefix
      const ts = useTransferStore.getState()
      // Sequential on purpose: preserves server-side conflict-suffix
      // ordering and keeps failure attribution per-file; the progress list
      // makes the sequencing visible instead of confusing.
      for (const file of Array.from(files)) {
        // NOT crypto.randomUUID(): that's secure-context-only, and plenty of
        // self-hosted installs serve the dashboard over plain http on a LAN.
        const clientId = `local:${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
        ts.beginLocalUpload({
          clientId, agent, targetDir: dir,
          filename: file.name, bytesTotal: file.size,
        })
        try {
          const res = await uploadWithProgress(file, agent, dir, (sent) =>
            useTransferStore.getState().updateLocalUpload(clientId, sent),
          )
          useTransferStore.getState().linkUpload(clientId, {
            transferId: res.transfer_id,
            relPath: res.path,
            remotePush: res.remote_push,
          })
        } catch (e) {
          useTransferStore.getState().failLocalUpload(clientId)
          // eslint-disable-next-line no-console
          console.error('upload failed', file.name, e)
        }
        // Refresh after EACH file so big multi-file batches appear as they
        // land, not only at the end.
        qc.invalidateQueries({ queryKey: ['agent-files', agent] })
      }
    },
    [activeSection, state.path, agent, qc],
  )

  const handleOpen = useCallback(
    (node: FileNode) => {
      if (node.type === 'dir') {
        actions.setPath(node.path)
      } else {
        actions.openPreview(node.path)
      }
    },
    [actions],
  )

  const handleRefresh = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['agent-files', agent] })
    qc.invalidateQueries({ queryKey: ['recover-bin', agent] })
  }, [qc, agent])

  // Explorer-style type-ahead: printable chars accumulate (1s reset) and
  // select the first visible item whose name starts with the buffer;
  // repeating one char cycles its matches; Enter opens; Escape clears.
  // Works in BOTH views via the data-ta-* attributes rendered by grid
  // tiles and tree rows (queried in display order).
  const handleTypeAhead = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (searchOpen || state.preview || showRecover || pendingDelete || menu || renamingPath) return
      const target = e.target as HTMLElement
      if (target.closest('input, textarea, [contenteditable="true"]')) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const container = fileAreaRef.current
      if (!container) return
      if (e.key === 'Escape') {
        typeAheadRef.current = emptyTypeAhead()
        return
      }
      const els = Array.from(
        container.querySelectorAll<HTMLElement>('[data-ta-name]'),
      )
      if (!els.length) return
      const selPath = state.selected.length === 1 ? state.selected[0] : null
      if (e.key === 'Enter' && selPath) {
        const node = findNode(tree, selPath)
        if (node) {
          e.preventDefault()
          handleOpen(node)
        }
        return
      }
      if (e.key.length !== 1) return
      typeAheadRef.current = pushChar(typeAheadRef.current, e.key, Date.now())
      const names = els.map((el) => el.dataset.taName || '')
      const curIdx = selPath
        ? els.findIndex((el) => el.dataset.taPath === selPath)
        : -1
      const idx = findMatch(names, typeAheadRef.current, curIdx)
      if (idx >= 0) {
        e.preventDefault()
        const el = els[idx]
        actions.select(el.dataset.taPath!, true)
        el.scrollIntoView({ block: 'nearest' })
      }
    },
    [
      searchOpen, state.preview, state.selected, showRecover, pendingDelete,
      menu, renamingPath, tree, actions, handleOpen,
    ],
  )

  // ---- Batch action handlers (delete / download / paste) ----

  const downloadSingleFile = useCallback(
    (node: FileNode) => {
      // `?fn=` is read by the Android app's DownloadListener as the
      // authoritative filename (MainActivity.startDownload). Browsers
      // ignore it and rely on the `download` attribute / Content-Disposition.
      const url = `/v1/agents/${encodeURIComponent(agent)}/files/${node.path
        .split('/')
        .map(encodeURIComponent)
        .join('/')}?download=true&fn=${encodeURIComponent(node.name)}`
      const a = document.createElement('a')
      a.href = url
      a.download = node.name
      a.click()
    },
    [agent],
  )

  /** Download: single file → direct attachment; everything else (folder or
   * multi-select) → zip via `/zip` endpoint. */
  const handleDownload = useCallback(
    (nodes: FileNode[]) => {
      if (nodes.length === 1 && nodes[0].type === 'file') {
        downloadSingleFile(nodes[0])
      } else if (nodes.length > 0) {
        zipPaths.mutate({ agent, paths: nodes.map((n) => n.path) })
      }
    },
    [downloadSingleFile, zipPaths, agent],
  )

  /** Drag-to-move target — folder tiles/rows call this when an internal
   * drag is dropped. Validates loops client-side and invokes /move. */
  const handleMoveDrop = useCallback(
    async (destPath: string, srcPaths: string[]) => {
      // Defence in depth: the tile/row already rejects self/descendant
      // drops, but a stale payload could slip through.
      const valid = srcPaths.filter(
        (p) => p !== destPath && !destPath.startsWith(p + '/'),
      )
      if (valid.length === 0) return
      try {
        await movePaths.mutateAsync({ agent, srcPaths: valid, destDir: destPath })
        actions.clearSelection()
        // Any of the moved paths that were also sitting in the clipboard
        // (e.g. the user cut them and then dragged them in the same gesture)
        // would 404 on a subsequent Ctrl+V. Drop them from the clipboard so
        // the user never sees a paste fail because of a now-missing source.
        actions.dropFromClipboard(valid)
      } catch (e) {
        // eslint-disable-next-line no-console
        console.error('move-drop failed', e)
      }
    },
    [movePaths, agent, actions],
  )

  /** Paste into `destDir`. Move if clipboard.mode === 'cut' (clipboard then
   * clears); copy otherwise (clipboard preserved for repeat pastes). */
  const handlePaste = useCallback(
    async (destDir: string) => {
      if (!actions.clipboard) return
      const { mode, paths } = actions.clipboard
      try {
        if (mode === 'cut') {
          await movePaths.mutateAsync({ agent, srcPaths: paths, destDir })
          actions.clearClipboard()
        } else {
          await copyPaths.mutateAsync({ agent, srcPaths: paths, destDir })
        }
        actions.clearSelection()
      } catch (e) {
        // On a cut-mode failure the sources are almost certainly already
        // moved by a prior drag (the common cause is exactly that: user
        // cuts files, then drags them into a folder, then tries Ctrl+V
        // somewhere else). Drop the clipboard so the same paste doesn't
        // keep failing. Copy-mode failures keep the clipboard so the user
        // can retry against a different destination.
        if (mode === 'cut') actions.clearClipboard()
        // eslint-disable-next-line no-console
        console.error('paste failed', e)
      }
    },
    [actions, movePaths, copyPaths, agent],
  )

  const binCap = user?.feature_flags?.recover_bin_max_bytes ?? 100 * 1024 * 1024
  const queueDelete = useCallback((nodes: FileNode[]) => {
    if (nodes.length === 0) return
    const totalDescendants = nodes.reduce(
      (sum, n) => sum + (n.type === 'dir' ? countDescendants(n) : 0),
      0,
    )
    const binSkippedCount = nodes.reduce(
      (sum, n) => sum + countBinSkipped(n, binCap), 0,
    )
    setPendingDelete({ nodes, totalDescendants, binSkippedCount })
  }, [binCap])

  // ---- Context-menu actions ----

  const resolveTargetNodes = useCallback(
    (clicked: FileNode): FileNode[] => {
      const targetPaths = resolveActionTargets(clicked.path, state.selected)
      if (targetPaths.length === 1) return [clicked]
      // Resolve each path back to its node; fall back to the clicked one
      // for any path that's not in the visible tree anymore.
      return targetPaths
        .map((p) => findNode(tree, p) ?? (p === clicked.path ? clicked : null))
        .filter((n): n is FileNode => n !== null)
    },
    [state.selected, tree],
  )

  const buildActions = useCallback(
    (node: FileNode): MenuAction[] =>
      buildActionsImpl(node, {
        resolveTargetNodes,
        actions,
        activeSection,
        handleOpen,
        setRenamingPath,
        handleDownload,
        handlePaste,
        queueDelete,
      }),
    [
      activeSection, actions, handleOpen, handleDownload, handlePaste,
      queueDelete, resolveTargetNodes,
    ],
  )

  const buildEmptyActions = useCallback(
    (): MenuAction[] =>
      buildEmptyActionsImpl({
        activeSection,
        state,
        actions,
        handleNewFile,
        handleNewFolder,
        handlePaste,
        emptyUploadInputRef,
        onRefresh: handleRefresh,
      }),
    [activeSection, state.path, actions.clipboard, handleNewFile, handleNewFolder, handlePaste, handleRefresh],
  )

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return
    const settled = await Promise.allSettled(
      pendingDelete.nodes.map((n) => {
        // Always recurse on directory deletes. The tree depth cap can hide
        // real on-disk contents (`children: []` in the API response while
        // the dir is non-empty), so a `recursive=false` request would 400.
        // The user already confirmed via the dialog — recursion is intent.
        const recursive = n.type === 'dir'
        return deletePath.mutateAsync({ agent, path: n.path, recursive })
      }),
    )
    const failed = settled.filter((r) => r.status === 'rejected').length
    if (failed > 0) {
      // eslint-disable-next-line no-console
      console.warn(`Delete: ${failed} of ${pendingDelete.nodes.length} failed`)
    }
    qc.invalidateQueries({ queryKey: ['agent-files', agent] })
    actions.clearSelection()
    // Drop deleted paths from the clipboard so a follow-up paste doesn't
    // 404 on the now-missing sources.
    actions.dropFromClipboard(pendingDelete.nodes.map((n) => n.path))
    setPendingDelete(null)
  }, [pendingDelete, deletePath, qc, agent, actions])

  const commitRename = useCallback(
    async (oldPath: string, newName: string) => {
      const parent = parentDir(oldPath)
      const newPath = parent ? `${parent}/${newName}` : newName
      await renamePath.mutateAsync({ agent, oldPath, newPath })
      setRenamingPath(null)
    },
    [agent, renamePath],
  )

  // ---- Keyboard shortcuts ----
  //
  // Bindings (all gated on `state.open` AND focus not in a text input so
  // typing into the chat textarea / rename input never triggers a batch op):
  //   Delete / Backspace  → open delete-confirm for current selection
  //   Ctrl/Cmd+X          → cut selection into the per-agent clipboard
  //   Ctrl/Cmd+C          → copy selection
  //   Ctrl/Cmd+V          → paste clipboard into the currently-displayed folder
  //   Ctrl/Cmd+A          → select all visible items in the current folder
  useWorkspaceKeyboardShortcuts({
    state,
    actions,
    tree,
    findNode,
    queueDelete,
    children,
    activeSection,
    handlePaste,
  })

  // ---- Preview portal ----
  const previewNode = useMemo(() => {
    if (!state.preview) return null
    return findNode(tree, state.preview)
  }, [state.preview, tree])

  // Build the FileNode[] for whatever paths are currently selected — used
  // by SelectionModeBar action handlers so they can reuse the same code
  // paths as the context-menu buttons. Declared before the early return
  // below so hook order stays stable (Rules of Hooks).
  const selectionNodes = useMemo<FileNode[]>(() => {
    return state.selected
      .map((p) => findNode(tree, p))
      .filter((n): n is FileNode => n !== null)
  }, [state.selected, tree])

  if (!state.open) return null

  const selectedSet = new Set(state.selected)
  const cutSet = actions.clipboard?.mode === 'cut'
    ? new Set(actions.clipboard.paths)
    : undefined
  const targetDir = state.path || activeSection?.pathPrefix || ''
  const targetDisplay =
    targetDir && activeSection
      ? `Saving to: ${
          targetDir === activeSection.pathPrefix
            ? activeSection.label
            : `${activeSection.label} / ${targetDir.slice(activeSection.pathPrefix.length + 1)}`
        }`
      : undefined

  const showSelectionBar = !IS_DESKTOP && state.selectionMode

  return (
    <div className={`h-full flex flex-col bg-p-bg ${topPadding ? 'pt-14' : ''}`}>
      {showSelectionBar && (
        <SelectionModeBar
          selectedCount={state.selected.length}
          canWrite={!!activeSection?.canWrite}
          onCut={() => {
            if (!activeSection?.canWrite || state.selected.length === 0) return
            actions.setClipboard('cut', state.selected, activeSection.key)
          }}
          onCopy={() => {
            if (state.selected.length === 0) return
            actions.setClipboard('copy', state.selected, activeSection?.key ?? '')
          }}
          onDownload={() => handleDownload(selectionNodes)}
          onDelete={() => queueDelete(selectionNodes)}
          onDone={() => actions.exitSelectionMode()}
        />
      )}
      <ScopeChips
        sections={sections}
        activeKey={(activeSection?.key ?? '') as ScopeKey | ''}
        onSelect={handleScopeSelect}
      />
      {activeSection && (
        <>
          <WorkspaceBreadcrumb
            virtualPrefix={activeSection.virtualPrefix}
            currentPath={state.path}
            scopeRoot={activeSection.pathPrefix}
            scopeLabel={activeSection.label}
            onNavigate={(p) => actions.setPath(p)}
          />
          <div className="border-t border-p-border-light" />
          <WorkspaceToolbar
            canWrite={activeSection.canWrite}
            view={state.view}
            onChangeView={actions.setView}
            onNewFile={handleNewFile}
            onNewFolder={handleNewFolder}
            onUpload={handleUpload}
            targetDisplay={targetDisplay}
            recoverCount={recoverEntries.length}
            onOpenRecover={() => setShowRecover(true)}
            transferCount={transfers.length}
            transferActive={transfers.some((t) => isTransferActive(t))}
            onOpenTransfers={(anchor) =>
              setTransferAnchor((cur) => (cur ? null : anchor))
            }
            searchOpen={searchOpen}
            onToggleSearch={() => {
              if (searchOpen) {
                closeSearch()
              } else {
                setSearchOpen(true)
                // Focus after the row renders.
                setTimeout(() => searchInputRef.current?.focus(), 0)
              }
            }}
          />
          {searchOpen && (
            <div className="flex items-center gap-2 px-3 pb-2">
              <span className="text-p-text-light"><Icon name="search" /></span>
              <input
                ref={searchInputRef}
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    e.stopPropagation()
                    closeSearch()
                  }
                }}
                placeholder={`Search ${activeSection.label}…`}
                className="flex-1 min-w-0 px-2 py-1 text-xs rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-p-text placeholder:text-p-text-light focus:outline-none focus:border-brand/60"
              />
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery('')}
                  className="text-xs text-p-text-light hover:text-p-text"
                  title="Clear"
                >
                  ✕
                </button>
              )}
            </div>
          )}
          {actions.clipboard && (
            <div className="flex items-center px-3 pb-1">
              <ClipboardIndicator
                clipboard={actions.clipboard}
                onPasteHere={
                  activeSection.canWrite
                    ? () => {
                        const dest = state.path || activeSection.pathPrefix
                        void handlePaste(dest)
                      }
                    : undefined
                }
                onClear={() => actions.clearClipboard()}
              />
            </div>
          )}
          <div className="border-t border-p-border-light" />
          <div
            ref={fileAreaRef}
            tabIndex={-1}
            onKeyDown={handleTypeAhead}
            className="flex-1 min-h-0 overflow-hidden relative outline-none"
          >
            {/* One-line description of the active scope, with an info glyph.
                Sits just above the files so users learn what each folder
                is for without leaving the workspace. */}
            <div
              // pr-24 keeps the description text out from under the
              // floating "Done · N" pill (which shows whenever the user
              // is in selection mode).
              className={`flex items-start gap-1.5 px-3 py-1.5 text-[11px] text-p-text-light border-b border-p-border-light/60 bg-p-bg/60 ${
                state.selectionMode ? 'pr-24' : ''
              }`}
            >
              <svg className="w-3.5 h-3.5 mt-px shrink-0 opacity-70" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <circle cx="12" cy="12" r="9" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 8h.01M11 12h1v4h1" />
              </svg>
              <span className="leading-snug">{SCOPE_INFO[activeSection.key]}</span>
            </div>
            {/* Floating "Done" pill — visible whenever the user is in
                selection mode (regardless of how many items are
                selected). Works in both grid and list views. */}
            {state.selectionMode && (
              <button
                onClick={() => actions.exitSelectionMode()}
                className="absolute top-1.5 right-2 z-20 px-2.5 py-1 text-[11px] font-medium rounded-full bg-brand text-white shadow-sm hover:bg-brand-hover transition-colors"
                title="Exit selection mode"
              >
                {state.selected.length > 0 ? `Done · ${state.selected.length}` : 'Done'}
              </button>
            )}
            {state.view === 'grid' && searchActive && searchResult ? (
              <div className="h-full overflow-auto pb-24 px-2 py-1">
                {searchResult.matches.length === 0 ? (
                  <div className="flex items-center justify-center h-24 text-xs text-p-text-light">
                    No matches for “{searchQuery.trim()}”
                  </div>
                ) : (
                  <>
                    {searchResult.matches.map(({ node, parentRel }) => (
                      <button
                        key={node.path}
                        data-ta-name={node.name}
                        data-ta-path={node.path}
                        onClick={() => {
                          if (node.type === 'dir') {
                            closeSearch()
                            actions.setPath(node.path)
                          } else {
                            actions.openPreview(node.path)
                          }
                        }}
                        onContextMenu={(e) => {
                          e.preventDefault()
                          setMenu({ node, point: { clientX: e.clientX, clientY: e.clientY } })
                        }}
                        className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left hover:bg-p-surface-hover min-w-0"
                      >
                        {node.type === 'dir' ? (
                          <span className="text-p-text-secondary shrink-0"><Icon name="folder" /></span>
                        ) : (
                          <svg className="w-3.5 h-3.5 shrink-0 text-p-text-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                            <path strokeLinecap="round" strokeLinejoin="round" d="M14 2v6h6" />
                          </svg>
                        )}
                        <span className="flex-1 min-w-0">
                          <span className="block text-xs text-p-text truncate">{node.name}</span>
                          <span className="block text-[10px] text-p-text-light truncate">
                            {parentRel || '(section root)'}
                          </span>
                        </span>
                      </button>
                    ))}
                    {searchResult.truncated && (
                      <div className="px-2 py-2 text-[11px] text-p-text-light">
                        Showing the first {searchResult.matches.length} matches — refine your search.
                      </div>
                    )}
                  </>
                )}
              </div>
            ) : state.view === 'grid' ? (
              <FileGrid
                agent={agent}
                nodes={children}
                selectedPaths={selectedSet}
                cutPaths={cutSet}
                renamingPath={renamingPath}
                isDesktop={IS_DESKTOP}
                selectionMode={state.selectionMode}
                onSelectReplace={(p) => actions.select(p, true)}
                onSelectToggle={(p) => actions.select(p, false)}
                onSelectRange={(target, visibleOrder) =>
                  actions.rangeSelect(visibleOrder, target)
                }
                onOpen={handleOpen}
                onClearSelection={() => actions.clearSelection()}
                onContextMenu={(node, point) => setMenu({ node, point })}
                onLongPress={(node) => actions.enterSelectionMode(node.path)}
                onDropFiles={
                  activeSection.canWrite
                    ? (files) => void handleUpload(files)
                    : undefined
                }
                onMoveDrop={
                  activeSection.canWrite
                    ? (dest, srcs) => void handleMoveDrop(dest, srcs)
                    : undefined
                }
                onEmptyContextMenu={(point) => setMenu({ node: null, point })}
                renderRename={(node) =>
                  renamingPath === node.path ? (
                    <InlineRename
                      initial={node.name}
                      onCommit={(name) => commitRename(node.path, name)}
                      onCancel={() => setRenamingPath(null)}
                    />
                  ) : null
                }
              />
            ) : (
              <div
                className="h-full overflow-auto pb-24"
                onClick={() => {
                  // Empty-area click clears the current selection, mirroring
                  // grid behaviour. Row clicks call `e.stopPropagation()`
                  // implicitly via the inner `<button>` so this only fires
                  // for clicks in the empty area below / between rows.
                  if (state.selected.length > 0) actions.clearSelection()
                }}
                onContextMenu={(e) => {
                  // Tree rows call `e.stopPropagation()` on their own
                  // contextmenu, so anything that bubbles up here is the
                  // empty area below the tree or the gap between rows.
                  e.preventDefault()
                  setMenu({ node: null, point: { clientX: e.clientX, clientY: e.clientY } })
                }}
              >
                <FileTree
                  // Tree mode shows the FULL section subtree (vscode-style);
                  // folder clicks expand + mark the folder as the active
                  // upload target (state.path), they don't filter the tree.
                  // While SEARCHING: the pruned tree (matches + ancestors,
                  // auto-expanded — VS Code filter behavior).
                  nodes={prunedTreeNodes ?? activeSection.nodes}
                  extraExpanded={searchExpandedDirs}
                  selectedPaths={selectedSet}
                  cutPaths={cutSet}
                  activeDirPath={state.path}
                  agentName={agent}
                  isDesktop={IS_DESKTOP}
                  selectionMode={state.selectionMode}
                  onSelectReplace={(p) => {
                    // In tree mode the user can click files anywhere in
                    // the section. Re-anchor the breadcrumb / active upload
                    // target to the parent of the just-clicked file so the
                    // "paste here" context matches what the user is looking
                    // at — without this, the previously-clicked folder
                    // stays sticky.
                    const parent = parentDir(p)
                    if (parent !== state.path) actions.setPath(parent)
                    actions.select(p, true)
                  }}
                  onSelectToggle={(p) => actions.select(p, false)}
                  onSelectRange={(target, visibleOrder) =>
                    actions.rangeSelect(visibleOrder, target)
                  }
                  onSelectDir={(p) => actions.setPath(p)}
                  onOpenFile={(p) => actions.openPreview(p)}
                  onContextMenu={(node, point) => setMenu({ node, point })}
                  onLongPress={(node) => actions.enterSelectionMode(node.path)}
                  onMoveDrop={
                    activeSection.canWrite
                      ? (dest, srcs) => void handleMoveDrop(dest, srcs)
                      : undefined
                  }
                />
              </div>
            )}
          </div>
        </>
      )}

      {menu && (
        <FileContextMenu
          x={menu.point.clientX}
          y={menu.point.clientY}
          actions={menu.node ? buildActions(menu.node) : buildEmptyActions()}
          onClose={() => setMenu(null)}
        />
      )}
      {/* Hidden file input — wired to the empty-space "Upload" menu item. */}
      <input
        ref={emptyUploadInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => {
          if (e.target.files?.length) void handleUpload(e.target.files)
          e.target.value = ''
        }}
      />
      {pendingDelete && (
        <DeleteConfirmDialog
          names={pendingDelete.nodes.map((n) => n.name)}
          isDir={pendingDelete.nodes.length === 1 && pendingDelete.nodes[0].type === 'dir'}
          childCount={pendingDelete.totalDescendants}
          binSkippedCount={pendingDelete.binSkippedCount}
          pending={deletePath.isPending}
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
      {showRecover && (
        <RecoverBinModal
          agent={agent}
          entries={recoverEntries}
          onClose={() => setShowRecover(false)}
        />
      )}
      {transferAnchor && (
        <TransferPopup
          items={transfers}
          anchor={transferAnchor}
          onClose={() => setTransferAnchor(null)}
        />
      )}
      {previewNode && (
        <FilePreviewBody
          agent={agent}
          node={previewNode}
          canWrite={activeSection?.canWrite ?? false}
          onClose={actions.closePreview}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function findNode(nodes: FileNode[], path: string): FileNode | null {
  for (const n of nodes) {
    if (n.path === path) return n
    if (n.type === 'dir' && n.children && path.startsWith(n.path + '/')) {
      const found = findNode(n.children, path)
      if (found) return found
    }
  }
  return null
}

/** Files at/under `node` too large for the Recover bin (no undo copy). */
function countBinSkipped(node: FileNode, cap: number): number {
  if (node.type !== 'dir') return (node.size ?? 0) > cap ? 1 : 0
  let total = 0
  for (const c of node.children ?? []) total += countBinSkipped(c, cap)
  return total
}

function countDescendants(node: FileNode): number {
  if (!node.children) return 0
  let total = node.children.length
  for (const c of node.children) {
    if (c.type === 'dir') total += countDescendants(c)
  }
  return total
}
