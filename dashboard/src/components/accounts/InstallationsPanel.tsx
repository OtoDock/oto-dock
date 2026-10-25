/**
 * The installations of a GitHub-App account (inside its AccountCard): where
 * the app is installed and what it reaches, a badge for a suspended one and
 * for one the person does not own, "Install on GitHub" (the install page,
 * which returns to OtoDock and re-lists them) and "Refresh" (re-lists them
 * now).
 */

import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { AccountInstallation, AccountSummary, Integration } from '../../api/credentials'
import { useAppInstallUrl, useRefreshInstallations } from '../../api/oauth'
import { eventCatalogKey } from '../../api/subscriptions'
import { BADGE_BASE, BADGE_TONE } from '../ui/classes'

interface Props {
  integration: Integration
  account: AccountSummary
}

function reach(i: AccountInstallation): string {
  if (i.repository_selection === 'all') return 'all repositories'
  const n = i.repositories.length
  if (n === 0) return 'no repository'
  if (n <= 3 && !i.repositories_truncated) return i.repositories.join(', ')
  const rest = i.repositories_truncated ? 'and more' : `and ${n - 3} more`
  return `${i.repositories.slice(0, 3).join(', ')} ${rest}`
}

export function InstallationsPanel({ integration, account }: Props) {
  const qc = useQueryClient()
  const installUrl = useAppInstallUrl()
  const refresh = useRefreshInstallations()
  const [error, setError] = useState('')
  const providerId = integration.oauth_meta?.provider_id || 'github'
  const mcpName = integration.mcp_name
  const installations = account.installations ?? []
  const canInstall = integration.oauth_meta?.app_install_available === true

  // The setup page (the app's return after an install, on the web a popup)
  // posts the same message as a connect's success page and closes itself;
  // the connect form that listens for it is gone by now, so the panel
  // re-reads the list itself. Only our own origin is trusted.
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return
      if (event.data?.type === 'oauth-complete') {
        qc.invalidateQueries({ queryKey: ['my-integrations'] })
        // The subscribe dialog's installation picker reads the same list.
        qc.invalidateQueries({ queryKey: eventCatalogKey(mcpName) })
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [qc, mcpName])

  const handleInstall = async () => {
    setError('')
    try {
      const isNative = !!(window as unknown as {
        Capacitor?: { isNativePlatform?: () => boolean }
      }).Capacitor?.isNativePlatform?.()
      const { url } = await installUrl.mutateAsync({
        provider: providerId,
        mcpName: integration.mcp_name,
        accountLabel: account.account_label,
        mobile: isNative,
      })
      const { openOAuthWindow } = await import('../../lib/oauth')
      const ok = await openOAuthWindow(url, `${providerId}-install`, () => {
        qc.invalidateQueries({ queryKey: ['my-integrations'] })
        qc.invalidateQueries({ queryKey: eventCatalogKey(mcpName) })
      })
      if (!ok && !isNative) setError('Popup blocked. Allow popups for this site.')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to open the install page')
    }
  }

  const handleRefresh = async () => {
    setError('')
    try {
      await refresh.mutateAsync({
        provider: providerId,
        mcpName: integration.mcp_name,
        accountLabel: account.account_label,
      })
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to refresh')
    }
  }

  return (
    <div className="mt-2 border-t border-subtle pt-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-semibold text-tertiary">
          Installations
        </span>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => void handleRefresh()}
            disabled={refresh.isPending}
            className="text-xs text-secondary hover:underline disabled:opacity-50"
          >
            {refresh.isPending ? 'Refreshing…' : 'Refresh'}
          </button>
          {canInstall && (
            <button
              type="button"
              onClick={() => void handleInstall()}
              disabled={installUrl.isPending}
              className="text-xs text-link hover:underline disabled:opacity-50"
            >
              {installations.length === 0 ? 'Install on GitHub' : 'Change on GitHub'}
            </button>
          )}
        </div>
      </div>
      {installations.length === 0 ? (
        <div className="text-xs text-tertiary italic">
          Not installed anywhere yet. Install the app on the repositories you
          choose. Until then, this account reaches nothing.
        </div>
      ) : (
        <ul className="space-y-1">
          {installations.map((i) => (
            <li key={i.id} className="text-xs text-primary flex items-center gap-2 flex-wrap">
              <span className="font-medium">{i.account}</span>
              <span className="text-tertiary">
                {i.target_type === 'Organization' ? 'organization' : 'account'}: {reach(i)}
              </span>
              {i.suspended_at && (
                <span className={`${BADGE_BASE} ${BADGE_TONE.warning}`}>
                  Suspended
                </span>
              )}
              {!i.admin && (
                <span
                  className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}
                  title="You can reach this installation but do not own it. It cannot be an agent's identity."
                >
                  Not an owner
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {error && <div className="mt-1 text-xs text-status-danger-fg">{error}</div>}
    </div>
  )
}
