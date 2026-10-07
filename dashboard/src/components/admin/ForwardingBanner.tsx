/**
 * The admin banner for a reverse-proxy misconfiguration: while the proxy
 * reports one (an address that forwards without being trusted, a trusted
 * proxy that appends no X-Forwarded-For, or an https public URL with no
 * TRUSTED_PROXY in a container), every admin page and chat shows the first
 * row's advice with a link to the Security tab. Hidden on the OtoDock cloud,
 * where the operator owns the edge. Dismissing it holds for this tab's
 * session until the set of warnings changes.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
import { isAdmin } from '../../lib/permissions'
import { FORWARDING_CONSEQUENCE, forwardingAdvice } from './forwardingAdvice'
import type { ForwardingWarning } from './forwardingAdvice'

const DISMISSED = 'otodock.forwardingBanner.dismissed'

function readDismissed(): string {
  try { return sessionStorage.getItem(DISMISSED) ?? '' } catch { return '' }
}

export function ForwardingBanner() {
  const { user, authConfig } = useAuth()
  const enabled = isAdmin(user) && !!authConfig && !authConfig.cloud
  const { data } = useQuery({
    queryKey: ['forwarding-warnings'],
    queryFn: async (): Promise<{ warnings: ForwardingWarning[] }> => {
      const res = await apiFetch('/v1/admin/forwarding-warnings')
      if (!res.ok) throw new Error('Failed to fetch')
      return res.json()
    },
    enabled,
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
  })
  const [dismissed, setDismissed] = useState(readDismissed)

  const warnings = data?.warnings ?? []
  const signature = warnings.map((w) => `${w.case}:${w.peer}`).join('|')
  if (!enabled || !warnings.length || dismissed === signature) return null

  const dismiss = () => {
    try { sessionStorage.setItem(DISMISSED, signature) } catch { /* no storage: this render only */ }
    setDismissed(signature)
  }
  const more = warnings.length - 1

  return (
    <div role="alert" className="bg-amber-50 dark:bg-amber-900/20 border-b border-amber-200 dark:border-amber-800 px-4 py-2.5 flex items-start justify-between gap-3 relative z-50">
      <div className="flex items-start gap-2 text-sm text-amber-800 dark:text-amber-300 min-w-0">
        <svg className="w-4 h-4 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.268 16.5c-.77.833.192 2.5 1.732 2.5z" />
        </svg>
        <span>
          <strong>Client addresses are not being resolved.</strong>{' '}
          {forwardingAdvice(warnings[0], false)}{' '}
          {FORWARDING_CONSEQUENCE}
          {more > 0 && ` ${more} more in the Security settings.`}
        </span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Link
          to="/admin/platform?tab=security"
          className="px-3 py-1 text-xs font-medium rounded-lg bg-amber-600 text-white hover:bg-amber-700 transition-colors"
        >
          Security settings
        </Link>
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss"
          className="p-1 rounded text-amber-700 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-amber-900/40"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  )
}
