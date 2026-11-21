import { createPortal } from 'react-dom'
import { usePortalMenu } from '@/hooks/usePortalMenu'
import { MENU_ITEM, POPOVER } from '../../ui/classes'
import { FORMAT_WORD, type DownloadFormat } from './documentDownloads'

interface Props {
  /** A document is on show. */
  hasDocument: boolean
  /** A plain Download is possible (the live file's link, or a version). */
  canDownload: boolean
  /** The conversions the document offers, in the menu's order. */
  conversions: Array<'pdf' | 'csv'>
  /** A download is being prepared: the download rows wait. */
  busy: boolean
  onRefresh: () => void
  onDownload: (format: DownloadFormat) => void
}

/**
 * The three-dots menu of the document pane's header, just left of its
 * Close: Refresh, Download (the document on show as it is), and Download as
 * PDF and Download as CSV where the document offers them. On
 * `usePortalMenu`, closed as the Versions menu is. While a download is
 * prepared the button shows a spinner and says so.
 */
export default function DocumentPaneMenu({
  hasDocument, canDownload, conversions, busy, onRefresh, onDownload,
}: Props) {
  const { open, buttonRef, panelRef, style, portalRoot, close, toggle, onKeyDown } = usePortalMenu({
    align: 'right', closeOnMove: true,
  })
  // The focus goes back to the button before the row acts, so a toast or a
  // leave note it causes finds the page where the person was.
  const pick = (act: () => void) => () => {
    close()
    act()
  }
  const row = `${MENU_ITEM} focus-visible:bg-surface-hover focus:outline-none disabled:cursor-not-allowed disabled:text-tertiary disabled:hover:bg-transparent`
  const label = busy ? 'Preparing the download' : 'Document options'

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-busy={busy}
        aria-label={label}
        title={label}
        className="p-1.5 rounded-control text-secondary transition-colors hover:bg-surface-hover hover:text-primary"
      >
        {busy ? (
          <span className="block h-4 w-4 rounded-full border-2 border-action/30 border-t-action animate-spin" aria-hidden="true" />
        ) : (
          <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" />
          </svg>
        )}
      </button>
      {open && createPortal(
        <div
          ref={panelRef}
          role="menu"
          aria-label="Document options"
          onKeyDown={onKeyDown}
          style={style}
          className={`w-52 py-1 ${POPOVER}`}
        >
          <button type="button" role="menuitem" className={row} disabled={!hasDocument} onClick={pick(onRefresh)}>
            Refresh
          </button>
          <button type="button" role="menuitem" className={row} disabled={!hasDocument || !canDownload || busy}
            onClick={pick(() => onDownload(''))}>
            Download
          </button>
          {conversions.map((format) => (
            <button key={format} type="button" role="menuitem" className={row} disabled={!hasDocument || busy}
              onClick={pick(() => onDownload(format))}>
              Download as {FORMAT_WORD[format]}
            </button>
          ))}
        </div>,
        portalRoot,
      )}
    </>
  )
}
