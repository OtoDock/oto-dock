/**
 * OAuth Connect / Reconnect / Disconnect form for per-user MCP credentials.
 * Driven by ``UserAccountsManager``.
 *
 * Two behaviors:
 *   - Ways to connect — one per flow in ``oauth_meta.flows``; with app kinds
 *     (GitHub) the browser flow is one way per ``oauth_meta.connect_kinds``
 *     entry, the default first. More than one way renders a radio.
 *     ``client_credentials`` (Server-to-Server, admin-only) is hidden — it's
 *     configured elsewhere.
 *   - PAT branch — when ``personal_access_token`` is selected, the
 *     "Connect" popup is replaced by a textarea + "Save token" button.
 *
 * ``requires_user_oauth`` gating — rows flagged ``requires_user_oauth: true``
 * are rendered disabled when ``has_service_credentials_only`` is true (the
 * admin configured S2S-only creds; those tools cannot work via S2S).
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { safeHref } from '../../lib/safeUrl'
import { OAUTH_APP_KIND, isGitHubApp } from '../../lib/kinds/oauthApp'
import { useQueryClient } from '@tanstack/react-query'
import type {
  AccountSummary,
  Integration,
  OAuthService,
} from '../../api/credentials'
import { reconnectReasonText } from '../../api/credentials'
import {
  exchangeOAuthCode,
  useStartOAuth,
  useDisconnectOAuth,
  usePatSave,
  useOAuthAccounts,
  useStartDeviceCode,
  usePollDeviceCode,
  useStartAdminConsent,
} from '../../api/oauth'
import { eventCatalogKey } from '../../api/subscriptions'
import { useDialogs } from '../ui/dialogs'
import Button from '../ui/Button'
import { Input } from '../ui/Field'
import { BADGE_BASE, BADGE_TONE, FIELD, FIELD_REGULAR, FIELD_BORDER } from '../ui/classes'
interface Props {
  integration: Integration
  account: AccountSummary | null
  onDone: () => void
}

const FLOW_LABELS: Record<string, string> = {
  authorization_code: 'OAuth in the browser',
  authorization_code_pkce: 'OAuth in the browser with PKCE',
  device_code: 'Device code',
  personal_access_token: 'Personal Access Token',
  client_credentials: 'Service-to-Server, admin only',
}

const BROWSER_SENTENCE = 'Sign in with your account in a browser window.'
const TICK_A_SERVICE = 'Tick at least one service.'

const FLOW_DESCRIPTIONS: Record<string, string> = {
  authorization_code: BROWSER_SENTENCE,
  authorization_code_pkce: BROWSER_SENTENCE,
  device_code: 'Open a page on another device and type the code shown.',
  personal_access_token: 'Create a token in your account settings and paste it here.',
  client_credentials: 'Set up once by an admin. Nobody signs in.',
}

// GitHub's ways to connect: the one provider whose manifest declares app
// kinds, so its browser flow is one way per kind.
const KIND_LABELS: Record<string, string> = {
  [OAUTH_APP_KIND.GITHUB_APP]: 'GitHub App',
  [OAUTH_APP_KIND.OAUTH_APP]: 'GitHub sign-in',
}
const GITHUB_APP_SENTENCE =
  'Sign in with GitHub and install the app on the repositories you choose.'
const HOSTED_GITHUB_APP_SENTENCE =
  'Sign in with GitHub and install the OtoDock app on the repositories you choose.'
const GITHUB_SIGN_IN_SENTENCE = 'Sign in with your personal GitHub account.'
const GITHUB_TOKEN_SENTENCE = 'Create a personal access token on GitHub and paste it here.'

interface ConnectMethod {
  id: string
  flow: string
  /** The kind of app a browser way goes through; '' without app kinds. */
  kind: string
}

/** One way per flow; with app kinds the browser flow is one way per kind,
 * in the server's order (the default first). */
function connectMethods(flows: string[], kinds: string[]): ConnectMethod[] {
  return flows.flatMap((flow) =>
    flow === 'authorization_code' && kinds.length
      ? kinds.map((kind) => ({ id: `${flow}:${kind}`, flow, kind }))
      : [{ id: flow, flow, kind: '' }],
  )
}

export function OAuthAccountForm({ integration, account, onDone }: Props) {
  const dialogs = useDialogs()
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
  const meta = integration.oauth_meta

  // Per-(provider, mcp) accounts probe — used for requires_user_oauth gating.
  const accountsProbe = useOAuthAccounts(providerId, integration.mcp_name)
  const hasServiceOnly =
    accountsProbe.data?.has_service_credentials_only || false

  // Ways to connect — the first is the default. `client_credentials`
  // (Server-to-Server) is admin-only and configured elsewhere, so it's
  // never offered here.
  const metaFlows = meta?.flows
  const metaKinds = meta?.connect_kinds
  const kindInUse = meta?.app_kind
  const availableFlows = useMemo(() => {
    const flows = metaFlows || ['authorization_code']
    return flows.filter((f) => f !== 'client_credentials')
  }, [metaFlows])
  const connectKinds = useMemo(
    () => metaKinds ?? (kindInUse ? [kindInUse] : []),
    [metaKinds, kindInUse],
  )
  const methods = useMemo(
    () => connectMethods(availableFlows, connectKinds),
    [availableFlows, connectKinds],
  )
  const [methodId, setMethodId] = useState(methods[0]?.id ?? '')
  const method = methods.find((m) => m.id === methodId) ?? methods[0]
  const selectedFlow = method?.flow ?? 'authorization_code'
  // A reconnect keeps the account's own kind while it is offered, so an
  // OAuth App account never turns into a GitHub App one by itself.
  const accountKind = account?.kind ?? OAUTH_APP_KIND.OAUTH_APP
  const connectKind = account
    ? (connectKinds.includes(accountKind) ? accountKind : connectKinds[0] ?? '')
    : method?.kind ?? ''
  // A GitHub App: the app's permissions decide, so no services are picked;
  // the person signs in, then installs the app where they choose.
  const githubApp = isGitHubApp(connectKind)
  const [patValue, setPatValue] = useState('')
  const [patLabel, setPatLabel] = useState('')
  // The MCP server names its own authorization server: the install
  // registers itself there and the person signs in on the vendor's page;
  // an optional label keeps a second account of the same person apart.
  const authServer = integration.oauth_meta?.authorization_server
  const registeredClient = authServer && authServer.active !== false ? authServer : undefined
  const [registeredLabel, setRegisteredLabel] = useState('')

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
        qc.invalidateQueries({ queryKey: eventCatalogKey(integration.mcp_name) })
        onDone()
      } else if (event.data?.type === 'oauth-error') {
        setError(event.data.error || 'OAuth failed')
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [qc, onDone, queryKey, integration.mcp_name])

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
          qc.invalidateQueries({ queryKey: eventCatalogKey(integration.mcp_name) })
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

  // The account list, and the subscribe dialog's catalog: its scope line
  // reads the account's scopes, which a connect changes.
  const refreshAccounts = () => {
    qc.invalidateQueries({ queryKey: [queryKey] })
    qc.invalidateQueries({ queryKey: eventCatalogKey(integration.mcp_name) })
  }

  const handleStartDeviceCode = async () => {
    setError('')
    if (selectedServices.length === 0 && services.length > 0) {
      setError(TICK_A_SERVICE)
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
    if (!githubApp && selectedServices.length === 0 && services.length > 0) {
      setError(TICK_A_SERVICE)
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
        accountLabel: account?.account_label || (registeredClient ? registeredLabel.trim() : ''),
        mobile: isNative,
        appKind: connectKind,
      })
      const { openOAuthWindow, waitForDeepLink } = await import(
        '../../lib/oauth'
      )
      if (isNative) {
        await openOAuthWindow(url, `${providerId}-oauth`, undefined, {
          useDeepLink: true,
        })
        let link = ''
        try {
          link = await waitForDeepLink()
        } catch {
          return /* timeout / user closed */
        }
        // The link carries the vendor's code: the exchange runs here, under
        // this login, so the connect is the person's who started it.
        try {
          await exchangeOAuthCode(providerId, link)
        } catch (e: unknown) {
          setError(e instanceof Error ? e.message : 'Failed to complete the connect')
          return
        }
        refreshAccounts()
        onDone()
      } else {
        const ok = await openOAuthWindow(url, `${providerId}-oauth`, refreshAccounts)
        if (!ok) setError('Popup blocked. Allow popups for this site.')
      }
    } catch (e: unknown) {
      // A refused start may come from a stale list (the admin switched the
      // app, the relay stopped offering one): read the ways that stand now.
      qc.invalidateQueries({ queryKey: [queryKey] })
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
      setError(TICK_A_SERVICE)
      return
    }
    try {
      await patSave.mutateAsync({
        provider: providerId,
        mcpName: integration.mcp_name,
        token: patValue.trim(),
        services: selectedServices,
        accountLabel: account?.account_label || patLabel.trim(),
      })
      setPatValue('')
      setPatLabel('')
      refreshAccounts()
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
    // The person's own repositories and Read only per agent live on the
    // account's choices, which go with it.
    const narrowed = (account.agent_reaches ?? []).some((r) => r.repositories.length > 0 || r.read_only)
    if (!(await dialogs.confirm({
      title: `Disconnect ${target}?`,
      body: 'Agents bound to this account fall back to the default account.' +
        (narrowed ? ' The repositories and Read only you chose for them with it are cleared.' : ''),
      confirmLabel: 'Disconnect',
      tone: 'danger',
    })))
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

  const showFlowPicker = methods.length > 1
  const isPatFlow = selectedFlow === 'personal_access_token'
  const isDeviceCodeFlow = selectedFlow === 'device_code'
  const patUrl = meta?.pat_instructions_url || ''
  const patPlaceholder = meta?.pat_placeholder || 'Paste your token…'
  const describe = (m: ConnectMethod): string => {
    if (!m.kind && m.flow === 'personal_access_token') {
      return meta?.pat_description ||
        (connectKinds.length ? GITHUB_TOKEN_SENTENCE : FLOW_DESCRIPTIONS[m.flow])
    }
    if (isGitHubApp(m.kind)) {
      return meta?.app_hosted ? HOSTED_GITHUB_APP_SENTENCE : GITHUB_APP_SENTENCE
    }
    if (m.kind) return GITHUB_SIGN_IN_SENTENCE
    return FLOW_DESCRIPTIONS[m.flow] || ''
  }

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
        <div className="text-sm text-primary">
          Connected as <strong>{account.display_email || account.account_label}</strong>
        </div>
      ) : null}

      {account && account.missing_scopes.length > 0 && (
        <div className="px-3 py-2 rounded-card bg-status-warning-bg border border-status-warning-border">
          <div className="text-sm text-status-warning-fg">
            ⚠️ Some services you ticked need more access. Click{' '}
            <strong>Reconnect</strong> to grant it.
          </div>
        </div>
      )}

      {account?.needs_reconnect && (
        <div className="px-3 py-2 rounded-card bg-status-warning-bg border border-status-warning-border">
          <div className="text-sm text-status-warning-fg">
            ⚠️ Reconnect needed: {reconnectReasonText(account.reconnect_reason)}.
            Agents leave this account out until you click <strong>Reconnect</strong>.
          </div>
        </div>
      )}

      {registeredClient && !account && (
        <div className="space-y-2">
          <p className="text-xs text-secondary">
            Signs you in at <strong>{registeredClient.resource_host}</strong>.
            The first time, OtoDock registers itself there. There is no
            OAuth app to create.
          </p>
          <Input
            type="text"
            value={registeredLabel}
            onChange={(e) => setRegisteredLabel(e.target.value)}
            placeholder="Account label, optional"
          />
        </div>
      )}

      {showFlowPicker && !account && (
        <div>
          <p className="text-xs text-secondary mb-2">
            Connection method:
          </p>
          <div className="space-y-1.5">
            {methods.map((m) => (
              <label
                key={m.id}
                className="flex items-start gap-2.5 cursor-pointer"
              >
                <input
                  type="radio"
                  name={`flow-${integration.mcp_name}`}
                  checked={method?.id === m.id}
                  onChange={() => setMethodId(m.id)}
                  className="mt-0.5 accent-action focus:ring-focus-ring"
                />
                <div>
                  <div className="text-sm font-medium text-primary">
                    {KIND_LABELS[m.kind] || FLOW_LABELS[m.flow] || m.flow}
                    {isGitHubApp(m.kind) && (
                      <span className={`${BADGE_BASE} ${BADGE_TONE.action} ml-1.5 align-middle`}>
                        Recommended
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-tertiary">{describe(m)}</div>
                </div>
              </label>
            ))}
          </div>
        </div>
      )}

      {githubApp && !account && !showFlowPicker && method && (
        <p className="text-xs text-secondary">{describe(method)}</p>
      )}

      {account && isGitHubApp(account.kind) && !githubApp && (
        <p className="text-xs text-status-warning-fg">
          Reconnecting uses GitHub sign-in now. The account then loses its
          installations.
        </p>
      )}
      {account && !isGitHubApp(account.kind) && githubApp && (
        <p className="text-xs text-status-warning-fg">
          Reconnecting uses the GitHub App now. The account then reaches only
          the repositories you install it on.
        </p>
      )}

      {services.length > 0 && !githubApp && (
        <div>
          <p className="text-xs text-secondary mb-2">
            Select which services to connect:
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {services.map((svc) => {
              const isUserOauthGated =
                !!svc.requires_user_oauth && hasServiceOnly
              const tooltip = isUserOauthGated
                ? 'Requires user OAuth. The admin has set up only Service-to-Server credentials. Ask your admin to add a user OAuth app.'
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
                    className="mt-0.5 accent-action disabled:cursor-not-allowed"
                  />
                  <div>
                    <div className="text-sm font-medium text-primary">
                      {svc.label}
                      {isUserOauthGated && (
                        <span className="ml-1 text-xs text-status-warning-fg">
                          Requires user OAuth
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-tertiary">
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
        <div className="px-3 py-2 rounded-card border border-status-danger-border bg-status-danger-bg">
          <div className="text-sm font-medium text-status-danger-fg mb-1">
            ⚠️ Sensitive capabilities
          </div>
          <ul className="text-xs text-status-danger-fg list-disc list-inside">
            {Array.from(capabilityWarnings).map((cap) => (
              <li key={cap}>{cap.replace(/_/g, ' ')}</li>
            ))}
          </ul>
        </div>
      )}

      {needsAdminConsent && !account && (
        <div className="px-3 py-2 rounded-card bg-status-warning-bg border border-status-warning-border">
          <div className="text-sm text-status-warning-fg mb-2">
            🔒 One or more selected services require <strong>tenant-admin
            consent</strong>. A Microsoft tenant admin must grant these
            scopes for the whole organization. Then users can connect.
          </div>
          <Button variant="secondary" size="xs"
            onClick={handleGrantAdminConsent}
            disabled={startAdminConsent.isPending}
          >
            {startAdminConsent.isPending
              ? 'Opening admin consent…'
              : 'Grant for whole tenant'}
          </Button>
        </div>
      )}

      {isDeviceCodeFlow && !account ? (
        <div className="space-y-2">
          {deviceCode ? (
            <div className="space-y-2">
              <div className="px-3 py-2 rounded-card bg-status-info-bg border border-status-info-border">
                <p className="text-xs text-status-info-fg mb-2">
                  1. Visit{' '}
                  <a
                    href={safeHref(
                      deviceCode.verification_uri_complete ||
                      deviceCode.verification_uri
                    )}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline font-medium"
                  >
                    {deviceCode.verification_uri}
                  </a>
                </p>
                <p className="text-xs text-status-info-fg mb-2">
                  2. Enter this code:
                </p>
                <div className="text-2xl font-mono font-bold text-primary select-all">
                  {deviceCode.user_code}
                </div>
                <p className="text-xs text-status-info-fg mt-2">
                  Waiting for you to complete the prompt in your browser…
                </p>
              </div>
              <div className="flex gap-2">
                <Button variant="secondary" size="md"
                  onClick={handleCancelDeviceCode}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <Button variant="primary" size="md"
                onClick={handleStartDeviceCode}
                disabled={
                  startDeviceCode.isPending ||
                  (selectedServices.length === 0 && services.length > 0)
                }
              >
                {startDeviceCode.isPending
                  ? 'Starting…'
                  : 'Get device code'}
              </Button>
            </div>
          )}
        </div>
      ) : isPatFlow && !account ? (
        <div className="space-y-2">
          {patUrl && (
            <p className="text-xs text-tertiary">
              Create a token at{' '}
              <a
                href={safeHref(patUrl)}
                target="_blank"
                rel="noopener noreferrer"
                className="underline text-link hover:text-link-hover"
              >
                {patUrl}
              </a>
              {services.length > 0
                ? ' with access to the services you ticked.'
                : '.'}
            </p>
          )}
          <textarea
            value={patValue}
            onChange={(e) => setPatValue(e.target.value)}
            placeholder={patPlaceholder}
            rows={3}
            className={`${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER} font-mono`}
          />
          <Input
            type="text"
            value={patLabel}
            onChange={(e) => setPatLabel(e.target.value)}
            placeholder="Account label, optional"
          />
          <p className="text-xs text-tertiary">
            Set a label to keep this token as its own account. Left empty, the
            account&apos;s own name is used. Saving then replaces the token of
            the account with that name.
          </p>
          <div className="flex gap-2">
            <Button variant="primary" size="md"
              onClick={handlePatSave}
              disabled={
                patSave.isPending ||
                !patValue.trim() ||
                (selectedServices.length === 0 && services.length > 0)
              }
            >
              {patSave.isPending ? 'Saving…' : 'Save token'}
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex gap-2">
          <Button variant="primary" size="md"
            onClick={handleConnect}
            disabled={
              startOAuth.isPending ||
              (!githubApp && selectedServices.length === 0 && services.length > 0)
            }
          >
            {startOAuth.isPending
              ? 'Connecting...'
              : account
                ? 'Reconnect'
                : 'Connect'}
          </Button>
          {account && (
            <Button variant="danger" size="md"
              onClick={handleDisconnect}
              disabled={disconnect.isPending}
            >
              {disconnect.isPending ? 'Disconnecting...' : 'Disconnect'}
            </Button>
          )}
        </div>
      )}

      {error && <div className="text-sm text-status-danger-fg">{error}</div>}
    </div>
  )
}
