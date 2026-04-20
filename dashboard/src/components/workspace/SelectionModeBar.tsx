interface Props {
  selectedCount: number
  canWrite: boolean
  onCut: () => void
  onCopy: () => void
  onDownload: () => void
  onDelete: () => void
  onDone: () => void
}

/**
 * Mobile-only action bar shown while the user is in selection mode (or
 * has more than zero items selected). Matches the Google Drive / Files
 * pattern: a sticky band above the scope chips with batch actions on the
 * left, a count in the middle, and a Done chip on the right.
 *
 * Desktop users discover the same actions through the right-click menu,
 * so the bar is hidden at `md:` and up to keep the chrome quiet.
 */
export default function SelectionModeBar({
  selectedCount,
  canWrite,
  onCut,
  onCopy,
  onDownload,
  onDelete,
  onDone,
}: Props) {
  const ChipButton = ({
    onClick,
    title,
    disabled,
    tone,
    children,
  }: {
    onClick: () => void
    title: string
    disabled?: boolean
    tone?: 'danger'
    children: React.ReactNode
  }) => (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={`shrink-0 w-9 h-9 flex items-center justify-center rounded-full transition-colors ${
        disabled
          ? 'text-p-text-light/50'
          : tone === 'danger'
            ? 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20'
            : 'text-p-text-secondary hover:bg-p-surface-hover'
      }`}
    >
      {children}
    </button>
  )

  return (
    <div className="md:hidden flex items-center gap-1 px-2 py-1.5 bg-brand text-white">
      <ChipButton onClick={onCut} title="Cut" disabled={!canWrite || selectedCount === 0}>
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <circle cx="6" cy="6" r="3" />
          <circle cx="6" cy="18" r="3" />
          <line x1="20" y1="4" x2="8.12" y2="15.88" />
          <line x1="14.47" y1="14.48" x2="20" y2="20" />
          <line x1="8.12" y1="8.12" x2="12" y2="12" />
        </svg>
      </ChipButton>
      <ChipButton onClick={onCopy} title="Copy" disabled={selectedCount === 0}>
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
        </svg>
      </ChipButton>
      <ChipButton onClick={onDownload} title="Download" disabled={selectedCount === 0}>
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
        </svg>
      </ChipButton>
      <ChipButton
        onClick={onDelete}
        title="Delete"
        disabled={!canWrite || selectedCount === 0}
        tone="danger"
      >
        <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 16 16">
          <path fillRule="evenodd" d="M5 3.25V4H2.75a.75.75 0 000 1.5h.3l.815 8.15A1.5 1.5 0 005.357 15h5.285a1.5 1.5 0 001.493-1.35l.815-8.15h.3a.75.75 0 000-1.5H11v-.75A2.25 2.25 0 008.75 1h-1.5A2.25 2.25 0 005 3.25z" clipRule="evenodd" />
        </svg>
      </ChipButton>
      <span className="flex-1 text-xs text-white/90 text-center font-medium">
        {selectedCount} selected
      </span>
      <button
        onClick={onDone}
        className="shrink-0 px-3 py-1 text-xs font-medium rounded-full bg-white/15 hover:bg-white/25 text-white transition-colors"
      >
        Done
      </button>
    </div>
  )
}
