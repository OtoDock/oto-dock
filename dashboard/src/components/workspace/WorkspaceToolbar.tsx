import { useRef } from 'react'
import { createPortal } from 'react-dom'
import { usePortalMenu } from '../../hooks/usePortalMenu'
import type { WorkspaceView } from '../../hooks/useWorkspaceState'
import { canPickFolder, FOLDER_PICKER_ATTRS } from '../../lib/droppedItems'
import { MENU_ITEM, POPOVER } from '../ui/classes'

interface NewFileOption {
  ext: string
  label: string
  icon: string
  color: string
}

// The file letters as in lib/fileTypes.ts: documents primary, text and config secondary.
const NEW_FILE_OPTIONS: NewFileOption[] = [
  { ext: '.md',   label: 'Markdown',    icon: 'M',  color: 'text-primary' },
  { ext: '.txt',  label: 'Text File',   icon: 'T',  color: 'text-secondary' },
  { ext: '.json', label: 'JSON',        icon: '{}', color: 'text-secondary' },
  { ext: '.yaml', label: 'YAML',        icon: 'Y',  color: 'text-secondary' },
  { ext: '.docx', label: 'Word Doc',    icon: 'W',  color: 'text-primary' },
  { ext: '.xlsx', label: 'Spreadsheet', icon: 'X',  color: 'text-primary' },
  { ext: '.pptx', label: 'Presentation',icon: 'S',  color: 'text-primary' },
]

interface Props {
  canWrite: boolean
  view: WorkspaceView
  onChangeView: (v: WorkspaceView) => void
  onNewFile: (ext: string) => void
  onNewFolder: () => void
  onUpload: (files: FileList) => void
  /** Upload a whole folder; offered where the browser has a folder picker
   * (`canPickFolder`). */
  onUploadFolder?: (files: FileList) => void
  targetDisplay?: string  // e.g. "Saving to: research/2026"
  /** Count of recoverable (removed/replaced) files the caller may restore; the
   * Recover button + badge shows only when > 0 (independent of canWrite — the
   * list endpoint already scopes what each user can recover). */
  recoverCount?: number
  onOpenRecover?: () => void
  /** Active uploads/syncs for the CURRENT section. The transfer icon renders
   * only while > 0 (mirrors the recover-bin gating) and pulses while any
   * transfer is genuinely moving (vs merely lingering done/failed). */
  transferCount?: number
  transferActive?: boolean
  /** Click handler receives the anchor point for the portaled popup. */
  onOpenTransfers?: (anchor: { left: number; top: number }) => void
  /** Per-section search toggle (Phase G): the magnifier lives here in the
   * right icon group (no toolbar space for an inline input, esp. mobile);
   * the host renders the query row underneath while open. */
  searchOpen?: boolean
  onToggleSearch?: () => void
}

export default function WorkspaceToolbar({
  canWrite,
  view,
  onChangeView,
  onNewFile,
  onNewFolder,
  onUpload,
  onUploadFolder: onUploadFolderProp,
  targetDisplay,
  recoverCount = 0,
  onOpenRecover,
  transferCount = 0,
  transferActive = false,
  onOpenTransfers,
  searchOpen = false,
  onToggleSearch,
}: Props) {
  // The New File and Upload dropdowns are portaled to the portal root (the body)
  // (instead of inline-absolute under the button) because the toolbar uses
  // `overflow-x: auto`, which per CSS spec computes `overflow-y` to `auto` as
  // well — so an absolute-positioned child would get clipped by the
  // toolbar's scroll viewport. Left-aligned to the button and clamped to
  // the viewport on both axes; keyboard-usable (`usePortalMenu`).
  const {
    open: newFileOpen, buttonRef: newFileButtonRef, panelRef: newFileMenuRef, style: newFileStyle,
    portalRoot, toggle: toggleNewFileMenu, close: closeNewFileMenu, onKeyDown: onNewFileMenuKey,
  } = usePortalMenu()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  // Upload ▾ (Files / Folder): a browser's file dialog picks files or a
  // folder, never both, so one button offers the two pickers.
  const {
    open: uploadOpen, buttonRef: uploadButtonRef, panelRef: uploadMenuRef, style: uploadStyle,
    toggle: toggleUploadMenu, close: closeUploadMenu, onKeyDown: onUploadMenuKey,
  } = usePortalMenu()
  const onUploadFolder = canPickFolder() ? onUploadFolderProp : undefined

  const onUploadClick = () => {
    if (!onUploadFolder) {
      fileInputRef.current?.click()
      return
    }
    toggleUploadMenu()
  }

  const btnShape = 'flex items-center gap-1 px-2 py-1 text-xs rounded-control border transition-colors shrink-0'
  const btnBase = `${btnShape} border-default bg-surface-card text-primary hover:bg-surface-hover`

  return (
    <div className="flex items-center gap-1.5 px-3 py-2 overflow-x-auto">
      {canWrite && (
        <>
          {/* + File ▾ — the plus icon already conveys "new", so the label
              stays short to fit mobile widths. The dropdown is portaled
              (see openNewFileMenu) to escape the toolbar's overflow clip. */}
          <button
            ref={newFileButtonRef}
            onClick={toggleNewFileMenu}
            className={btnBase}
            title="New file"
            aria-haspopup="menu"
            aria-expanded={newFileOpen}
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
            ref={uploadButtonRef}
            onClick={onUploadClick}
            className={btnBase}
            title="Upload"
            aria-label="Upload"
            aria-haspopup={onUploadFolder ? 'menu' : undefined}
            aria-expanded={onUploadFolder ? uploadOpen : undefined}
          >
            <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4-4m0 0l-4 4m4-4v12" />
            </svg>
            {onUploadFolder && (
              <svg width={10} height={10} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
              </svg>
            )}
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
          {onUploadFolder && (
            <input
              ref={folderInputRef}
              type="file"
              className="hidden"
              {...FOLDER_PICKER_ATTRS}
              onChange={(e) => {
                if (e.target.files?.length) onUploadFolder(e.target.files)
                e.target.value = ''
              }}
            />
          )}
        </>
      )}

      {targetDisplay && (
        <span className="hidden sm:inline text-2xs text-tertiary truncate min-w-0 flex-1">
          {targetDisplay}
        </span>
      )}
      <span className="flex-1" />

      {onOpenTransfers && transferCount > 0 && (
        // Transfer-progress icon (uploads + machine sync for the CURRENT
        // section). Appears only while something is in flight or lingering
        // done/failed; same badge pattern as the recover bin beside it.
        <button
          onClick={(e) => {
            const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect()
            onOpenTransfers({ left: Math.max(8, rect.right - 300), top: rect.bottom + 4 })
          }}
          className={`${btnBase} relative mr-1`}
          title={`${transferCount} transfer${transferCount === 1 ? '' : 's'} in progress`}
          aria-label={`Transfers: ${transferCount} in progress`}
        >
          <svg
            width={14} height={14} viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth={2}
            className={transferActive ? 'animate-pulse text-link' : ''}
          >
            {/* cloud with up/down arrows — upload + sync */}
            <path strokeLinecap="round" strokeLinejoin="round" d="M20 17.6A4.5 4.5 0 0 0 17.5 9h-1.1A6 6 0 1 0 5 15.7" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.5 14.5 12 12l2.5 2.5M12 12v9" />
          </svg>
          <span className="absolute -top-1.5 -right-1.5 inline-flex items-center justify-center min-w-[16px] h-4 px-1 rounded-full bg-action text-action-fg text-3xs font-semibold pointer-events-none">
            {transferCount > 99 ? '99+' : transferCount}
          </span>
        </button>
      )}

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
          <span className="absolute -top-1.5 -right-1.5 inline-flex items-center justify-center min-w-[16px] h-4 px-1 rounded-full bg-action text-action-fg text-3xs font-semibold pointer-events-none">
            {recoverCount > 99 ? '99+' : recoverCount}
          </span>
        </button>
      )}

      {onToggleSearch && (
        <button
          onClick={onToggleSearch}
          className={`${searchOpen ? `${btnShape} border-action bg-action-subtle text-link` : btnBase} mr-1`}
          title={searchOpen ? 'Close search' : 'Search this section'}
          aria-label={searchOpen ? 'Close search' : 'Search this section'}
        >
          <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <circle cx="11" cy="11" r="7" />
            <path strokeLinecap="round" d="m21 21-4.35-4.35" />
          </svg>
        </button>
      )}

      {/* Tree / Grid toggle */}
      <div className="shrink-0 flex items-center rounded-control border border-default overflow-hidden">
        <button
          onClick={() => onChangeView('grid')}
          title="Grid view"
          aria-label="Grid view"
          className={`p-1 ${
            view === 'grid'
              ? 'bg-action-subtle text-link'
              : 'bg-surface-card text-secondary hover:bg-surface-hover'
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
              ? 'bg-action-subtle text-link'
              : 'bg-surface-card text-secondary hover:bg-surface-hover'
          }`}
        >
          <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 6h18M3 12h18M3 18h18" />
          </svg>
        </button>
      </div>

      {newFileOpen && createPortal(
        <div
          ref={newFileMenuRef}
          role="menu"
          aria-label="New file"
          onKeyDown={onNewFileMenuKey}
          style={newFileStyle}
          className={`min-w-[160px] py-1 ${POPOVER}`}
        >
          {NEW_FILE_OPTIONS.map((opt) => (
            <button
              key={opt.ext}
              type="button"
              role="menuitem"
              onClick={() => {
                closeNewFileMenu()
                onNewFile(opt.ext)
              }}
              className={`${MENU_ITEM} focus-visible:bg-surface-hover focus-visible:text-primary focus:outline-none`}
            >
              <span className={`w-4 h-4 flex items-center justify-center text-3xs font-bold ${opt.color}`}>
                {opt.icon}
              </span>
              <span>{opt.label}</span>
            </button>
          ))}
        </div>,
        portalRoot,
      )}
      {uploadOpen && createPortal(
        <div
          ref={uploadMenuRef}
          role="menu"
          aria-label="Upload"
          onKeyDown={onUploadMenuKey}
          style={uploadStyle}
          className={`min-w-[140px] py-1 ${POPOVER}`}
        >
          {([
            ['Files', fileInputRef],
            ['Folder', folderInputRef],
          ] as const).map(([label, ref]) => (
            <button
              key={label}
              type="button"
              role="menuitem"
              onClick={() => {
                closeUploadMenu()
                ref.current?.click()
              }}
              className={`${MENU_ITEM} focus-visible:bg-surface-hover focus-visible:text-primary focus:outline-none`}
            >
              {label}
            </button>
          ))}
        </div>,
        portalRoot,
      )}
    </div>
  )
}
