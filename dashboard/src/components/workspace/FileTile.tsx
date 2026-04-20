import { useRef, useState } from 'react'
import type { FileNode } from '../../api/agents'
import { getFileKind, formatFileSize } from '../../lib/fileTypes'
import { toSandboxVirtualPath } from '../../lib/paths'
import { useFileThumbnail } from '../../hooks/useFileThumbnail'
import FileIcon, { FolderIcon } from './FileIcon'

interface Props {
  agent: string
  node: FileNode
  selected: boolean
  renaming: boolean
  isDesktop: boolean
  /** When the workspace is in mobile-selection-mode, tile taps toggle
   * selection instead of opening files. The flag is also used to make
   * "cut" tiles dim correctly when this tile is in the clipboard. */
  selectionMode?: boolean
  /** True if this tile's path is currently in the workspace clipboard
   * with `mode === 'cut'` — rendered dimmed. */
  cutPending?: boolean
  /** All currently-selected paths. When the dragged tile is in this list
   * AND length > 1, dragstart attaches the whole selection so the user
   * can move all selected items in a single gesture. */
  selectedPaths?: string[]
  /** Called when a folder tile accepts a drop carrying our internal
   * `application/x-otodock-paths` MIME. The host validates loops + calls
   * the move endpoint. */
  onMoveDrop?: (destPath: string, srcPaths: string[]) => void
  onClick: (e: React.MouseEvent) => void
  onDoubleClick: () => void
  onContextMenu: (e: { clientX: number; clientY: number }) => void
  /** Long-press: enters selection mode (mobile). */
  onLongPress?: () => void
  renameSlot?: React.ReactNode
}

/** Custom MIME for internal drag payload — `dataTransfer.types` carries
 * this everywhere the browser allows, so dropzones can detect an internal
 * drag without peeking at the actual data (forbidden mid-drag). */
const INTERNAL_PATHS_MIME = 'application/x-otodock-paths'
const INTERNAL_PATH_MIME = 'application/x-otodock-path'

const LONG_PRESS_MS = 500

export default function FileTile({
  agent,
  node,
  selected,
  renaming,
  isDesktop,
  selectionMode: _selectionMode,
  cutPending,
  selectedPaths,
  onMoveDrop,
  onClick,
  onDoubleClick,
  onContextMenu,
  onLongPress,
  renameSlot,
}: Props) {
  const isDir = node.type === 'dir'
  const kind = isDir ? null : getFileKind(node.name)
  const isImage = kind === 'image'
  const { src: thumbSrc } = useFileThumbnail(agent, node.path, isImage)
  // Drag-depth counter (not a boolean) so the ring clears reliably when
  // the pointer leaves the tile after passing over a child element. A
  // plain boolean stays stuck on because dragenter/dragleave on nested
  // children both bubble to this handler.
  const [dragDepth, setDragDepth] = useState(0)
  const isDragHover = dragDepth > 0

  const pressTimerRef = useRef<number | null>(null)
  const startedAtRef = useRef<{ x: number; y: number } | null>(null)

  const cancelLongPress = () => {
    if (pressTimerRef.current !== null) {
      window.clearTimeout(pressTimerRef.current)
      pressTimerRef.current = null
    }
    startedAtRef.current = null
  }

  const handleTouchStart = (e: React.TouchEvent) => {
    if (!onLongPress) return
    const t = e.touches[0]
    if (!t) return
    startedAtRef.current = { x: t.clientX, y: t.clientY }
    pressTimerRef.current = window.setTimeout(() => {
      onLongPress()
      pressTimerRef.current = null
    }, LONG_PRESS_MS)
  }

  const handleTouchMove = (e: React.TouchEvent) => {
    if (!startedAtRef.current) return
    const t = e.touches[0]
    if (!t) return
    const dx = Math.abs(t.clientX - startedAtRef.current.x)
    const dy = Math.abs(t.clientY - startedAtRef.current.y)
    if (dx > 8 || dy > 8) cancelLongPress()
  }

  const handleDragStart = (e: React.DragEvent) => {
    if (!isDesktop) return
    // If this tile is part of a multi-selection, drag the whole selection.
    // Otherwise drag just this node.
    const draggedPaths =
      selectedPaths && selectedPaths.length > 1 && selectedPaths.includes(node.path)
        ? selectedPaths
        : [node.path]
    // Multi: agent-relative JSON list (for internal drops).
    e.dataTransfer.setData(INTERNAL_PATHS_MIME, JSON.stringify(draggedPaths))
    // Single: sandbox-virtual form (for the chat-textarea drop target).
    const singleVirtual = toSandboxVirtualPath(node.path)
    e.dataTransfer.setData(INTERNAL_PATH_MIME, singleVirtual)
    e.dataTransfer.setData('text/plain', singleVirtual)
    // 'copyMove' lets the browser show a move cursor over our drop zones
    // while still permitting text-only drops on the chat textarea.
    e.dataTransfer.effectAllowed = 'copyMove'
  }

  // ---- Drop target (folder tiles only) ----

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
      // Client-side guard: never drop onto self or a descendant of any source.
      if (srcPaths.some((p) => p === node.path || node.path.startsWith(p + '/'))) {
        return
      }
      if (srcPaths.length > 0) onMoveDrop(node.path, srcPaths)
    } catch {
      // Bad payload — ignore.
    }
  }

  return (
    <div
      draggable={isDesktop && !renaming}
      onDragStart={handleDragStart}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      onContextMenu={(e) => {
        // Stop propagation so the grid's empty-space onContextMenu never
        // fires on a tile click. The grid handler intentionally lacks a
        // strict-target check so right-clicks in the flex-wrap gaps still
        // trigger the empty menu; that only works if tile clicks don't
        // bubble through.
        e.preventDefault()
        e.stopPropagation()
        onContextMenu({ clientX: e.clientX, clientY: e.clientY })
      }}
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={cancelLongPress}
      onTouchCancel={cancelLongPress}
      className={`group relative flex flex-col w-24 h-24 p-1 rounded-lg cursor-pointer select-none transition-colors ${
        selected
          ? 'bg-brand-surface ring-1 ring-brand'
          : isDragHover
            ? 'bg-brand/10 ring-2 ring-brand'
            : 'hover:bg-p-surface-hover'
      } ${cutPending ? 'opacity-50' : ''}`}
      style={{
        WebkitTouchCallout: 'none',
        WebkitUserSelect: 'none',
        ...(cutPending ? { outline: '1px dashed currentColor', outlineOffset: -2 } : {}),
      }}
    >
      {/* Icon / thumbnail area — fixed-size box that the image must fit
          inside (object-contain, never overflowing). */}
      <div className="flex-1 min-h-0 w-full overflow-hidden flex items-center justify-center">
        {isDir ? (
          <FolderIcon open={false} size={44} />
        ) : isImage && thumbSrc ? (
          <img
            src={thumbSrc}
            alt={node.name}
            className="block w-full h-full object-contain rounded-sm"
            draggable={false}
          />
        ) : (
          <FileIcon name={node.name} size={36} />
        )}
      </div>
      {/* Name row + three-dot menu, aligned on the same baseline. */}
      <div className="w-full mt-0.5 flex items-center gap-0.5 h-5">
        <div className="min-w-0 flex-1">
          {renaming ? (
            renameSlot
          ) : (
            <span
              className="block w-full text-center text-[11px] leading-tight text-p-text truncate"
              title={`${node.name}${!isDir && node.size > 0 ? ` — ${formatFileSize(node.size)}` : ''}`}
            >
              {node.name}
            </span>
          )}
        </div>
        {!renaming && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
              onContextMenu({ clientX: rect.right, clientY: rect.bottom })
            }}
            className="shrink-0 w-4 h-4 flex items-center justify-center rounded-sm text-p-text-light hover:text-p-text-secondary opacity-100 md:opacity-0 md:group-hover:opacity-100"
          >
            <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 16 16">
              <circle cx="8" cy="3" r="1.4" /><circle cx="8" cy="8" r="1.4" /><circle cx="8" cy="13" r="1.4" />
            </svg>
          </button>
        )}
      </div>
    </div>
  )
}
