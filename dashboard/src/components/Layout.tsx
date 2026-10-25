import { useAuth } from '../contexts/AuthContext'
import { useFlag } from '../hooks/useFlag'
import { isAdmin as isPlatformAdmin } from '../lib/permissions'
import { SetupBanner } from './PlatformSetupGuard'
import { ForwardingBanner } from './admin/ForwardingBanner'
import { ReleaseBanner } from './admin/ReleaseBanner'
import { ReleaseDialog } from './ReleaseDialog'
import { useAdminMcpRequests } from '../api/community'
import SettingsShell from './settings/SettingsShell'
import { ADMIN_BASE, adminSections } from './settings/sections'

/** The admin area on the settings shell, with the admin banner strips
 *  above the content on every width and its dialogs beside it. */
export default function Layout() {
  const { user } = useAuth()
  const remoteMachinesAvailable = useFlag('remote_machines_available')
  // Pending-count badge for the MCP Requests section. Only admins poll;
  // managers reach the queue via their own agent pages.
  const isAdmin = isPlatformAdmin(user)
  const { data: mcpRequestsData } = useAdminMcpRequests(true)
  const pendingMcpRequests = isAdmin ? (mcpRequestsData?.pending_count ?? 0) : 0

  // The dialogs sit beside the shell: its banner node remounts with the
  // phone's list and section moves, and a dialog keeps its dismissal in
  // its own state. Both are portaled, so their place changes nothing.
  return (
    <>
      <ReleaseDialog />
      <SettingsShell
        base={ADMIN_BASE}
        title="OtoDock"
        wordmark
        subtitle="Admin"
        sections={adminSections({ remoteMachinesAvailable, pendingMcpRequests })}
        banners={
          <>
            <SetupBanner />
            <ForwardingBanner />
            <ReleaseBanner />
          </>
        }
      />
    </>
  )
}
