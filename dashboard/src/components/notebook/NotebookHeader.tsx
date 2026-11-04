import type { ReactNode } from 'react'

/** The fold's header the two Notebook places share: the label and the chevron
 *  on one button, the one-line description under it, `controls` the id of
 *  the section it opens (named while that section is rendered). The sections
 *  start folded: a team's notes grow, and the pages they sit on have more to
 *  show. */
export default function NotebookHeader({ open, onToggle, controls, children }: {
  open: boolean
  onToggle: () => void
  controls: string
  children: ReactNode
}) {
  return (
    <button type="button" className="w-full text-left" aria-expanded={open}
      aria-controls={open ? controls : undefined} onClick={onToggle}>
      <span className="flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-secondary uppercase">Notebook</span>
        <svg
          className={`w-4 h-4 shrink-0 text-tertiary transition-transform duration-fast ease-standard motion-reduce:transition-none ${open ? 'rotate-180' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </span>
      <span className="block text-xs text-tertiary mt-1">{children}</span>
    </button>
  )
}
