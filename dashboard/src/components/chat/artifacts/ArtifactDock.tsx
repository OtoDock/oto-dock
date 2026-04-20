import type { ArtifactWindow } from '@/hooks/useArtifactWindows'
import type { MessageBlock } from '../types'

/**
 * Minimized interactive-CLI artifact windows, docked as
 * icon-buttons in the page's top-left panel stack — the same idiom as PlanPanel /
 * TodoPanel / WorkflowPanel (and TaskMetadata), appearing BELOW them. Each icon
 * carries a type glyph + a small "expand" badge (signals it opens on click) +
 * an accent border to distinguish it from the status panels; click restores the
 * floating window, the corner × dismisses it. Rendered by the page (AgentChat /
 * driven by the lifted `useArtifactWindows` state.
 */
interface Props {
  windows: ArtifactWindow[]
  minimized: Set<number>
  onRestore: (id: number) => void
  onClose: (id: number) => void
}

function TypeIcon({ type }: { type: MessageBlock['type'] }) {
  const cls = 'w-5 h-5 text-p-text-secondary'
  switch (type) {
    case 'images':
    case 'image_generating':
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <circle cx="8.5" cy="9.5" r="1.5" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M21 16l-5-5L5 20" />
        </svg>
      )
    case 'video':
    case 'media_processing':
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <rect x="3" y="5" width="18" height="14" rx="2" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M10 9l5 3-5 3V9z" />
        </svg>
      )
    case 'audio':
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M11 5L6 9H3v6h3l5 4V5z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M16 9a3 3 0 010 6M18.5 7a6 6 0 010 10" />
        </svg>
      )
    case 'url':
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 10.5a4 4 0 015.66 0l.34.34a4 4 0 010 5.66l-2.5 2.5a4 4 0 01-5.66 0M10.5 13.5a4 4 0 01-5.66 0l-.34-.34a4 4 0 010-5.66l2.5-2.5a4 4 0 015.66 0" />
        </svg>
      )
    default: // file, document_preview
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8l-5-5z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 3v5h5M9 13h6M9 17h6" />
        </svg>
      )
  }
}

export default function ArtifactDock({ windows, minimized, onRestore, onClose }: Props) {
  const items = windows.filter((w) => minimized.has(w.id))
  if (items.length === 0) return null

  return (
    <div className="flex flex-col gap-2 items-start">
      {items.map((w) => (
        <div key={w.id} className="oto-pop-in group relative">
          <button
            onClick={() => onRestore(w.id)}
            title={`Open: ${w.title}`}
            className="relative flex h-10 w-10 items-center justify-center rounded-xl border border-brand/40 bg-white/80 shadow-xs backdrop-blur-xs transition-all hover:bg-white hover:shadow-md dark:bg-gray-900/80 dark:hover:bg-p-surface"
          >
            <TypeIcon type={w.block.type} />
            {/* expand badge — signals "click to open" */}
            <span className="absolute -bottom-0.5 -right-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-full border border-brand/40 bg-white text-brand dark:bg-p-surface">
              <svg className="h-2 w-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 14v6h6M20 10V4h-6" />
              </svg>
            </span>
          </button>
          {/* dismiss — red, top-left. Always visible on mobile (no hover); on
              desktop it appears on hover to keep the dock clean. */}
          <button
            onClick={() => onClose(w.id)}
            title="Close"
            className="absolute -left-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full border border-p-accent-red bg-p-accent-red text-white shadow-xs hover:brightness-110 md:hidden md:group-hover:flex"
          >
            <svg className="h-2.5 w-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  )
}
