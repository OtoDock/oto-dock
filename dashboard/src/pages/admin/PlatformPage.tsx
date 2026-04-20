import { useState } from 'react'
import { ScrollableTabs } from '../../components/ScrollableTabs'
import GeneralTab from './GeneralTab'
import ExecutionLayersTab from './ExecutionLayersTab'
import OtodockTab from './OtodockTab'
import SystemSettingsTab from './SystemSettingsTab'
import AudioTab from './AudioTab'
import SecurityTab from './SecurityTab'

// ---------------------------------------------------------------------------
// Main page with tabs
// ---------------------------------------------------------------------------

type Tab = 'general' | 'execution-layers' | 'system' | 'otodock' | 'audio' | 'phone-servers' | 'security'

const SETUP_TABS: { id: Tab; label: string }[] = [
  { id: 'general', label: 'General' },
  { id: 'execution-layers', label: 'AI Engines' },
  { id: 'otodock', label: 'OtoDock' },
  { id: 'audio', label: 'Audio' },
  { id: 'security', label: 'Security' },
  { id: 'system', label: 'System Settings' },
]

export default function PlatformPage() {
  const [tab, setTab] = useState<Tab>('general')

  return (
    <div className="max-w-3xl">
      <h2 className="text-lg font-semibold text-p-text mb-1">Setup</h2>
      <p className="text-sm text-p-text-light mb-4">
        Configure your platform identity, AI engines, integrations, and policies.
      </p>

      {/* Horizontally scrollable tab bar — scales to any width / tab count. */}
      <div className="mb-5">
        <ScrollableTabs tabs={SETUP_TABS} active={tab} onChange={setTab} />
      </div>

      {/* Tab content */}
      {tab === 'general' && <GeneralTab />}
      {tab === 'execution-layers' && <ExecutionLayersTab />}
      {tab === 'otodock' && <OtodockTab />}
      {tab === 'system' && <SystemSettingsTab />}
      {tab === 'audio' && <AudioTab />}
      {tab === 'security' && <SecurityTab />}
    </div>
  )
}
