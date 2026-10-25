import { useState, type ComponentType } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { BADGE_BASE, BADGE_TONE } from './classes'
import { IconChevronDown, type NavIconProps } from '../icons/nav'

export interface NavGroupItem {
  path: string
  label: string
  exact?: boolean
  /** Defaults to visible; pass `false` to hide a child for the current role. */
  visible?: boolean
  icon?: ComponentType<NavIconProps>
  /** A count shown beside the label when above zero. */
  badge?: number
}

interface NavGroupProps {
  label: string
  items: NavGroupItem[]
  /** Called when a child link is clicked. */
  onNavigate?: () => void
}

/** One row of the settings sidebar: the look every row and group heading shares. */
export const NAV_ROW = 'flex items-center gap-2.5 px-3 py-2 rounded-control text-sm mb-1 transition-colors duration-fast'
export const NAV_ROW_ACTIVE = 'bg-action-subtle text-link font-medium'
export const NAV_ROW_IDLE = 'text-secondary hover:bg-surface-hover hover:text-primary'

/** A section's pending count, beside its label. */
export function NavBadge({ count }: { count?: number }) {
  if (!count || count <= 0) return null
  return (
    <span className={`${BADGE_BASE} ${BADGE_TONE.warning}`} aria-label={`${count} pending`}>
      {count}
    </span>
  )
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
  // A child becoming the current page opens the group (in render, on the
  // transition); leaving it keeps whatever the person chose.
  const [wasActive, setWasActive] = useState(hasActiveChild)
  if (wasActive !== hasActiveChild) {
    setWasActive(hasActiveChild)
    if (hasActiveChild) setExpanded(true)
  }

  if (visibleItems.length === 0) return null

  return (
    <div className="mb-1">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
        className={`w-full justify-between ${NAV_ROW} ${
          hasActiveChild ? 'text-link font-medium' : NAV_ROW_IDLE
        }`}
      >
        <span>{label}</span>
        <IconChevronDown className={`w-3.5 h-3.5 transition-transform duration-base ${expanded ? 'rotate-180' : ''}`} />
      </button>
      {expanded && (
        <div className="mt-1">
          {visibleItems.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.path}
                to={item.path}
                end={item.exact}
                onClick={onNavigate}
                className={({ isActive }) => `${NAV_ROW} pl-6 ${isActive ? NAV_ROW_ACTIVE : NAV_ROW_IDLE}`}
              >
                {Icon && <Icon className="w-4 h-4 shrink-0" />}
                <span className="flex-1 truncate">{item.label}</span>
                <NavBadge count={item.badge} />
              </NavLink>
            )
          })}
        </div>
      )}
    </div>
  )
}
