import { useState, useRef } from 'react'
import { Outlet, NavLink, Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useSwipeGesture } from '../hooks/useSwipeGesture'
import ResponsiveDrawer from './ui/ResponsiveDrawer'
import NavGroup from './ui/NavGroup'
import { SetupBanner } from './PlatformSetupGuard'
import { useAdminMcpRequests } from '../api/community'

const ROLE_BADGE: Record<string, string> = {
  admin: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400',
  manager: 'bg-brand-100 text-brand',
  viewer: 'bg-gray-100 dark:bg-gray-800 text-p-text-secondary',
}

const navItems = [
  { path: '/admin', label: 'Overview', exact: true },
  { path: '/admin/users', label: 'Users' },
  { path: '/admin/usage', label: 'Usage' },
  { path: '/admin/mcp-servers', label: 'MCP Servers' },
  { path: '/admin/skills', label: 'Skills' },
  { path: '/admin/mcp-requests', label: 'MCP Requests', badge: 'pendingMcpRequests' as const },
  { path: '/admin/remote-machines', label: 'Remote Machines' },
]

// Monitoring = the admin-audit operational views (all agents, all users),
// grouped under a collapsible section at the end of the sidebar.
const monitoringItems = [
  { path: '/admin/scheduled-tasks', label: 'Scheduled Tasks' },
  { path: '/admin/triggers', label: 'Triggers' },
  { path: '/admin/notifications', label: 'Notifications' },
  { path: '/admin/task-history', label: 'Task History' },
  { path: '/admin/meetings', label: 'Meetings' },
]

export default function Layout() {
  const { user, logout } = useAuth()
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 768)
  // Pending-count badge for the MCP Requests nav item. Only admins poll;
  // managers reach the queue via their own agent pages.
  const isAdmin = user?.role === 'admin'
  const { data: mcpRequestsData } = useAdminMcpRequests(true)
  const pendingMcpRequests = isAdmin ? (mcpRequestsData?.pending_count ?? 0) : 0
  const badgeCounts = { pendingMcpRequests }

  const swipeRef = useRef<HTMLDivElement>(null)
  useSwipeGesture(swipeRef, {
    onSwipeRight: () => { if (!sidebarOpen) setSidebarOpen(true) },
    onSwipeLeft: () => { if (sidebarOpen) setSidebarOpen(false) },
  })

  return (
    <div ref={swipeRef} className="flex h-screen-safe bg-p-bg">
      <ResponsiveDrawer open={sidebarOpen} onClose={() => setSidebarOpen(false)} width="w-52" widthPx={208}>
        <aside className="w-full bg-white dark:bg-p-surface border-r border-p-border-light flex flex-col h-full">
          <div className="p-3 border-b border-p-border-light">
            <Link
              to="/"
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
            <h1 className="text-sm font-bold text-p-text">OtoDock</h1>
            <p className="text-xs text-p-text-light mt-0.5">Admin</p>
          </div>
          <nav className="flex-1 p-2 overflow-y-auto">
            {navItems.filter((item) =>
              // Hidden when this build ships without the remote-machines feature.
              item.path !== '/admin/remote-machines'
              || user?.feature_flags?.remote_machines_available !== false
            ).map((item) => {
              const count = item.badge ? badgeCounts[item.badge] : 0
              return (
                <NavLink
                  key={item.path}
                  to={item.path}
                  end={item.exact}
                  onClick={() => { if (window.innerWidth < 768) setSidebarOpen(false) }}
                  className={({ isActive }) =>
                    `flex items-center justify-between px-3 py-2 rounded-lg text-sm mb-1 transition-colors ${
                      isActive
                        ? 'bg-brand-surface text-brand font-medium'
                        : 'text-p-text-secondary hover:bg-p-surface-hover hover:text-p-text'
                    }`
                  }
                >
                  <span>{item.label}</span>
                  {count > 0 && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500 text-white font-medium">
                      {count}
                    </span>
                  )}
                </NavLink>
              )
            })}
            <NavGroup
              label="Monitoring"
              items={monitoringItems}
              onNavigate={() => { if (window.innerWidth < 768) setSidebarOpen(false) }}
            />
            {/* Setup sits last — below Monitoring. */}
            <NavLink
              to="/admin/platform"
              onClick={() => { if (window.innerWidth < 768) setSidebarOpen(false) }}
              className={({ isActive }) =>
                `flex items-center px-3 py-2 rounded-lg text-sm mb-1 transition-colors ${
                  isActive
                    ? 'bg-brand-surface text-brand font-medium'
                    : 'text-p-text-secondary hover:bg-p-surface-hover hover:text-p-text'
                }`
              }
            >
              Setup
            </NavLink>
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

          {/* Desktop: section label. */}
          <span className="hidden md:inline ml-3 text-sm font-medium text-p-text">Admin</span>

          {/* Mobile: the sidebar's "Back to Chat" is hidden behind the drawer,
              so surface a duplicate that fills the rest of the header. */}
          <Link
            to="/"
            className="md:hidden ml-3 flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
            </svg>
            Back to Chat
          </Link>
        </div>

        <SetupBanner />
        <main className="flex-1 overflow-auto p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
