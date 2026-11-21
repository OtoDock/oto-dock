/**
 * A short list of features, each a small icon and a bit of text: the
 * release dialog's cards (with a title) and a community template's
 * highlights (text only). The icons are the one set in release/highlights.ts;
 * a name this dashboard does not know draws a neutral mark.
 */

import { ICON_PATHS, UNKNOWN_ICON_PATH, type IconName } from '../../release/highlights'

export interface Feature {
  icon: string
  title?: string
  text: string
}

export function FeatureIcon({ name, className = 'w-5 h-5' }: { name: string; className?: string }) {
  const d = (ICON_PATHS as Record<string, string>)[name] ?? UNKNOWN_ICON_PATH
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true" data-icon={name in ICON_PATHS ? (name as IconName) : 'unknown'}>
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d={d} />
    </svg>
  )
}

/** `cards`: the release dialog's size. `list`: a template's highlights in a
 *  dialog. `compact`: two columns of small items, a store card's. */
export default function FeatureList({ items, size = 'cards', testId }: {
  items: Feature[]; size?: 'cards' | 'list' | 'compact'; testId?: string
}) {
  if (items.length === 0) return null
  if (size === 'compact') {
    return (
      <ul className="grid grid-cols-2 gap-x-3 gap-y-1.5" data-testid={testId}>
        {items.map((f, i) => (
          <li key={i} className="flex items-center gap-1.5 min-w-0 text-2xs text-secondary">
            <span className="w-6 h-6 rounded-full bg-surface-muted text-secondary flex items-center justify-center shrink-0">
              <FeatureIcon name={f.icon} className="w-3.5 h-3.5" />
            </span>
            <span className="min-w-0 break-words">{f.text}</span>
          </li>
        ))}
      </ul>
    )
  }
  const big = size === 'cards'
  return (
    <ul className={big ? 'space-y-3' : 'space-y-2'} data-testid={testId}>
      {items.map((f, i) => (
        <li key={i} className={`flex ${f.title ? 'items-start' : 'items-center'} gap-3`}>
          <span className={`${big ? 'w-9 h-9' : 'w-8 h-8'} rounded-full bg-surface-muted text-secondary flex items-center justify-center shrink-0`}>
            <FeatureIcon name={f.icon} className={big ? 'w-5 h-5' : 'w-4 h-4'} />
          </span>
          <div className="min-w-0">
            {f.title && <p className="text-sm font-semibold text-primary">{f.title}</p>}
            <p className={big ? 'text-sm text-secondary' : 'text-xs text-secondary'}>{f.text}</p>
          </div>
        </li>
      ))}
    </ul>
  )
}
