/**
 * OAuth Connect / Reconnect / Disconnect form for per-user MCP credentials.
 * Driven by ``UserAccountsManager``.
 *
 * Two behaviors:
 *   - Flow picker — when ``oauth_meta.flows`` lists more than one flow,
 *     a radio appears so the user picks at connect time. ``client_credentials``
 *     (Server-to-Server, admin-only) is hidden — it's configured elsewhere.
 *   - PAT branch — when ``personal_access_token`` is selected, the
 *     "Connect" popup is replaced by a textarea + "Save token" button.
 *
 * ``requires_user_oauth`` gating — rows flagged ``requires_user_oauth: true``
 * are rendered disabled when ``has_service_credentials_only`` is true (the
 * admin configured S2S-only creds; those tools cannot work via S2S).
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type {
  AccountSummary,
  Integration,
  OAuthService,
} from '../../api/credentials'
import {
  useStartOAuth,
  useDisconnectOAuth,
  usePatSave,
  useOAuthAccounts,
  useStartDeviceCode,
  usePollDeviceCode,
  useStartAdminConsent,
} from '../../api/oauth'
interface Props {
  integration: Integration
  account: AccountSummary | null
  onDone: () => void
}

const FLOW_LABELS: Record<string, string> = {
  authorization_code: 'OAuth (browser)',
  authorization_code_pkce: 'OAuth (browser, PKCE)',
  device_code: 'Device code',
  personal_access_token: 'Personal Access Token',
  client_credentials: 'Service-to-Server (admin)',
}

const FLOW_DESCRIPTIONS: Record<string, string> = {
  authorization_code:
    'Recommended for first-time users. Opens a consent popup; the token refreshes automatically.',
  authorization_code_pkce:
    'Same browser flow as OAuth, with PKCE for added safety on public clients.',
  device_code:
    'For devices without a browser. Visit a URL on another device and enter the code shown.',
  personal_access_token:
    'Generate a token in your provider settings and paste it here. Best for production — no mid-session expiry surprises.',
  client_credentials:
    'Admin-managed. No per-user grant; one token serves all calls.',
}

export function OAuthAccountForm({ integration, account, onDone }: Props) {
  const qc = useQueryClient()
  const startOAuth = useStartOAuth()
  const disconnect = useDisconnectOAuth()
  const patSave = usePatSave()
  const startDeviceCode = useStartDeviceCode()
  const pollDeviceCode = usePollDeviceCode()
  const startAdminConsent = useStartAdminConsent()
  const [error, setError] = useState('')
  const providerId = integration.oauth_meta?.provider_id || 'google'
  const queryKey = 'my-integrations'
  const services: OAuthService[] = integration.oauth_services || []

  // Per-(provider, mcp) accounts probe — used for requires_user_oauth gating.
  const accountsProbe = useOAuthAccounts(providerId, integration.mcp_name)
  const hasServiceOnly =
    accountsProbe.data?.has_service_credentials_only || false

  // Flow picker — defaults to the first declared flow (typically OAuth).
  // `client_credentials` (Server-to-Server) is admin-only and configured
  // elsewhere, so it's never offered here.
  const availableFlows = useMemo(() => {
    const flows = integration.oauth_meta?.flows || ['authorization_code']
    return flows.filter((f) => f !== 'client_credentials')
  }, [integration.oauth_meta?.flows])
  const [selectedFlow, setSelectedFlow] = useState<string>(
    availableFlows[0] || 'authorization_code',
  )
  const [patValue, setPatValue] = useState('')

  // Device-code polling state. Persisted to sessionStorage so
  // a popup close / page refresh resumes the poll instead of losing it.
  // Storage key is per-(provider, mcp) so concurrent device-code flows
  // for different MCPs don't collide.
  const sessionKey = `oauth_devicecode_${providerId}_${integration.mcp_name}`
  const [deviceCode, setDeviceCode] = useState<{
    device_code: string
    user_code: string
    verification_uri: string
    verification_uri_complete?: string
    interval: number
    started_at: number
  } | null>(() => {
    try {
      const raw = sessionStorage.getItem(sessionKey)
      return raw ? JSON.parse(raw) : null
    } catch {
      return null
    }
  })
  const pollTimer = useRef<number | null>(null)

  const [selectedServices, setSelectedServices] = useState<string[]>(
    account?.connected_services?.length
      ? account.connected_services
      : services.map((s) => s.key),
  )

  const toggleService = (key: string) =>
    setSelectedServices((prev) =>
      prev.includes(key) ? prev.filter((s) => s !== key) : [...prev, key],
    )

  useEffect(() => {
    const handler = (event: MessageEvent) => {
      // Only trust the OAuth popup, which is served from our own origin — reject
      // postMessages from any other window so a malicious page can't forge an
      // "oauth-complete" and make us treat a connect as finished.
      if (event.origin !== window.location.origin) return
      if (event.data?.type === 'oauth-complete') {
        qc.invalidateQueries({ queryKey: [queryKey] })
        onDone()
      } else if (event.data?.type === 'oauth-error') {
        setError(event.data.error || 'OAuth failed')
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [qc, onDone, queryKey])

  // Device-code polling loop. Runs while `deviceCode` is set; clears the
  // sessionStorage entry on success/cancel/expiry so a stale code doesn't
  // resume on next page load.
  useEffect(() => {
    if (!deviceCode) return
    let cancelled = false
    const tick = async () => {
      if (cancelled) return
      try {
        const result = await pollDeviceCode.mutateAsync({
          provider: providerId,
          mcpName: integration.mcp_name,
          deviceCode: deviceCode.device_code,
          services: selectedServices,
          accountLabel: account?.account_label || '',
        })
        if (cancelled) return
        if (result.status === 'ok') {
          sessionStorage.removeItem(sessionKey)
          setDeviceCode(null)
          qc.invalidateQueries({ queryKey: [queryKey] })
          onDone()
        } else {
          pollTimer.current = window.setTimeout(
            tick, deviceCode.interval * 1000,
          )
        }
      } catch (e: unknown) {
        if (cancelled) return
        sessionStorage.removeItem(sessionKey)
        setDeviceCode(null)
        const msg = e instanceof Error ? e.message : 'Device-code poll failed'
        setError(msg)
      }
    }
    pollTimer.current = window.setTimeout(tick, deviceCode.interval * 1000)
    return () => {
      cancelled = true
      if (pollTimer.current !== null) {
        window.clearTimeout(pollTimer.current)
        pollTimer.current = null
      }
    }
  }, [
    deviceCode, providerId, integration.mcp_name, selectedServices,
    account?.account_label, sessionKey, qc, queryKey,
    onDone, pollDeviceCode,
  ])

  const handleStartDeviceCode = async () => {
    setError('')
    if (selectedServices.length === 0 && services.length > 0) {
      setError('Select at least one service')
      return
    }
    try {
      const result = await startDeviceCode.mutateAsync({
        provider: providerId,
        mcpName: integration.mcp_name,
        services: selectedServices,
        accountLabel: account?.account_label || '',
      })
      const persisted = { ...result, started_at: Date.now() }
      sessionStorage.setItem(sessionKey, JSON.stringify(persisted))
      setDeviceCode(persisted)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to start device code'
      setError(msg)
    }
  }

  const handleCancelDeviceCode = () => {
    sessionStorage.removeItem(sessionKey)
    setDeviceCode(null)
    setError('')
  }

  const handleGrantAdminConsent = async () => {
    setError('')
    try {
      const isNative = !!(window as unknown as {
        Capacitor?: { isNativePlatform?: () => boolean }
      }).Capacitor?.isNativePlatform?.()
      const { url } = await startAdminConsent.mutateAsync({
        mcpName: integration.mcp_name,
        mobile: isNative,
      })
      const { openOAuthWindow } = await import('../../lib/oauth')
      const ok = await openOAuthWindow(
        url, `${providerId}-admin-consent`, () => {
          qc.invalidateQueries({ queryKey: [queryKey] })
        },
      )
      if (!ok && !isNative) {
        setError('Popup blocked. Allow popups for this site.')
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to start admin consent'
      setError(msg)
    }
  }

  const handleConnect = async () => {
    setError('')
    if (selectedServices.length === 0 && services.length > 0) {
      setError('Select at least one service')
      return
    }
    try {
      const isNative = !!(window as unknown as {
        Capacitor?: { isNativePlatform?: () => boolean }
      }).Capacitor?.isNativePlatform?.()
      const { url } = await startOAuth.mutateAsync({
        provider: providerId,
        mcpName: integration.mcp_name,
        services: selectedServices,
        accountLabel: account?.account_label || '',
        mobile: isNative,
      })
      const { openOAuthWindow, waitForDeepLink } = await import(
        '../../lib/oauth'
      )
      if (isNative) {
        await openOAuthWindow(url, `${providerId}-oauth`, undefined, {
          useDeepLink: true,
        })
        try {
          await waitForDeepLink()
          qc.invalidateQueries({ queryKey: [queryKey] })
          onDone()
        } catch {
          /* timeout / user closed */
        }
      } else {
        const ok = await openOAuthWindow(url, `${providerId}-oauth`, () => {
          qc.invalidateQueries({ queryKey: [queryKey] })
        })
        if (!ok) setError('Popup blocked. Allow popups for this site.')
      }
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : 'Failed to start OAuth'
      setError(message)
    }
  }

  const handlePatSave = async () => {
    setError('')
    if (!patValue.trim()) {
      setError('Paste your token before saving')
      return
    }
    if (selectedServices.length === 0 && services.length > 0) {
      setError('Select at least one service')
      return
    }
    try {
      await patSave.mutateAsync({
        provider: providerId,
        mcpName: integration.mcp_name,
        token: patValue.trim(),
        services: selectedServices,
        accountLabel: account?.account_label || '',
      })
      setPatValue('')
      onDone()
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : 'Failed to save token'
      setError(message)
    }
  }

  const handleDisconnect = async () => {
    if (!account) return
    const label = account.display_email || account.account_label
    const target = `${label} from ${integration.display_name}`
    if (
      !confirm(
        `Disconnect ${target}? Agents bound to this account will fall back to the default account.`,
      )
    )
      return
    setError('')
    try {
      await disconnect.mutateAsync({
        provider: providerId,
        mcpName: integration.mcp_name,
        accountLabel: account.account_label,
      })
      onDone()
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : 'Failed to disconnect'
      setError(message)
    }
  }

  // Capabilities warnings — collect across selected services.
  const capabilityWarnings = new Set<string>()
  for (const s of services) {
    if (selectedServices.includes(s.key)) {
      for (const cap of s.capabilities || []) {
        capabilityWarnings.add(cap)
      }
    }
  }

  const showFlowPicker = availableFlows.length > 1
  const isPatFlow = selectedFlow === 'personal_access_token'
  const isDeviceCodeFlow = selectedFlow === 'device_code'
  const patUrl = integration.oauth_meta?.pat_instructions_url || ''

  // Microsoft tenant-admin consent: only render the banner / button
  // when the provider is microsoft AND a service requiring admin
  // consent is selected. The vendor (Microsoft) rejects non-admins
  // with AADSTS65004; we don't pre-gate by OtoDock role because a
  // platform manager who happens to be a Microsoft tenant admin should
  // be able to use the flow.
  const needsAdminConsent =
    providerId === 'microsoft' &&
    services.some(
      (s) => s.requires_admin_consent && selectedServices.includes(s.key),
    )

  return (
    <div className="space-y-3">
      {account ? (
        <div className="text-sm text-p-text">
          Connected as <strong>{account.display_email || account.account_label}</strong>
        </div>
      ) : null}

      {account && account.missing_scopes.length > 0 && (
        <div className="px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
          <div className="text-sm text-amber-700 dark:text-amber-300">
            ⚠️ This account is missing some scopes for the services you
            enabled. Click <strong>Reconnect</strong> to grant the
            additional access without redoing the others.
          </div>
        </div>
      )}

      {showFlowPicker && !account && (
        <div>
          <p className="text-xs text-p-text-secondary mb-2">
            Connection method:
          </p>
          <div className="space-y-1.5">
            {availableFlows.map((flow) => (
              <label
                key={flow}
                className="flex items-start gap-2.5 cursor-pointer"
              >
                <input
                  type="radio"
                  name={`flow-${integration.mcp_name}`}
                  checked={selectedFlow === flow}
                  onChange={() => setSelectedFlow(flow)}
                  className="mt-0.5 text-brand focus:ring-brand"
                />
                <div>
                  <div className="text-sm font-medium text-p-text">
                    {FLOW_LABELS[flow] || flow}
                  </div>
                  <div className="text-xs text-p-text-light">
                    {FLOW_DESCRIPTIONS[flow] || ''}
                  </div>
                </div>
              </label>
            ))}
          </div>
        </div>
      )}

      {services.length > 0 && (
        <div>
          <p className="text-xs text-p-text-secondary mb-2">
            Select which services to connect:
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {services.map((svc) => {
              const isUserOauthGated =
                !!svc.requires_user_oauth && hasServiceOnly
              const tooltip = isUserOauthGated
                ? 'Requires user OAuth — admin has only Service-to-Server credentials configured. Ask your admin to add a user OAuth app.'
                : svc.description
              return (
                <label
                  key={svc.key}
                  title={tooltip}
                  className={`flex items-start gap-2.5 ${
                    isUserOauthGated
                      ? 'cursor-not-allowed opacity-50'
                      : 'cursor-pointer'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={
                      !isUserOauthGated && selectedServices.includes(svc.key)
                    }
                    disabled={isUserOauthGated}
                    onChange={() =>
                      !isUserOauthGated && toggleService(svc.key)
                    }
                    className="mt-0.5 rounded-sm border-p-border-light text-brand focus:ring-brand disabled:cursor-not-allowed"
                  />
                  <div>
                    <div className="text-sm font-medium text-p-text">
                      {svc.label}
                      {isUserOauthGated && (
                        <span className="ml-1 text-xs text-amber-600 dark:text-amber-400">
                          (requires user OAuth)
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-p-text-light">
                      {svc.description}
                    </div>
                  </div>
                </label>
              )
            })}
          </div>
        </div>
      )}

      {capabilityWarnings.size > 0 && (
        <div className="px-3 py-2 rounded-lg border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/20">
          <div className="text-sm font-medium text-red-700 dark:text-red-300 mb-1">
            ⚠️ Sensitive capabilities
          </div>
          <ul className="text-xs text-red-700 dark:text-red-300 list-disc list-inside">
            {Array.from(capabilityWarnings).map((cap) => (
              <li key={cap}>{cap.replace(/_/g, ' ')}</li>
            ))}
          </ul>
        </div>
      )}

      {needsAdminConsent && !account && (
        <div className="px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
          <div className="text-sm text-amber-700 dark:text-amber-300 mb-2">
            🔒 One or more selected services require <strong>tenant-admin
            consent</strong>. A Microsoft tenant admin must grant these
            scopes for the entire organization before users can connect.
          </div>
          <button
            onClick={handleGrantAdminConsent}
            disabled={startAdminConsent.isPending}
            className="px-3 py-1.5 text-xs bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 border border-amber-300 dark:border-amber-700 rounded-sm hover:bg-amber-200 dark:hover:bg-amber-900/60 disabled:opacity-50"
          >
            {startAdminConsent.isPending
              ? 'Opening admin consent…'
              : 'Grant for whole tenant'}
          </button>
        </div>
      )}

      {isDeviceCodeFlow && !account ? (
        <div className="space-y-2">
          {deviceCode ? (
            <div className="space-y-2">
              <div className="px-3 py-2 rounded-lg bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800">
                <p className="text-xs text-blue-700 dark:text-blue-300 mb-2">
                  1. Visit{' '}
                  <a
                    href={
                      deviceCode.verification_uri_complete ||
                      deviceCode.verification_uri
                    }
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline font-medium"
                  >
                    {deviceCode.verification_uri}
                  </a>
                </p>
                <p className="text-xs text-blue-700 dark:text-blue-300 mb-2">
                  2. Enter this code:
                </p>
                <div className="text-2xl font-mono font-bold text-blue-900 dark:text-blue-100 select-all">
                  {deviceCode.user_code}
                </div>
                <p className="text-xs text-blue-600 dark:text-blue-400 mt-2">
                  Waiting for you to complete the prompt in your browser…
                </p>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={handleCancelDeviceCode}
                  className="px-4 py-2 border border-p-border-light rounded-lg text-sm hover:bg-p-bg-secondary"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <button
                onClick={handleStartDeviceCode}
                disabled={
                  startDeviceCode.isPending ||
                  (selectedServices.length === 0 && services.length > 0)
                }
                className="px-4 py-2 bg-brand hover:bg-brand-hover text-white rounded-lg text-sm disabled:opacity-50"
              >
                {startDeviceCode.isPending
                  ? 'Starting…'
                  : 'Get device code'}
              </button>
            </div>
          )}
        </div>
      ) : isPatFlow && !account ? (
        <div className="space-y-2">
          {patUrl && (
            <p className="text-xs text-p-text-light">
              Generate a token at{' '}
              <a
                href={patUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="underline text-brand hover:text-brand-hover"
              >
                {patUrl}
              </a>{' '}
              with the scopes for the services you selected.
            </p>
          )}
          <textarea
            value={patValue}
            onChange={(e) => setPatValue(e.target.value)}
            placeholder="ghp_… or github_pat_…"
            rows={3}
            className="w-full px-3 py-2 text-sm font-mono border border-p-border-light rounded-lg bg-p-bg focus:ring-2 focus:ring-brand focus:border-transparent"
          />
          <div className="flex gap-2">
            <button
              onClick={handlePatSave}
              disabled={
                patSave.isPending ||
                !patValue.trim() ||
                (selectedServices.length === 0 && services.length > 0)
              }
              className="px-4 py-2 bg-brand hover:bg-brand-hover text-white rounded-lg text-sm disabled:opacity-50"
            >
              {patSave.isPending ? 'Saving…' : 'Save token'}
            </button>
          </div>
        </div>
      ) : (
        <div className="flex gap-2">
          <button
            onClick={handleConnect}
            disabled={
              startOAuth.isPending ||
              (selectedServices.length === 0 && services.length > 0)
            }
            className="px-4 py-2 bg-brand hover:bg-brand-hover text-white rounded-lg text-sm disabled:opacity-50"
          >
            {startOAuth.isPending
              ? 'Connecting...'
              : account
                ? 'Reconnect'
                : 'Connect'}
          </button>
          {account && (
            <button
              onClick={handleDisconnect}
              disabled={disconnect.isPending}
              className="px-4 py-2 border border-p-accent-red text-p-accent-red rounded-lg text-sm hover:bg-red-50 dark:hover:bg-red-900/20"
            >
              {disconnect.isPending ? 'Disconnecting...' : 'Disconnect'}
            </button>
          )}
        </div>
      )}

      {error && <div className="text-sm text-p-accent-red">{error}</div>}
    </div>
  )
}
