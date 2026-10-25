import { lazy, useEffect } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useFlag } from '../hooks/useFlag'
import { AudioPrefsSection } from '../components/audio/AudioPrefsSection'
import SettingsShell from '../components/settings/SettingsShell'
import {
  USER_SETTINGS_BASE, userSections, userSettingsPath, userSettingsTabTarget,
} from '../components/settings/sections'
import { ProfileSection, SecuritySection, AppearanceSection, MyMemorySection, WakeWordSection, NotificationsSection, AppsSection } from './UserSettings.general'
import { IntegrationsTab } from './UserSettings.integrations'
import { MyMachinesSection } from './UserSettings.machines'
import { ExecutionLayersSection } from './UserSettings.aiEngines'

// The usage section carries the charts library; it loads when it opens.
const UsageSection = lazy(() =>
  import('./UserSettings.usage').then(m => ({ default: m.UsageSection })))

/**
 * The user settings area: the shell with the person's sections. An older
 * `/user-settings?tab=<id>` link is sent to the section's own path with
 * every other query parameter kept, before anything paints.
 */
export default function UserSettings() {
  const { user } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const remoteMachinesAvailable = useFlag('remote_machines_available')
  const redirecting = location.pathname === USER_SETTINGS_BASE && new URLSearchParams(location.search).has('tab')

  useEffect(() => {
    if (redirecting) navigate(userSettingsTabTarget(location.search), { replace: true })
  }, [redirecting, location.search, navigate])

  if (redirecting) return null

  return (
    <SettingsShell
      base={USER_SETTINGS_BASE}
      title={user?.display_name || user?.name || 'Settings'}
      subtitle="User Settings"
      sections={userSections({ remoteMachinesAvailable })}
      readingWidth
    />
  )
}

export function GeneralSection() {
  const { user } = useAuth()
  const isLocal = user?.auth_provider?.startsWith('local')
  return (
    <>
      <ProfileSection />
      {/* Security — only for local auth users (SSO providers manage it) */}
      {isLocal && <SecuritySection />}
      <AppearanceSection />
      <NotificationsSection />
      <AppsSection />
      <WakeWordSection />
      <MyMemorySection />
    </>
  )
}

export function IntegrationsSection() {
  return <IntegrationsTab />
}

/** Every authenticated person pairs their own laptop, viewers included; the
 *  section exists only in a build that ships the feature. The admin's
 *  switch (allow_user_paired_machines) keeps the section and it says so. */
export function RemoteMachinesSection() {
  const { user } = useAuth()
  const location = useLocation()
  const remoteMachinesAvailable = useFlag('remote_machines_available')
  if (!remoteMachinesAvailable) {
    return <Navigate to={`${userSettingsPath('general')}${location.search}`} replace />
  }
  return user ? <MyMachinesSection /> : null
}

export function AiEnginesSection() {
  return <ExecutionLayersSection />
}

export function AudioSection() {
  return <AudioPrefsSection />
}

export function UsageRoute() {
  return <UsageSection />
}

/** A section no row names opens General, the query kept. */
export function UnknownSection() {
  const location = useLocation()
  return <Navigate to={`${userSettingsPath('general')}${location.search}`} replace />
}
