import { useState, useEffect } from 'react'
import { NavLink, useLocation } from 'react-router-dom'

export interface NavGroupItem {
  path: string
  label: string
  exact?: boolean
  /** Defaults to visible; pass `false` to hide a child for the current role. */
  visible?: boolean
}

interface NavGroupProps {
  label: string
  items: NavGroupItem[]
  /** Called when a child link is clicked (e.g. close the mobile drawer). */
  onNavigate?: () => void
}

/**
 * A collapsible sidebar section. Collapsed by default, it auto-expands when one
 * of its child routes is active (deep link / refresh) so the active item stays
 * visible, but never force-collapses — the user keeps control once it's open.
 */
export default function NavGroup({ label, items, onNavigate }: NavGroupProps) {
  const location = useLocation()
  const visibleItems = items.filter((i) => i.visible !== false)
  const hasActiveChild = visibleItems.some(
    (i) => location.pathname === i.path || location.pathname.startsWith(i.path + '/'),
  )
  const [expanded, setExpanded] = useState(hasActiveChild)

  useEffect(() => {
    if (hasActiveChild) setExpanded(true)
  }, [hasActiveChild])

  if (visibleItems.length === 0) return null

  return (
    <div className="mb-1">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
        className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm transition-colors ${
          hasActiveChild
            ? 'text-brand font-medium'
            : 'text-p-text-secondary hover:bg-p-surface-hover hover:text-p-text'
        }`}
      >
        <span>{label}</span>
        <svg
          className={`w-3.5 h-3.5 transition-transform ${expanded ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {expanded && (
        <div className="mt-1 space-y-1">
          {visibleItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.exact}
              onClick={onNavigate}
              className={({ isActive }) =>
                `block pl-6 pr-3 py-2 rounded-lg text-sm transition-colors ${
                  isActive
                    ? 'bg-brand-surface text-brand font-medium'
                    : 'text-p-text-secondary hover:bg-p-surface-hover hover:text-p-text'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  )
}
