import { useState, useRef } from 'react'
import { Outlet, NavLink, Link, useParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useAgentInfo } from '../api/agents'
import { useSwipeGesture } from '../hooks/useSwipeGesture'
import { canManageAgent } from '../lib/permissions'
import ResponsiveDrawer from './ui/ResponsiveDrawer'
import NavGroup from './ui/NavGroup'

const ROLE_BADGE: Record<string, string> = {
  admin: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400',
  manager: 'bg-brand-100 text-brand',
  viewer: 'bg-gray-100 dark:bg-gray-800 text-p-text-secondary',
}

export default function AgentLayout() {
  const { name } = useParams<{ name: string }>()
  const { user, logout } = useAuth()
  const { data: agentInfo } = useAgentInfo(name || '')
  const agentDisplayName = agentInfo?.display_name || name
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 768)

  const swipeRef = useRef<HTMLDivElement>(null)
  useSwipeGesture(swipeRef, {
    onSwipeRight: () => { if (!sidebarOpen) setSidebarOpen(true) },
    onSwipeLeft: () => { if (sidebarOpen) setSidebarOpen(false) },
  })

  const canManage = canManageAgent(user, name || '')

  const topNavItems = [
    { path: `/agents/${name}`, label: 'Overview', exact: true, visible: true },
    { path: `/agents/${name}/mcps`, label: 'MCPs', visible: canManage },
    { path: `/agents/${name}/skills`, label: 'Skills', visible: canManage },
    { path: `/agents/${name}/config`, label: 'Configuration', visible: canManage },
  ]

  // Monitoring = read-only operational views, grouped under a collapsible
  // section so the settings sidebar stays focused on Overview / MCPs / Config.
  const monitoringItems = [
    // Conversations = external (phone / future webhook) sessions on this agent.
    // Visible to anyone who can manage the agent; dashboard chats live on the
    // chat-history page, not here.
    { path: `/agents/${name}/conversations`, label: 'Conversations', visible: canManage },
    { path: `/agents/${name}/scheduled-tasks`, label: 'Scheduled Tasks', visible: true },
    // Triggers tab — visible to all users with agent access. Viewers see
    // read-only metadata + their own user-scoped triggers; managers see +
    // can mutate agent-scoped triggers. Permission flags from API gate
    // mutate actions inside the page.
    { path: `/agents/${name}/triggers`, label: 'Triggers', visible: true },
    // Task history moved into the chat sidebar (the tasks toggle); the admin
    // History page stays as the audit surface.
    { path: `/agents/${name}/notifications`, label: 'Notifications', visible: true },
    { path: `/agents/${name}/meetings`, label: 'Meetings', visible: true },
  ]

  return (
    <div ref={swipeRef} className="flex h-screen-safe bg-p-bg">
      <ResponsiveDrawer open={sidebarOpen} onClose={() => setSidebarOpen(false)} width="w-52" widthPx={208}>
        <aside className="w-full bg-white dark:bg-p-surface border-r border-p-border-light flex flex-col h-full">
          <div className="p-3 border-b border-p-border-light">
            <Link
              to={`/chat/${name}`}
              className="flex items-center justify-center gap-1.5 w-full px-3 py-1.5 rounded-lg text-sm font-medium
                         text-white bg-brand hover:bg-brand-hover transition-colors"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
              </svg>
              Back to Chat
            </Link>
          </div>
          <div className="px-4 py-3 border-b border-p-border-light">
            <h1 className="text-sm font-bold text-p-text">{agentDisplayName}</h1>
            <p className="text-xs text-p-text-light mt-0.5">Agent Settings</p>
          </div>
          <nav className="flex-1 p-2 overflow-y-auto">
            {topNavItems.filter((i) => i.visible).map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                end={item.exact}
                onClick={() => { if (window.innerWidth < 768) setSidebarOpen(false) }}
                className={({ isActive }) =>
                  `block px-3 py-2 rounded-lg text-sm mb-1 transition-colors ${
                    isActive
                      ? 'bg-brand-surface text-brand font-medium'
                      : 'text-p-text-secondary hover:bg-p-surface-hover hover:text-p-text'
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
            <NavGroup
              label="Monitoring"
              items={monitoringItems}
              onNavigate={() => { if (window.innerWidth < 768) setSidebarOpen(false) }}
            />
          </nav>
          {user && (
            <div className="p-3 border-t border-p-border-light">
              <p className="text-xs text-p-text-secondary truncate">{user.name}</p>
              <div className="flex items-center gap-2 mt-1">
                <span className={`px-1.5 py-0.5 rounded-sm text-xs font-medium ${ROLE_BADGE[user.role] || ''}`}>
                  {user.role}
                </span>
                <button onClick={logout} className="text-xs text-p-text-light hover:text-p-text-secondary">
                  Logout
                </button>
              </div>
            </div>
          )}
        </aside>
      </ResponsiveDrawer>

      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-center h-12 px-4 bg-white/80 dark:bg-gray-900/80 backdrop-blur-sm border-b border-p-border-light shrink-0">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="w-8 h-8 rounded-lg bg-p-surface/80 hover:bg-p-border/50 flex items-center justify-center text-p-text-secondary hover:text-p-text transition-colors shrink-0"
            title="Toggle sidebar"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>

          {/* Desktop: settings context label (the sidebar already shows it). */}
          <span className="hidden md:inline ml-3 text-sm font-medium text-p-text">{agentDisplayName}</span>
          <span className="hidden md:inline ml-2 text-xs text-p-text-light">Agent Settings</span>

          {/* Mobile: the sidebar's "Back to Chat" is hidden behind the drawer,
              so surface a duplicate that fills the rest of the header. */}
          <Link
            to={`/chat/${name}`}
            className="md:hidden ml-3 flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
            </svg>
            Back to Chat
          </Link>
        </div>

        <main className="flex-1 overflow-auto p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
