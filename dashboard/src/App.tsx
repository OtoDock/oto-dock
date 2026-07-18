import { useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import Layout from './components/Layout'
import AgentLayout from './components/AgentLayout'
import RequireAuth from './components/RequireAuth'
import RequireRole from './components/RequireRole'
import PlatformSetupGuard, { SetupBanner } from './components/PlatformSetupGuard'
import AgentGuard from './components/AgentGuard'
import DefaultAgentRedirect from './components/DefaultAgentRedirect'
import AgentGrid from './pages/AgentGrid'
import AuthCallback from './pages/AuthCallback'
import Overview from './pages/Overview'
import Schedules from './pages/Schedules'
import Triggers from './pages/Triggers'
import History from './pages/History'
import RunRedirect from './pages/RunRedirect'
import UsersPage from './pages/admin/UsersPage'
import UsagePage from './pages/admin/UsagePage'
import McpServersPage from './pages/admin/McpServersPage'
import McpRequestsPage from './pages/admin/McpRequestsPage'
import SkillsPage from './pages/admin/SkillsPage'
import PlatformPage from './pages/admin/PlatformPage'
import NotificationsPage from './pages/admin/NotificationsPage'
import MeetingsPage from './pages/admin/MeetingsPage'
import RemoteMachinesPage from './pages/admin/RemoteMachinesPage'
import UserSettings from './pages/UserSettings'
import AgentOverview from './pages/agent/AgentOverview'
import AgentChat from './pages/agent/AgentChat'
import AgentSchedules from './pages/agent/AgentSchedules'
import AgentTriggers from './pages/agent/AgentTriggers'
import AgentNotifications from './pages/agent/AgentNotifications'
import AgentMeetings from './pages/agent/AgentMeetings'
import AgentConversations from './pages/agent/AgentConversations'
import ConversationView from './pages/ConversationView'
import AgentMcps from './pages/agent/AgentMcps'
import AgentSkills from './pages/agent/AgentSkills'
import AgentConfig from './pages/agent/AgentConfig'
import ChangePassword from './pages/ChangePassword'
import ResetPassword from './pages/ResetPassword'
import AcceptInvite from './pages/AcceptInvite'
import Setup2FA from './pages/Setup2FA'
import NativePasskey from './pages/NativePasskey'

/**
 * Ensures back button works on secondary pages (settings, agents, admin).
 *
 * When a secondary page is loaded fresh (direct URL, app restore, page refresh),
 * there's no prior history entry — back would exit the platform. This guard
 * injects the root path into history behind the current page so back navigates
 * to the chat page (via DefaultAgentRedirect) instead of exiting.
 */
function NavigationGuard() {
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    // location.key === 'default' means direct URL load, not navigated from within the app
    if (
      location.key === 'default' &&
      location.pathname !== '/' &&
      location.pathname !== '/auth/callback' &&
      location.pathname !== '/change-password' &&
      location.pathname !== '/reset-password' &&
      location.pathname !== '/accept-invite' &&
      location.pathname !== '/setup-2fa' &&
      location.pathname !== '/native-passkey' &&
      !location.pathname.startsWith('/chat/')
    ) {
      const returnTo = location.pathname + location.search
      navigate('/', { replace: true })
      navigate(returnTo)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return null
}

export default function App() {
  useEffect(() => {
    // Signal native that the SPA has mounted, so it can deliver any OAuth deep
    // link it is holding for this installation (see MainActivity.dashboardReady).
    try { (window as any).Android?.dashboardReady?.() } catch { /* not native */ }
  }, [])

  return (
    <BrowserRouter>
      <NavigationGuard />
      <Routes>
        {/* Public routes — no auth required */}
        <Route path="/auth/callback" element={<AuthCallback />} />
        <Route path="/reset-password" element={<ResetPassword />} />
        <Route path="/accept-invite" element={<AcceptInvite />} />
        <Route path="/native-passkey" element={<NativePasskey />} />

        {/* All other routes require auth */}
        <Route element={<RequireAuth />}>
          {/* Force password change / 2FA enrollment (exempt from setup guard) */}
          <Route path="change-password" element={<ChangePassword />} />
          <Route path="setup-2fa" element={<Setup2FA />} />
          {/* Platform setup guard — blocks non-admin if no subscriptions configured */}
          <Route element={<PlatformSetupGuard />}>
          {/* Landing page: redirect to default agent chat */}
          <Route index element={<DefaultAgentRedirect />} />

          {/* Chat routes (standalone, no AgentLayout) */}
          <Route path="chat/:name" element={<AgentGuard />}>
            <Route index element={<AgentChat />} />
            <Route path=":chatId" element={<AgentChat />} />
          </Route>

          {/* User settings */}
          <Route path="user-settings" element={<UserSettings />} />

          {/* Agent selector */}
          <Route path="agents" element={<AgentGrid />} />

          {/* Per-agent management (no Chat tab) */}
          <Route path="agents/:name" element={<AgentGuard />}>
            <Route element={<AgentLayout />}>
              <Route index element={<AgentOverview />} />
              <Route path="scheduled-tasks" element={<AgentSchedules />} />
              <Route path="config" element={<AgentConfig />} />
              <Route path="mcps" element={<AgentMcps />} />
              <Route path="skills" element={<AgentSkills />} />
              <Route path="triggers" element={<AgentTriggers />} />
              <Route path="notifications" element={<AgentNotifications />} />
              <Route path="meetings" element={<AgentMeetings />} />
              <Route path="conversations" element={<AgentConversations />} />
            </Route>
          </Route>

          {/* Admin: global views + user management (admin only) */}
          <Route path="admin" element={<RequireRole minRole="admin" />}>
            <Route element={<Layout />}>
              <Route index element={<Overview />} />
              <Route path="scheduled-tasks" element={<Schedules />} />
              <Route path="triggers" element={<Triggers />} />
              <Route path="notifications" element={<NotificationsPage />} />
              <Route path="task-history" element={<History />} />
              <Route path="meetings" element={<MeetingsPage />} />
              <Route path="users" element={<UsersPage />} />
              <Route path="usage" element={<UsagePage />} />
              <Route path="mcp-servers" element={<McpServersPage />} />
              <Route path="skills" element={<SkillsPage />} />
              <Route path="mcp-requests" element={<McpRequestsPage />} />
              <Route path="remote-machines" element={<RemoteMachinesPage />} />
              <Route path="platform" element={<PlatformPage />} />
            </Route>
          </Route>

          {/* Old run deep links resolve to the chat page (task mode on) */}
          <Route path="runs/:runId" element={<RunRedirect />} />
          <Route path="conversations/:chatId" element={<ConversationView />} />
          </Route>{/* end PlatformSetupGuard */}
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
