import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { AudioPrefsSection } from '../components/audio/AudioPrefsSection'
import { ScrollableTabs } from '../components/ScrollableTabs'
import { ProfileSection, SecuritySection, AppearanceSection, MyMemorySection } from './UserSettings.general'
import { IntegrationsTab } from './UserSettings.integrations'
import { MyMachinesSection } from './UserSettings.machines'
import { ExecutionLayersSection } from './UserSettings.aiEngines'
import { UsageSection } from './UserSettings.usage'

// ---------------------------------------------------------------------------
// Main page with tabs (mirrors the admin Setup page layout)
// ---------------------------------------------------------------------------

type SettingsTab = 'general' | 'integrations' | 'remote-machines' | 'ai-engines' | 'audio' | 'usage'

const SETTINGS_TABS: { id: SettingsTab; label: string }[] = [
  { id: 'general', label: 'General' },
  { id: 'integrations', label: 'Integrations' },
  { id: 'remote-machines', label: 'Remote Machines' },
  { id: 'ai-engines', label: 'AI Engines' },
  { id: 'audio', label: 'Audio' },
  { id: 'usage', label: 'Usage' },
]

export default function UserSettings() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  // Hide the whole Remote Machines tab when this build ships without the
  // feature (feature_flags.remote_machines_available === false). The
  // admin-disabled case (allow_user_paired_machines) keeps the tab and
  // shows its "disabled by admin" message instead.
  const remoteMachinesAvailable = user?.feature_flags?.remote_machines_available !== false
  const settingsTabs = remoteMachinesAvailable
    ? SETTINGS_TABS
    : SETTINGS_TABS.filter(t => t.id !== 'remote-machines')

  const isLocal = user?.auth_provider?.startsWith('local')

  // Tab is driven by the ?tab= query param so it's shareable and deep-linkable
  // (e.g. the service-account "connect new" flow lands on Integrations).
  const tabParam = searchParams.get('tab')
  const tab: SettingsTab = settingsTabs.some(t => t.id === tabParam)
    ? (tabParam as SettingsTab)
    : 'general'
  const setTab = (id: SettingsTab) =>
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('tab', id)
      return next
    }, { replace: true })

  return (
    <div className="min-h-screen bg-p-bg">
      <div className="max-w-2xl mx-auto px-4 py-8">
        <div className="flex items-center gap-3 mb-6">
          <button
            onClick={() => navigate(-1)}
            className="p-2 rounded-lg hover:bg-p-surface text-p-text-secondary"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>
          <div className="min-w-0">
            <h1 className="text-xl font-medium text-p-text truncate">
              {user?.display_name || user?.name || 'Settings'}
            </h1>
            <p className="text-sm text-p-text-secondary truncate">
              {user?.name}{user?.email ? ` \u00b7 ${user.email}` : ''}
            </p>
          </div>
        </div>

        {/* Horizontally scrollable tab bar — scales to any width / tab count. */}
        <div className="mb-6">
          <ScrollableTabs tabs={settingsTabs} active={tab} onChange={setTab} />
        </div>

        {/* Tab content */}
        {tab === 'general' && (
          <>
            <ProfileSection />
            {/* Security — only for local auth users (SSO providers manage it) */}
            {isLocal && <SecuritySection />}
            <AppearanceSection />
            <MyMemorySection />
          </>
        )}
        {tab === 'integrations' && <IntegrationsTab />}
        {/* Render for every authenticated user — viewers can pair
            their own laptop too; they own the hardware. */}
        {tab === 'remote-machines' && user && <MyMachinesSection />}
        {tab === 'ai-engines' && <ExecutionLayersSection />}
        {tab === 'audio' && <AudioPrefsSection />}
        {tab === 'usage' && <UsageSection />}
      </div>
    </div>
  )
}
