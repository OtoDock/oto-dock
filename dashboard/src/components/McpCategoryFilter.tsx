/**
 * The MCP lists' category filter: a small funnel button opening a menu with
 * All MCPs, Core, Custom and Community (each with its count). Core and
 * custom MCPs ship with the platform; community ones are installed from the
 * catalog. The active filter shows as a brand dot on the button.
 */

import IconDropdown from './ui/IconDropdown'

export type McpCategoryFilterValue = 'all' | 'core' | 'custom' | 'community'

const CATEGORY_LABEL: Record<Exclude<McpCategoryFilterValue, 'all'>, string> = {
  core: 'Core',
  custom: 'Custom',
  community: 'Community',
}

export function matchesMcpCategory(category: string, filter: McpCategoryFilterValue): boolean {
  return filter === 'all' || category === filter
}

export default function McpCategoryFilter({ value, onChange, counts }: {
  value: McpCategoryFilterValue
  onChange: (value: McpCategoryFilterValue) => void
  /** Rows per category, before any search filter. */
  counts: Partial<Record<string, number>>
}) {
  const total = Object.values(counts).reduce((sum, n) => (sum ?? 0) + (n ?? 0), 0) ?? 0
  const options = [
    { value: 'all', label: `All MCPs (${total})` },
    ...(Object.keys(CATEGORY_LABEL) as Array<keyof typeof CATEGORY_LABEL>)
      .filter(c => (counts[c] ?? 0) > 0 || c === value)
      .map(c => ({ value: c, label: `${CATEGORY_LABEL[c]} (${counts[c] ?? 0})` })),
  ]
  const active = value !== 'all'
  return (
    <IconDropdown
      label="Show"
      value={value}
      options={options}
      direction="down"
      onChange={v => onChange(v as McpCategoryFilterValue)}
      trigger={
        <span
          className={`relative flex items-center justify-center w-7 h-7 rounded-lg border transition-colors cursor-pointer ${
            active
              ? 'bg-brand/10 border-brand/40 text-brand'
              : 'bg-white dark:bg-p-surface border-p-border-light text-p-text-secondary hover:bg-p-surface-hover'
          }`}
          title={active ? `Showing ${CATEGORY_LABEL[value as keyof typeof CATEGORY_LABEL]} MCPs` : 'Filter by category'}
          aria-label="Filter MCPs by category"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" />
          </svg>
          {active && <span className="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full bg-brand" />}
        </span>
      }
    />
  )
}
