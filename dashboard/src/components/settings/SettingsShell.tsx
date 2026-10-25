import { Suspense, useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'
import { Link, Outlet, useLocation, useNavigationType } from 'react-router-dom'
import { useSwipeGesture } from '../../hooks/useSwipeGesture'
import { PageBoundary } from '../RouteBoundary'
import NavGroup, { NavBadge, NAV_ROW, NAV_ROW_ACTIVE, NAV_ROW_IDLE } from '../ui/NavGroup'
import { IconChevronLeft } from '../icons/nav'
import { currentSection, groupSections, type SettingsSection } from './sections'
import { useSettingsArea, type SettingsAreaNav } from './useSettingsArea'
import { AccountFooter, BackToChatButton } from './AccountFooter'
import SectionList from './SectionList'
import { isNarrowViewport } from './useNarrow'

export interface SettingsShellProps {
  /** The area's index path (`/admin`, `/agents/<name>`, `/user-settings`). */
  base: string
  title: string
  subtitle: string
  /** The sidebar's title is the wordmark, set in the display face (the
   *  phone list's title is a page title, always in the display face). */
  wordmark?: boolean
  sections: SettingsSection[]
  /** The agent area's slug: its Back to Chat stays on that agent's chats. */
  agent?: string
  /** The admin area's banner strips, above the content on every width. A
   *  dialog belongs beside the shell, not here: this node remounts with
   *  the phone's list and section moves. */
  banners?: ReactNode
  /** Forms read better in a bounded width (User Settings). */
  readingWidth?: boolean
}

type ViewProps = SettingsShellProps & { nav: SettingsAreaNav }

/** A touch starting this close to the screen's edge is Android's own back
 *  gesture, never the section's swipe. */
const EDGE_DEAD_ZONE_PX = 20

/** The phone list's scroll offset per area, kept while the page lives. */
const listScroll = new Map<string, number>()

/**
 * The one layout of User Settings, Agent Settings and Admin. On a desktop a
 * persistent sidebar beside the content; on a phone the index renders the
 * section list and a section renders under a header with a back arrow.
 * The history behind back and Back to Chat is `useSettingsArea`.
 */
export default function SettingsShell(props: SettingsShellProps) {
  const nav = useSettingsArea(props.base, props.agent)
  if (nav.narrow && nav.onIndex) return <ListView {...props} nav={nav} />
  return <SectionFrame {...props} nav={nav} />
}

function SectionOutlet() {
  // The admin pages are lazy routes. Router navigations run as transitions,
  // so the current page stays until the next chunk resolves; this fallback
  // shows only on a cold direct load.
  return (
    <PageBoundary>
      <Suspense fallback={<div className="min-h-[40vh]" />}>
        <Outlet />
      </Suspense>
    </PageBoundary>
  )
}

function SidebarRow({ section, onIndex }: { section: SettingsSection; onIndex: boolean }) {
  const location = useLocation()
  const Icon = section.icon
  const matched = section.exact
    ? location.pathname === section.path
    : location.pathname === section.path || location.pathname.startsWith(`${section.path}/`)
  // The index section is also the page on the area's own index path.
  const active = matched || (section.index === true && onIndex)
  return (
    <Link
      to={section.path}
      aria-current={active ? 'page' : undefined}
      className={`${NAV_ROW} ${active ? NAV_ROW_ACTIVE : NAV_ROW_IDLE}`}
    >
      <Icon className="w-4 h-4 shrink-0" />
      <span className="flex-1 truncate">{section.label}</span>
      <NavBadge count={section.badge} />
    </Link>
  )
}

/**
 * A section on both widths, and the desktop's index. One tree, with the
 * sidebar and the phone header as conditional siblings around an outlet
 * that never moves: a fold or an unfold on a section keeps the page and
 * its state (a Setup sub-tab, an unsaved field, the scroll).
 */
function SectionFrame({ base, title, subtitle, wordmark, sections, banners, readingWidth, nav }: ViewProps) {
  const location = useLocation()
  const action = useNavigationType()
  const { narrow } = nav
  const { ungrouped, groups } = groupSections(sections)
  const section = currentSection(sections, base, location.pathname)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const mainRef = useRef<HTMLElement>(null)
  // Attached on every width; the hook acts under 768 px alone.
  useSwipeGesture(mainRef, {
    onSwipeRight: nav.goBack,
    edgeDeadZone: EDGE_DEAD_ZONE_PX,
    ignoreIframeSwipes: true,
  })
  // On a phone a row tap moves the focus to the section's heading, so a
  // screen reader is not left on the vanished list; a traversal keeps the
  // focus where the browser put it.
  useEffect(() => {
    if (action === 'PUSH' && isNarrowViewport()) headingRef.current?.focus({ preventScroll: true })
  }, [location.key, action])
  // A new section starts at its top; a query change inside one (a sub-tab,
  // a highlighted row) keeps the place.
  useEffect(() => {
    if (mainRef.current) mainRef.current.scrollTop = 0
  }, [location.pathname])

  return (
    <div className="flex h-screen-safe bg-surface-page">
      {!narrow && (
        <aside className="w-[var(--ot-sidebar-width)] shrink-0 flex flex-col h-full bg-surface-card border-r border-subtle">
          <div className="p-3 border-b border-subtle">
            <BackToChatButton onClick={nav.backToChat} />
          </div>
          <div className="px-4 py-3 border-b border-subtle">
            <h1 className={`text-sm font-bold text-primary truncate ${wordmark ? 'font-display' : ''}`}>{title}</h1>
            <p className="text-xs text-tertiary mt-0.5 truncate">{subtitle}</p>
          </div>
          <nav aria-label={`${title} sections`} className="flex-1 p-2 overflow-y-auto">
            {ungrouped.map((s) => <SidebarRow key={s.path} section={s} onIndex={nav.onIndex} />)}
            {groups.map((g) => <NavGroup key={g.label} label={g.label} items={g.items} />)}
          </nav>
          <AccountFooter />
        </aside>
      )}

      <div className="flex-1 flex flex-col min-w-0">
        {narrow && (
          <header className="flex items-center h-12 px-1 shrink-0 bg-surface-card border-b border-subtle">
            <button
              type="button"
              aria-label="Back"
              onClick={nav.goBack}
              className="flex items-center justify-center size-control-md shrink-0 rounded-control text-secondary hover:bg-surface-hover hover:text-primary transition-colors duration-fast"
            >
              <IconChevronLeft className="w-5 h-5" />
            </button>
            <h1
              ref={headingRef}
              tabIndex={-1}
              className="ml-1 text-base font-semibold text-primary truncate focus:outline-none"
            >
              {section?.label ?? title}
            </h1>
          </header>
        )}
        {banners}
        <main ref={mainRef} className={`flex-1 overflow-auto ${narrow ? 'p-4' : 'p-6'}`}>
          <div className={readingWidth && !narrow ? 'max-w-3xl' : undefined}>
            <SectionOutlet />
          </div>
        </main>
      </div>
    </div>
  )
}

function ListView({ base, title, subtitle, sections, banners, nav }: ViewProps) {
  const mainRef = useRef<HTMLElement>(null)
  // A return from a section lands where the row was.
  useLayoutEffect(() => {
    const el = mainRef.current
    if (!el) return
    el.scrollTop = listScroll.get(base) ?? 0
    return () => {
      listScroll.set(base, el.scrollTop)
    }
  }, [base])

  return (
    <div className="flex flex-col h-screen-safe bg-surface-page">
      {banners}
      <main ref={mainRef} className="flex-1 overflow-auto">
        <SectionList
          title={title}
          subtitle={subtitle}
          sections={sections}
          onBackToChat={nav.backToChat}
        />
      </main>
    </div>
  )
}
