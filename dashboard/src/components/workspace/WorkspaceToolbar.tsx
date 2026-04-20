import { useState, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'
import type { WorkspaceView } from '../../hooks/useWorkspaceState'

interface NewFileOption {
  ext: string
  label: string
  icon: string
  color: string
}

const NEW_FILE_OPTIONS: NewFileOption[] = [
  { ext: '.md',   label: 'Markdown',    icon: 'M',  color: 'text-blue-500' },
  { ext: '.txt',  label: 'Text File',   icon: 'T',  color: 'text-gray-500' },
  { ext: '.json', label: 'JSON',        icon: '{}', color: 'text-yellow-600' },
  { ext: '.yaml', label: 'YAML',        icon: 'Y',  color: 'text-purple-500' },
  { ext: '.docx', label: 'Word Doc',    icon: 'W',  color: 'text-blue-600' },
  { ext: '.xlsx', label: 'Spreadsheet', icon: 'X',  color: 'text-green-600' },
  { ext: '.pptx', label: 'Presentation',icon: 'S',  color: 'text-orange-500' },
]

interface Props {
  canWrite: boolean
  view: WorkspaceView
  onChangeView: (v: WorkspaceView) => void
  onNewFile: (ext: string) => void
  onNewFolder: () => void
  onUpload: (files: FileList) => void
  targetDisplay?: string  // e.g. "Saving to: research/2026"
  /** Count of recoverable (removed/replaced) files the caller may restore; the
   * Recover button + badge shows only when > 0 (independent of canWrite — the
   * list endpoint already scopes what each user can recover). */
  recoverCount?: number
  onOpenRecover?: () => void
}

export default function WorkspaceToolbar({
  canWrite,
  view,
  onChangeView,
  onNewFile,
  onNewFolder,
  onUpload,
  targetDisplay,
  recoverCount = 0,
  onOpenRecover,
}: Props) {
  // Anchor coords for the New File dropdown. We render the dropdown via
  // `createPortal` to `document.body` (instead of inline-absolute under the
  // button) because the toolbar uses `overflow-x: auto`, which per CSS spec
  // computes `overflow-y` to `auto` as well — so an absolute-positioned
  // child would get clipped by the toolbar's scroll viewport.
  const [newFileAnchor, setNewFileAnchor] = useState<{ left: number; top: number } | null>(null)
  const newFileButtonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const openNewFileMenu = () => {
    const rect = newFileButtonRef.current?.getBoundingClientRect()
    if (!rect) return
    setNewFileAnchor({ left: rect.left, top: rect.bottom + 4 })
  }
  const closeNewFileMenu = () => setNewFileAnchor(null)

  useEffect(() => {
    if (!newFileAnchor) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (
        menuRef.current && !menuRef.current.contains(target)
        && newFileButtonRef.current && !newFileButtonRef.current.contains(target)
      ) {
        closeNewFileMenu()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [newFileAnchor])

  const btnBase =
    'flex items-center gap-1 px-2 py-1 text-xs rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-p-text hover:bg-p-surface-hover transition-colors shrink-0'

  return (
    <div className="flex items-center gap-1.5 px-3 py-2 overflow-x-auto">
      {canWrite && (
        <>
          {/* + File ▾ — the plus icon already conveys "new", so the label
              stays short to fit mobile widths. The dropdown is portaled
              (see openNewFileMenu) to escape the toolbar's overflow clip. */}
          <button
            ref={newFileButtonRef}
            onClick={() => (newFileAnchor ? closeNewFileMenu() : openNewFileMenu())}
            className={btnBase}
            title="New file"
          >
            <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
            </svg>
            File
            <svg width={10} height={10} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          <button onClick={onNewFolder} className={btnBase} title="New folder">
            <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
            </svg>
            Folder
          </button>
          <button
            onClick={() => fileInputRef.current?.click()}
            className={btnBase}
            title="Upload"
            aria-label="Upload"
          >
            <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4-4m0 0l-4 4m4-4v12" />
            </svg>
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files?.length) onUpload(e.target.files)
              e.target.value = ''
            }}
          />
        </>
      )}

      {targetDisplay && (
        <span className="hidden sm:inline text-[11px] text-p-text-light truncate min-w-0 flex-1">
          {targetDisplay}
        </span>
      )}
      <span className="flex-1" />

      {onOpenRecover && recoverCount > 0 && (
        // Icon-only bin with a count badge. The badge overflows the button's
        // top-right into the toolbar's py-2 padding (still inside the
        // overflow-x-auto scroll box, so it isn't clipped); mr-1 keeps it
        // clear of the view toggle.
        <button
          onClick={onOpenRecover}
          className={`${btnBase} relative mr-1`}
          title={`Recover ${recoverCount} removed or replaced file${recoverCount === 1 ? '' : 's'}`}
          aria-label={`Recycle bin: ${recoverCount} recoverable file${recoverCount === 1 ? '' : 's'}`}
        >
          <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 6h18" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M10 11v6M14 11v6" />
          </svg>
          <span className="absolute -top-1.5 -right-1.5 inline-flex items-center justify-center min-w-[16px] h-4 px-1 rounded-full bg-brand text-white text-[10px] font-semibold pointer-events-none">
            {recoverCount > 99 ? '99+' : recoverCount}
          </span>
        </button>
      )}

      {/* Tree / Grid toggle */}
      <div className="shrink-0 flex items-center rounded-lg border border-p-border-light overflow-hidden">
        <button
          onClick={() => onChangeView('grid')}
          title="Grid view"
          aria-label="Grid view"
          className={`p-1 ${
            view === 'grid'
              ? 'bg-brand-surface text-brand'
              : 'bg-white dark:bg-p-surface text-p-text-secondary hover:bg-p-surface-hover'
          }`}
        >
          <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <rect x="3" y="3" width="7" height="7" rx="1" />
            <rect x="14" y="3" width="7" height="7" rx="1" />
            <rect x="3" y="14" width="7" height="7" rx="1" />
            <rect x="14" y="14" width="7" height="7" rx="1" />
          </svg>
        </button>
        <button
          onClick={() => onChangeView('tree')}
          title="List view"
          aria-label="List view"
          className={`p-1 ${
            view === 'tree'
              ? 'bg-brand-surface text-brand'
              : 'bg-white dark:bg-p-surface text-p-text-secondary hover:bg-p-surface-hover'
          }`}
        >
          <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 6h18M3 12h18M3 18h18" />
          </svg>
        </button>
      </div>

      {newFileAnchor && createPortal(
        <div
          ref={menuRef}
          style={{ position: 'fixed', left: newFileAnchor.left, top: newFileAnchor.top, zIndex: 60 }}
          className="min-w-[160px] bg-white dark:bg-p-surface rounded-lg border border-p-border-light shadow-lg py-1"
        >
          {NEW_FILE_OPTIONS.map((opt) => (
            <button
              key={opt.ext}
              onClick={() => {
                closeNewFileMenu()
                onNewFile(opt.ext)
              }}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-p-text hover:bg-p-surface-hover"
            >
              <span className={`w-4 h-4 flex items-center justify-center text-[9px] font-bold ${opt.color}`}>
                {opt.icon}
              </span>
              <span>{opt.label}</span>
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  )
}
