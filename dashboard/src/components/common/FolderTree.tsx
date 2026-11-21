import type { FolderRow } from '../../lib/layout/knowledgeFolders'

/**
 * A folder tree to pick from, browsed and picked like the workspace list:
 * one row per folder, indented by its depth, in a scrolling box. The share
 * form picks one folder; the External identities card picks several
 * (`multiple`). A row that cannot be picked carries its reason as its title,
 * and a short tag when the caller gives one.
 */
export default function FolderTree({
  rows, label, selected, onPick, refusal, tag, rootLabel, multiple = false,
}: {
  rows: FolderRow[]
  /** The list's accessible name. */
  label: string
  selected: (path: string) => boolean
  onPick: (path: string) => void
  /** Why a row cannot be picked, or null. */
  refusal?: (path: string) => string | null
  tag?: (path: string) => string | null
  /** The name of the row whose path is `''`. */
  rootLabel?: string
  multiple?: boolean
}) {
  return (
    <div
      role="listbox"
      aria-label={label}
      aria-multiselectable={multiple || undefined}
      className="max-h-44 overflow-y-auto rounded-card border border-subtle divide-y divide-subtle"
    >
      {rows.map(({ path, depth }) => {
        const refused = refusal?.(path) ?? null
        const on = selected(path)
        const shownTag = tag?.(path) ?? null
        return (
          <button
            key={path || '(root)'}
            type="button"
            role="option"
            aria-selected={on}
            aria-disabled={multiple && refused ? true : undefined}
            disabled={!multiple && !!refused}
            onClick={() => { if (!refused) onPick(path) }}
            title={refused ?? undefined}
            style={{ paddingLeft: `${0.75 + depth * 1.1}rem` }}
            className={`w-full flex items-center gap-2 py-1.5 pr-3 text-sm text-left transition-colors ${
              on ? 'bg-action-subtle text-link' : 'text-primary hover:bg-surface-hover'
            } disabled:opacity-45 disabled:cursor-not-allowed aria-disabled:opacity-60 aria-disabled:cursor-not-allowed cursor-pointer`}
          >
            <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
            </svg>
            <span className={path ? 'font-mono' : ''}>
              {path || rootLabel}
            </span>
            {multiple && on && (
              <svg aria-hidden="true" className="ml-auto w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
              </svg>
            )}
            {shownTag && (
              // aria-hidden: the option's accessible name stays the bare
              // path (the title carries why).
              <span aria-hidden="true" className="ml-auto text-3xs text-tertiary shrink-0">{shownTag}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}
