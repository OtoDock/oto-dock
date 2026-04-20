import { useState, useCallback, useMemo, useRef } from 'react'
import type { FileNode } from '../../api/agents'
import FileTile from './FileTile'

interface Props {
  agent: string
  nodes: FileNode[]
  selectedPaths: Set<string>
  cutPaths?: Set<string>
  renamingPath: string | null
  isDesktop: boolean
  selectionMode: boolean
  onSelectReplace: (path: string) => void
  onSelectToggle: (path: string) => void
  onSelectRange: (target: string, visibleOrder: string[]) => void
  onOpen: (node: FileNode) => void
  /** Caller decides whether long-press enters selection mode or opens
   * the context menu (current wiring opens the context menu). FileGrid
   * passes through. */
  onLongPress?: (node: FileNode) => void
  onClearSelection: () => void
  onContextMenu: (node: FileNode, point: { clientX: number; clientY: number }) => void
  onEmptyContextMenu?: (point: { clientX: number; clientY: number }) => void
  /** Drop target — OS files dragged in. Caller handles the upload. */
  onDropFiles?: (files: FileList) => void
  /** Internal drag dropped on a folder tile — caller calls /move. */
  onMoveDrop?: (destPath: string, srcPaths: string[]) => void
  renderRename: (node: FileNode) => React.ReactNode
}

/** Tile grid container with selection clearing on background click and an
 * OS-file drop target with a visible ring during dragover.
 *
 * Owns click-dispatch logic: translates raw click/dblclick events into
 * one of four host callbacks (replace / toggle / range / open) based on
 * modifier keys, desktop vs. touch, and the workspace selection-mode flag.
 */
export default function FileGrid({
  agent,
  nodes,
  selectedPaths,
  cutPaths,
  renamingPath,
  isDesktop,
  selectionMode,
  onSelectReplace,
  onSelectToggle,
  onSelectRange,
  onOpen,
  onLongPress,
  onClearSelection,
  onContextMenu,
  onEmptyContextMenu,
  onDropFiles,
  onMoveDrop,
  renderRename,
}: Props) {
  const selectedArray = useMemo(() => Array.from(selectedPaths), [selectedPaths])
  const [dragDepth, setDragDepth] = useState(0)
  const visibleOrder = useMemo(() => nodes.map((n) => n.path), [nodes])

  const hasExternalFiles = (e: React.DragEvent) => {
    return Array.from(e.dataTransfer.types).includes('Files')
  }

  const onDragEnter = useCallback(
    (e: React.DragEvent) => {
      if (!onDropFiles || !hasExternalFiles(e)) return
      e.preventDefault()
      setDragDepth((d) => d + 1)
    },
    [onDropFiles],
  )
  const onDragLeave = useCallback(
    (e: React.DragEvent) => {
      if (!onDropFiles || !hasExternalFiles(e)) return
      setDragDepth((d) => Math.max(0, d - 1))
    },
    [onDropFiles],
  )
  const onDragOver = useCallback(
    (e: React.DragEvent) => {
      if (!onDropFiles || !hasExternalFiles(e)) return
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'
    },
    [onDropFiles],
  )
  const onDrop = useCallback(
    (e: React.DragEvent) => {
      if (!onDropFiles || !hasExternalFiles(e)) return
      e.preventDefault()
      setDragDepth(0)
      const files = e.dataTransfer.files
      if (files?.length) onDropFiles(files)
    },
    [onDropFiles],
  )

  // Manual "double-click on the SAME path" detection. The native
  // onDoubleClick fires too loosely when the user clicks two adjacent
  // tiles fast — sometimes opening the second file even though the
  // clicks landed on different elements. Tracking the path + timestamp
  // ourselves guarantees we only open when both clicks hit the same tile.
  const DBLCLICK_MS = 400
  const lastClickRef = useRef<{ path: string; time: number } | null>(null)

  const dispatchClick = (node: FileNode, e: React.MouseEvent) => {
    e.stopPropagation()
    // Mobile: tap = open in normal mode, tap = toggle in selection mode.
    if (!isDesktop) {
      if (selectionMode) onSelectToggle(node.path)
      else onOpen(node)
      return
    }
    // Desktop: modifier keys take priority; selection mode falls through to toggle.
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
    // Plain click — check for a same-tile double-click first.
    const now = Date.now()
    const prev = lastClickRef.current
    if (prev && prev.path === node.path && now - prev.time < DBLCLICK_MS) {
      onOpen(node)
      lastClickRef.current = null
      return
    }
    lastClickRef.current = { path: node.path, time: now }
    onSelectReplace(node.path)
  }

  return (
    <div
      onClick={() => {
        if (selectedPaths.size > 0) onClearSelection()
      }}
      onContextMenu={(e) => {
        // Tile clicks call `e.stopPropagation()` in FileTile, so anything
        // that bubbles up to here is genuine empty space — the flex-wrap
        // gaps between tiles, the padding below them, or the inner
        // flex-wrap container itself. No strict-target check needed.
        e.preventDefault()
        onEmptyContextMenu?.({ clientX: e.clientX, clientY: e.clientY })
      }}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDragOver={onDragOver}
      onDrop={onDrop}
      className={`relative h-full overflow-auto p-2 pb-24 transition-colors ${
        dragDepth > 0 ? 'ring-2 ring-brand ring-inset bg-brand/5' : ''
      }`}
    >
      {nodes.length === 0 ? (
        <div className="flex items-center justify-center h-full text-xs text-p-text-light">
          Empty folder.
        </div>
      ) : (
        <div className="flex flex-wrap gap-1 justify-center md:justify-start">
          {nodes.map((node) => (
            <FileTile
              key={node.path}
              agent={agent}
              node={node}
              selected={selectedPaths.has(node.path)}
              cutPending={cutPaths?.has(node.path)}
              renaming={renamingPath === node.path}
              isDesktop={isDesktop}
              selectionMode={selectionMode}
              selectedPaths={selectedArray}
              onMoveDrop={onMoveDrop}
              onClick={(e) => dispatchClick(node, e)}
              onDoubleClick={() => { /* superseded by same-path click counter in dispatchClick */ }}
              onContextMenu={(p) => onContextMenu(node, p)}
              onLongPress={() => onLongPress?.(node)}
              renameSlot={renderRename(node)}
            />
          ))}
        </div>
      )}
    </div>
  )
}
