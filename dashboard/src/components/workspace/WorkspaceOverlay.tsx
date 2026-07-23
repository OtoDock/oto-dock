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
import { apiFetch } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
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
    'The agent’s configuration folder. prompt.md plus every .md file under context/ is auto-loaded into every session for this agent.',
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
      for (const file of Array.from(files)) {
        const fd = new FormData()
        fd.append('file', file)
        fd.append('agent', agent)
        fd.append('target_dir', dir)
        try {
          await apiFetch('/v1/upload', { method: 'POST', body: fd })
        } catch (e) {
          // eslint-disable-next-line no-console
          console.error('upload failed', file.name, e)
        }
      }
      qc.invalidateQueries({ queryKey: ['agent-files', agent] })
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

  const queueDelete = useCallback((nodes: FileNode[]) => {
    if (nodes.length === 0) return
    const totalDescendants = nodes.reduce(
      (sum, n) => sum + (n.type === 'dir' ? countDescendants(n) : 0),
      0,
    )
    setPendingDelete({ nodes, totalDescendants })
  }, [])

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
      }),
    [activeSection, state.path, actions.clipboard, handleNewFile, handleNewFolder, handlePaste],
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
          />
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
          <div className="flex-1 min-h-0 overflow-hidden relative">
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
            {state.view === 'grid' ? (
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
                  nodes={activeSection.nodes}
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

function countDescendants(node: FileNode): number {
  if (!node.children) return 0
  let total = node.children.length
  for (const c of node.children) {
    if (c.type === 'dir') total += countDescendants(c)
  }
  return total
}
