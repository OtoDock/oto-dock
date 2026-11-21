import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  useMyIntegrations,
} from '../api/credentials'
import { UserAccountsManager } from '../components/UserAccountsManager'
import { CARD } from '../components/ui/classes'
import { ApiKeysSection } from './UserSettings.apiKeys'

// ---------------------------------------------------------------------------
// Integrations Tab (connected MCP accounts + personal trigger API keys)
// ---------------------------------------------------------------------------

export function IntegrationsTab() {
  const { data: integrations, isLoading } = useMyIntegrations()
  // `?provider=<mcp name>` (an app's "Connect X" button, navigation by kind)
  // scrolls to that integration's row and lights it for a moment. Only a
  // click on the row starts a connect flow — never the arrival itself.
  const [searchParams] = useSearchParams()
  const provider = searchParams.get('provider') || ''
  const [highlightDone, setHighlightDone] = useState('')
  const highlight = provider && highlightDone !== provider && integrations?.some((i) => i.mcp_name === provider)
    ? provider : ''
  useEffect(() => {
    if (!highlight) return
    document.getElementById(`integration-${highlight}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    const timer = setTimeout(() => setHighlightDone(highlight), 2500)
    return () => clearTimeout(timer)
  }, [highlight])
  return (
    <div>
      <div className="mb-8">
        <h2 className="font-display text-lg font-semibold text-primary mb-3">Connected Accounts</h2>
        <p className="text-sm text-secondary mb-4">
          Configure your personal credentials for MCP tools. These are used when agents access external services on your behalf.
        </p>
        {isLoading ? (
          <div className="text-sm text-tertiary">Loading...</div>
        ) : integrations && integrations.length > 0 ? (
          <div className="space-y-3">
            {integrations.map(i => (
              <div
                key={i.mcp_name}
                id={`integration-${i.mcp_name}`}
                className={`rounded-card transition-shadow ${highlight === i.mcp_name ? 'ring-2 ring-focus-ring ring-offset-2 ring-offset-surface-page' : ''}`}
              >
                <UserAccountsManager integration={i} />
              </div>
            ))}
          </div>
        ) : (
          <div className={`${CARD} text-sm text-tertiary p-6 text-center`}>
            No integrations available for your assigned agents.
          </div>
        )}
      </div>
      <ApiKeysSection />
    </div>
  )
}
