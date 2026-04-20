import type { Clipboard } from '../../hooks/useWorkspaceState'

interface Props {
  clipboard: Clipboard
  onPasteHere?: () => void
  onClear: () => void
}

/**
 * Small pill that surfaces the per-agent clipboard state. Shown in the
 * workspace toolbar whenever `clipboard` is non-null. The "paste here"
 * shortcut is exposed inline because mobile users rarely have a keyboard
 * for Ctrl+V — desktop users keep using the shortcut.
 */
export default function ClipboardIndicator({ clipboard, onPasteHere, onClear }: Props) {
  const count = clipboard.paths.length
  const verb = clipboard.mode === 'cut' ? 'cut' : 'copied'
  return (
    <div className="shrink-0 flex items-center gap-1 px-2 py-0.5 text-[11px] rounded-full bg-brand/10 text-brand border border-brand/30">
      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
        <rect x="8" y="4" width="8" height="3" rx="1" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M5 7h2v13a1 1 0 001 1h8a1 1 0 001-1V7h2" />
      </svg>
      <span className="font-medium">
        {count} {verb}
      </span>
      {onPasteHere && (
        <button
          onClick={onPasteHere}
          title="Paste here"
          className="ml-1 px-1.5 py-px rounded-sm bg-brand/20 hover:bg-brand/30 text-[10px]"
        >
          Paste here
        </button>
      )}
      <button
        onClick={onClear}
        title="Clear clipboard"
        aria-label="Clear clipboard"
        className="ml-0.5 w-4 h-4 flex items-center justify-center rounded-full hover:bg-brand/20"
      >
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  )
}
