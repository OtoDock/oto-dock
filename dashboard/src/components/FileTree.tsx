import { useMemo, useRef, useState } from 'react'
import type { FileNode } from '../api/agents'
import { formatFileSize } from '../lib/fileTypes'
import { encodePathSegments, toSandboxVirtualPath } from '../lib/paths'
import FileIcon, { FolderIcon } from './workspace/FileIcon'

const INTERNAL_PATHS_MIME = 'application/x-otodock-paths'
const INTERNAL_PATH_MIME = 'application/x-otodock-path'

interface FileTreeProps {
  nodes: FileNode[]
  selectedPaths: Set<string>
  cutPaths?: Set<string>
  activeDirPath?: string | null
  agentName?: string
  isDesktop?: boolean
  selectionMode?: boolean
  /** File single-click dispatch — caller picks the reducer action.
   * `range` mode receives the flat visible order so it can compute a slice. */
  onSelectReplace: (path: string) => void
  onSelectToggle: (path: string) => void
  onSelectRange: (target: string, visibleOrder: string[]) => void
  /** Folder click: toggles expand + sets the folder as the active upload dir. */
  onSelectDir?: (path: string) => void
  /** Double-click (desktop) OR single-tap (mobile, no selection mode) on a
   * file. Opens the preview portal. */
  onOpenFile: (path: string) => void
  /** Replaces the internal 2-option menu — right-click and the 3-dot button
   * delegate so the workspace overlay can render its unified `FileContextMenu`.
   * Long-press is routed through `onLongPress` instead (see below). */
  onContextMenu?: (node: FileNode, point: { clientX: number; clientY: number }) => void
  /** Touch long-press handler. When provided, replaces the "open menu" default
   * — the workspace overlay uses this to enter mobile selection mode instead.
   * When omitted, long-press falls through to opening the context menu. */
  onLongPress?: (node: FileNode) => void
  /** Fallback delete callback used only when `onContextMenu` is NOT
   * provided (preserves the legacy menu for non-workspace embeddings). */
  onDelete?: (path: string, type: 'file' | 'dir') => void
  /** Internal drag dropped on a folder row — host calls /move. */
  onMoveDrop?: (destPath: string, srcPaths: string[]) => void
}

// ---------------------------------------------------------------------------
// Visible-row flattening (DFS, honors `expanded` set)
// ---------------------------------------------------------------------------

function flattenVisible(nodes: FileNode[], expanded: Set<string>): string[] {
  const out: string[] = []
  const walk = (n: FileNode) => {
    out.push(n.path)
    if (n.type === 'dir' && expanded.has(n.path) && n.children) {
      for (const c of n.children) walk(c)
    }
  }
  for (const n of nodes) walk(n)
  return out
}

// ---------------------------------------------------------------------------
// SVG icons
// ---------------------------------------------------------------------------

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      className={`w-3.5 h-3.5 text-p-text-light transition-transform duration-150 ${open ? 'rotate-90' : ''}`}
      fill="none" stroke="currentColor" viewBox="0 0 24 24"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
    </svg>
  )
}

const LONG_PRESS_MS = 500

// ---------------------------------------------------------------------------
// Tree node
// ---------------------------------------------------------------------------

interface TreeNodeProps {
  node: FileNode
  depth: number
  selectedPaths: Set<string>
  cutPaths?: Set<string>
  activeDirPath?: string | null
  agentName?: string
  isDesktop: boolean
  selectionMode: boolean
  visibleOrder: string[]
  /** Shared "last clicked path + time" ref — drives the same-path
   * double-click-to-open detection so rapid clicks across DIFFERENT
   * rows never open a file (only two clicks on the same file do). */
  lastClickRef: React.MutableRefObject<{ path: string; time: number } | null>
  onSelectReplace: (path: string) => void
  onSelectToggle: (path: string) => void
  onSelectRange: (target: string, visibleOrder: string[]) => void
  onSelectDir?: (path: string) => void
  onOpenFile: (path: string) => void
  onContextMenu?: (node: FileNode, point: { clientX: number; clientY: number }) => void
  onLongPress?: (node: FileNode) => void
  onDelete?: (path: string, type: 'file' | 'dir') => void
  onMoveDrop?: (destPath: string, srcPaths: string[]) => void
  expanded: Set<string>
  toggleExpand: (path: string) => void
}

const DBLCLICK_MS = 400

function TreeNode(props: TreeNodeProps) {
  const {
    node, depth, selectedPaths, cutPaths, activeDirPath, agentName,
    isDesktop, selectionMode, visibleOrder, lastClickRef,
    onSelectReplace, onSelectToggle, onSelectRange, onSelectDir, onOpenFile,
    onContextMenu, onLongPress, onDelete, onMoveDrop, expanded, toggleExpand,
  } = props

  const [legacyMenuOpen, setLegacyMenuOpen] = useState(false)
  // Drag-depth counter for the drop-target highlight. Same reason as
  // FileTile: a plain boolean leaves the ring stuck on because nested
  // child elements bubble dragenter/dragleave to this handler.
  const [dragDepth, setDragDepth] = useState(0)
  const isDragHover = dragDepth > 0
  const longPressTimer = useRef<number | null>(null)
  const touchStart = useRef<{ x: number; y: number } | null>(null)

  const isDir = node.type === 'dir'
  const isExpanded = expanded.has(node.path)
  const isSelected = selectedPaths.has(node.path)
  const isCut = cutPaths?.has(node.path) ?? false
  const isDirActive = isDir && node.path === activeDirPath
  const isEmpty = isDir && (!node.children || node.children.length === 0)
  const indent = depth * 16 + 4
  const isAutoContext = !isDir && node.path.startsWith('config/context/')

  const legacyCanDelete = onDelete && (isDir ? isEmpty : true)
  const showLegacyMenu = !onContextMenu && (!isDir || legacyCanDelete)

  const openMenuAt = (point: { clientX: number; clientY: number }) => {
    if (onContextMenu) onContextMenu(node, point)
    else setLegacyMenuOpen(true)
  }

  const handleRowContextMenu = (e: React.MouseEvent) => {
    // Stop propagation so the FileTree wrapper's empty-space onContextMenu
    // never fires on a row click. The wrapper intentionally lacks a strict
    // target check so right-clicks in the empty area below the tree still
    // trigger the empty menu; that only works if row clicks don't bubble.
    e.preventDefault()
    e.stopPropagation()
    openMenuAt({ clientX: e.clientX, clientY: e.clientY })
  }
  const cancelLongPress = () => {
    if (longPressTimer.current !== null) {
      window.clearTimeout(longPressTimer.current)
      longPressTimer.current = null
    }
    touchStart.current = null
  }
  const handleTouchStart = (e: React.TouchEvent) => {
    const t = e.touches[0]
    if (!t) return
    touchStart.current = { x: t.clientX, y: t.clientY }
    longPressTimer.current = window.setTimeout(() => {
      // Host-provided long-press wins (workspace overlay routes it to
      // enter-selection-mode). Without it, fall back to opening the
      // unified context menu at the touch coords.
      if (onLongPress) onLongPress(node)
      else openMenuAt({ clientX: t.clientX, clientY: t.clientY })
      longPressTimer.current = null
    }, LONG_PRESS_MS)
  }
  const handleTouchMove = (e: React.TouchEvent) => {
    if (!touchStart.current) return
    const t = e.touches[0]
    if (!t) return
    const dx = Math.abs(t.clientX - touchStart.current.x)
    const dy = Math.abs(t.clientY - touchStart.current.y)
    if (dx > 8 || dy > 8) cancelLongPress()
  }

  // ---- Drag source ----
  const handleDragStart = (e: React.DragEvent) => {
    if (!isDesktop) return
    const sel = Array.from(selectedPaths)
    const draggedPaths =
      sel.length > 1 && selectedPaths.has(node.path) ? sel : [node.path]
    e.dataTransfer.setData(INTERNAL_PATHS_MIME, JSON.stringify(draggedPaths))
    const singleVirtual = toSandboxVirtualPath(node.path)
    e.dataTransfer.setData(INTERNAL_PATH_MIME, singleVirtual)
    e.dataTransfer.setData('text/plain', singleVirtual)
    e.dataTransfer.effectAllowed = 'copyMove'
  }

  // ---- Drop target (folder rows only) ----
  const isInternalDrag = (e: React.DragEvent) =>
    Array.from(e.dataTransfer.types).includes(INTERNAL_PATHS_MIME)
  const handleDragEnter = (e: React.DragEvent) => {
    if (!isDir || !onMoveDrop || !isInternalDrag(e)) return
    e.preventDefault()
    setDragDepth((d) => d + 1)
  }
  const handleDragOver = (e: React.DragEvent) => {
    if (!isDir || !onMoveDrop || !isInternalDrag(e)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
  }
  const handleDragLeave = (e: React.DragEvent) => {
    if (!isDir || !onMoveDrop || !isInternalDrag(e)) return
    setDragDepth((d) => Math.max(0, d - 1))
  }
  const handleDrop = (e: React.DragEvent) => {
    if (!isDir || !onMoveDrop || !isInternalDrag(e)) return
    e.preventDefault()
    setDragDepth(0)
    try {
      const raw = e.dataTransfer.getData(INTERNAL_PATHS_MIME)
      if (!raw) return
      const paths: unknown = JSON.parse(raw)
      if (!Array.isArray(paths)) return
      const srcPaths = paths.filter((p): p is string => typeof p === 'string')
      if (srcPaths.some((p) => p === node.path || node.path.startsWith(p + '/'))) {
        return
      }
      if (srcPaths.length > 0) onMoveDrop(node.path, srcPaths)
    } catch {
      // Bad payload — ignore.
    }
  }

  const dispatchFileClick = (e: React.MouseEvent) => {
    // Mobile: tap = open in normal mode, tap = toggle in selection mode.
    if (!isDesktop) {
      if (selectionMode) onSelectToggle(node.path)
      else onOpenFile(node.path)
      return
    }
    if (e.shiftKey) {
      onSelectRange(node.path, visibleOrder)
      lastClickRef.current = null
      return
    }
    if (e.metaKey || e.ctrlKey || selectionMode) {
      onSelectToggle(node.path)
      lastClickRef.current = null
      return
    }
    // Plain click — open only when the previous click was on the SAME row
    // (otherwise rapid clicks across different rows would spuriously open
    // a file, which was exactly the user-reported bug).
    const now = Date.now()
    const prev = lastClickRef.current
    if (prev && prev.path === node.path && now - prev.time < DBLCLICK_MS) {
      onOpenFile(node.path)
      lastClickRef.current = null
      return
    }
    lastClickRef.current = { path: node.path, time: now }
    onSelectReplace(node.path)
  }

  return (
    <>
      <div
        draggable={isDesktop}
        onDragStart={handleDragStart}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onContextMenu={handleRowContextMenu}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={cancelLongPress}
        onTouchCancel={cancelLongPress}
        className={`group relative flex items-center h-[26px] cursor-pointer transition-colors duration-75
          ${isSelected
            ? 'bg-brand/10 dark:bg-brand/20 text-brand'
            : isDragHover
              ? 'bg-brand/15 ring-1 ring-brand ring-inset'
              : isDirActive
                ? 'bg-amber-50 dark:bg-amber-900/15 text-amber-700 dark:text-amber-400'
                : 'text-p-text hover:bg-p-surface-hover dark:hover:bg-gray-800/60'
          } ${isCut ? 'opacity-50' : ''}`}
      >
        {/* Indent guides */}
        {Array.from({ length: depth }, (_, i) => (
          <span
            key={i}
            className="absolute top-0 bottom-0 w-px bg-p-border-light/50 dark:bg-gray-700/50"
            style={{ left: `${i * 16 + 12}px` }}
          />
        ))}

        <button
          onClick={(e) => {
            // Stop propagation so a row click doesn't also fire the tree
            // wrapper's empty-area onClick (which clears selection).
            e.stopPropagation()
            if (isDir) {
              toggleExpand(node.path)
              onSelectDir?.(node.path)
            } else {
              dispatchFileClick(e)
            }
          }}
          // Native dblclick is intentionally not wired — same-path open
          // is handled inside `dispatchFileClick` via `lastClickRef` so
          // rapid clicks across different rows can't accidentally open.
          className="flex-1 text-left flex items-center gap-1 min-w-0 h-full"
          style={{ paddingLeft: `${indent}px` }}
        >
          {/* Chevron or spacer */}
          <span className="w-4 h-4 flex items-center justify-center shrink-0">
            {isDir ? <ChevronIcon open={isExpanded} /> : null}
          </span>

          {/* Icon */}
          {isDir ? <FolderIcon open={isExpanded} /> : <FileIcon name={node.name} />}

          {/* Name */}
          <span className={`text-[13px] truncate ${isDir ? 'font-medium' : ''}`}>
            {node.name}
          </span>

          {/* Auto-context indicator — every file under config/context/ */}
          {isAutoContext && (
            <span className="w-1.5 h-1.5 rounded-full bg-brand shrink-0" title="Auto-loaded as context (config/context/)" />
          )}

          {/* File size (files only, on hover) */}
          {!isDir && node.size > 0 && (
            <span className="hidden group-hover:inline ml-auto mr-1 text-[10px] text-p-text-light/60 shrink-0">
              {formatFileSize(node.size)}
            </span>
          )}
        </button>

        {/* Three-dot menu — unified onContextMenu, or legacy popup. */}
        <button
          onClick={(e) => {
            e.stopPropagation()
            const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
            openMenuAt({ clientX: rect.right, clientY: rect.bottom })
          }}
          className="shrink-0 flex md:hidden md:group-hover:flex items-center px-1 h-full text-p-text-light hover:text-p-text-secondary"
          aria-label="More actions"
        >
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 16 16">
            <circle cx="8" cy="3" r="1.5" /><circle cx="8" cy="8" r="1.5" /><circle cx="8" cy="13" r="1.5" />
          </svg>
        </button>

        {showLegacyMenu && legacyMenuOpen && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setLegacyMenuOpen(false)} />
            <div className="absolute right-0 top-6 z-50 w-48 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-p-border-light py-1">
              {!isDir && agentName && (
                <a
                  href={`/v1/agents/${encodeURIComponent(agentName)}/files/${encodePathSegments(node.path)}?download=true&fn=${encodeURIComponent(node.name)}`}
                  download={node.name}
                  onClick={() => setLegacyMenuOpen(false)}
                  className="flex items-center gap-2 w-full px-3 py-1.5 text-xs text-p-text-secondary hover:bg-p-surface-hover"
                >
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                  </svg>
                  Download
                </a>
              )}
              {legacyCanDelete && (
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    setLegacyMenuOpen(false)
                    onDelete!(node.path, node.type as 'file' | 'dir')
                  }}
                  className="flex items-center gap-2 w-full px-3 py-1.5 text-xs text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20"
                >
                  <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 16 16">
                    <path fillRule="evenodd" d="M5 3.25V4H2.75a.75.75 0 000 1.5h.3l.815 8.15A1.5 1.5 0 005.357 15h5.285a1.5 1.5 0 001.493-1.35l.815-8.15h.3a.75.75 0 000-1.5H11v-.75A2.25 2.25 0 008.75 1h-1.5A2.25 2.25 0 005 3.25zm2.25-.75a.75.75 0 00-.75.75V4h3v-.75a.75.75 0 00-.75-.75h-1.5z" clipRule="evenodd" />
                  </svg>
                  Delete
                </button>
              )}
            </div>
          </>
        )}
      </div>

      {isDir && isExpanded && node.children && (
        <>
          {node.children.map((child) => (
            <TreeNode
              key={child.path}
              {...props}
              node={child}
              depth={depth + 1}
            />
          ))}
        </>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// FileTree
// ---------------------------------------------------------------------------

export default function FileTree({
  nodes,
  selectedPaths,
  cutPaths,
  activeDirPath,
  agentName,
  isDesktop = true,
  selectionMode = false,
  onSelectReplace,
  onSelectToggle,
  onSelectRange,
  onSelectDir,
  onOpenFile,
  onContextMenu,
  onLongPress,
  onDelete,
  onMoveDrop,
}: FileTreeProps) {
  // Auto-expand folders along the active path so switching from grid mode
  // doesn't drop the user at a collapsed root.
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    const initial = new Set<string>()
    if (activeDirPath) {
      const parts = activeDirPath.split('/')
      let acc = ''
      for (const p of parts) {
        acc = acc ? `${acc}/${p}` : p
        initial.add(acc)
      }
    }
    return initial
  })

  const toggleExpand = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const visibleOrder = useMemo(
    () => flattenVisible(nodes, expanded),
    [nodes, expanded],
  )

  // Shared across all TreeNodes so a fast pair of clicks on the SAME row
  // opens, but two clicks across different rows just re-select.
  const lastClickRef = useRef<{ path: string; time: number } | null>(null)

  if (nodes.length === 0) {
    return (
      <div className="text-xs text-p-text-light px-3 py-2">Empty folder.</div>
    )
  }

  return (
    <div className="py-0.5 select-none">
      {nodes.map((node) => (
        <TreeNode
          key={node.path}
          node={node}
          depth={0}
          selectedPaths={selectedPaths}
          cutPaths={cutPaths}
          activeDirPath={activeDirPath}
          agentName={agentName}
          isDesktop={isDesktop}
          selectionMode={selectionMode}
          visibleOrder={visibleOrder}
          lastClickRef={lastClickRef}
          onSelectReplace={onSelectReplace}
          onSelectToggle={onSelectToggle}
          onSelectRange={onSelectRange}
          onSelectDir={onSelectDir}
          onOpenFile={onOpenFile}
          onContextMenu={onContextMenu}
          onLongPress={onLongPress}
          onDelete={onDelete}
          onMoveDrop={onMoveDrop}
          expanded={expanded}
          toggleExpand={toggleExpand}
        />
      ))}
    </div>
  )
}
