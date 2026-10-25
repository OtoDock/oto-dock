import { Link } from 'react-router-dom'
import { CARD } from '../ui/classes'
import { NavBadge } from '../ui/NavGroup'
import { IconChevronRight } from '../icons/nav'
import { groupSections, type SettingsSection } from './sections'
import { AccountFooter, BackToChatButton } from './AccountFooter'

interface Props {
  title: string
  subtitle: string
  sections: SettingsSection[]
  onBackToChat: () => void
}

/**
 * A settings area's landing on a phone: the grouped rows, one card per
 * group, each row a link to its section (the section opens as its own
 * history entry, so the back arrow and the Android back gesture return
 * here).
 */
export default function SectionList({ title, subtitle, sections, onBackToChat }: Props) {
  const { ungrouped, groups } = groupSections(sections)
  const cards = [{ label: '', items: ungrouped }, ...groups].filter((g) => g.items.length > 0)
  return (
    <div className="flex flex-col min-h-full">
      <header className="px-4 pt-4 pb-3">
        <BackToChatButton onClick={onBackToChat} />
        <h1 className="mt-4 text-lg font-display font-semibold text-primary truncate">
          {title}
        </h1>
        <p className="text-sm text-secondary truncate">{subtitle}</p>
      </header>
      <nav aria-label={`${title} sections`} className="px-4 space-y-4">
        {cards.map((g) => (
          <section key={g.label || 'main'}>
            {g.label && (
              <h2 className="px-1 mb-1.5 text-2xs font-medium uppercase tracking-wide text-tertiary">{g.label}</h2>
            )}
            <ul className={`${CARD} overflow-hidden`}>
              {g.items.map((s) => {
                const Icon = s.icon
                return (
                  <li key={s.path} className="border-t border-subtle first:border-t-0">
                    <Link
                      to={s.path}
                      className="flex items-center gap-3 px-3 py-3 text-sm text-primary hover:bg-surface-hover active:bg-surface-hover transition-colors duration-fast"
                    >
                      <span className="flex items-center justify-center size-8 shrink-0 rounded-md bg-action-subtle text-link">
                        <Icon className="w-4 h-4" />
                      </span>
                      <span className="flex-1 truncate">{s.label}</span>
                      <NavBadge count={s.badge} />
                      <IconChevronRight className="w-4 h-4 shrink-0 text-tertiary" />
                    </Link>
                  </li>
                )
              })}
            </ul>
          </section>
        ))}
      </nav>
      <div className="mt-auto pt-4">
        <AccountFooter />
      </div>
    </div>
  )
}
