import { useState, useEffect, useCallback, useLayoutEffect, useRef, type RefObject } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { isAdmin as isPlatformAdmin } from '../lib/permissions'
import { toastError } from '../lib/http/apiJson'
import { fetchCurrentUser } from '../api/auth'
import { setNativeAuthInProgress } from '../lib/nativeBridge'
import { SubscriptionWindowBars, BalanceHint } from '../components/engines/SubscriptionWindows'
import { ApiKeyForm, type ApiKeyMutation } from '../components/engines/AddApiKeyForm'
import Dialog from '../components/ui/Dialog'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'
import InvokedForm from '../components/common/InvokedForm'
import { primaryPointerCoarse } from '../components/common/useInvokedForm'
import {
  useUserExecutionLayers,
  useUserAddSubscription,
  useUserDeleteSubscription,
  useUserUpdateSubscription,
  useStartClaudeOAuth,
  useExchangeClaudeOAuth,
  useStartOpenAIOAuth,
  useOpenAIOAuthStatus,
  useFinishOpenAIOAuth,
  loginAlreadyFinished,
  alreadyConnectedMessage,
  ClaudeExchangeError,
  CLAUDE_SECOND_ACCOUNT_HINT,
  type ClaudeExchangeResult,
  type UserLayerInfo,
  type Subscription,
} from '../api/executionLayers'
import {
  accountLabel, engineLabel, isCoding, keyProviders, oauthFlow, sortEngineRows, supportsOAuth, vendorBadge,
} from '../lib/engines'
import { ENGINE_SUBSCRIPTION_STATUS, type EngineSubscriptionStatus } from '../lib/status/engineSubscription'
import { useDialogs } from '../components/ui/dialogs'
import { BADGE_BASE, BADGE_TONE, CARD, CARD_BORDERED } from '../components/ui/classes'
import { Badge, SaveState } from '../components/ui/SettingsControls'
import { useSaveState } from '../hooks/useSaveState'
import InfoTip from '../components/ui/InfoTip'
import { PAGE_TITLE } from '../components/ui/classes'

// ---------------------------------------------------------------------------
// Execution Layers Section
// ---------------------------------------------------------------------------

// Mirrors the admin tab's STATUS_VARIANT — an expired row must never wear the
// green pill it used to.
const STATUS_CHIP: Record<EngineSubscriptionStatus, keyof typeof BADGE_TONE> = {
  active: 'success',
  expired: 'warning',
  disabled: 'neutral',
}

function UserLayerCard({ layer }: { layer: UserLayerInfo }) {
  const dialogs = useDialogs()
  const [expanded, setExpanded] = useState(false)
  const [showApiKey, setShowApiKey] = useState(false)
  // The open connect dialog; `reconnect` is the expired row a Reconnect
  // click is trying to revive. The backend matches by account identity, so
  // the dialog checks the account the person signed in as.
  const [connect, setConnect] = useState<{ reconnect: Subscription | null } | null>(null)
  const apiKeyButtonRef = useRef<HTMLButtonElement>(null)
  // The card's facts come from the engine's descriptor, never its id: the
  // login flow decides which connect box renders (a device code the page
  // polls on, or a popup whose code the user pastes), the identity labels
  // name the vendor and the account, keyProviders names the key's vendor.
  const engine = layer.capabilities
  const vendor = engine.identity.vendor_label
  const account = accountLabel(engine)
  const deleteSub = useUserDeleteSubscription()
  const updateSub = useUserUpdateSubscription()
  const { stateOf, run } = useSaveState()
  const addKey = useUserAddSubscription()
  const { user, setUser } = useAuth()
  const isAdmin = isPlatformAdmin(user)

  const userSubs = layer.user_subscriptions || []
  // "Your subscription" claims key on ACTIVE rows only — an expired row still
  // lists (so it can be reconnected) but must not report a working setup.
  const hasOwnSub = userSubs.some(s => s.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE)
  const hasOwnOAuth = userSubs.some(s => s.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE && s.auth_type !== 'api_key')
  const hasExpiredSub = userSubs.some(s => s.status === ENGINE_SUBSCRIPTION_STATUS.EXPIRED)
  const hasLogin = supportsOAuth(engine)
  const keyProviderList = keyProviders(engine)
  // The server's has_own_engine counts a personal subscription on a CODING
  // engine; the refresh below mirrors that predicate.
  const countsAsOwnEngine = isCoding(engine)

  // Keep the auth user's `has_own_engine` fresh so the global "connect an AI
  // engine" banner clears the instant the user connects one here. It derives
  // from /auth/me (fetched only on load), so without this the banner lingered
  // until a full page reload. Fires only when THIS engine layer's own-sub set
  // flips (not on mount), covering every connect path (OAuth + API key + delete).
  const prevHasOwnSub = useRef(hasOwnSub)
  useEffect(() => {
    const flipped = hasOwnSub !== prevHasOwnSub.current
    prevHasOwnSub.current = hasOwnSub
    if (!flipped || !countsAsOwnEngine) return
    void (async () => {
      const u = await fetchCurrentUser()
      if (u) setUser(u)
    })()
  }, [hasOwnSub, countsAsOwnEngine, setUser])

  const handleDelete = async (sub: Subscription) => {
    const title = sub.auth_type === 'api_key' ? 'Remove your API key?' : 'Remove your subscription?'
    if (await dialogs.confirm({ title, confirmLabel: 'Remove', tone: 'danger' })) {
      void deleteSub.mutateAsync({ layer: layer.name, id: sub.id }).catch(toastError(
        sub.auth_type === 'api_key' ? 'The API key was not removed' : 'The subscription was not removed',
      ))
    }
  }

  // The pill's name: the row's label, else what the row is on this engine.
  const subLabel = (sub: Subscription) =>
    sub.label || (sub.auth_type === 'api_key'
      ? `${vendor} API key`
      : `${account} Subscription`)

  return (
    <div className={CARD}>
      <button
        type="button"
        aria-expanded={expanded}
        className="w-full flex items-center justify-between p-4 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            {vendorBadge(engine) && (
              <span className="shrink-0 text-3xs font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-surface-muted text-secondary">
                {vendorBadge(engine)}
              </span>
            )}
            <span className="font-medium text-primary">{engineLabel(engine)}</span>
          </div>
          <div className="text-sm text-secondary">
            {hasOwnOAuth
              ? 'Using your subscription'
              : hasOwnSub
                ? 'Using your API key'
              : hasExpiredSub
                ? 'Your subscription needs reconnecting'
                : layer.platform_available
                  ? 'Using platform subscription'
                  : 'No subscription available'}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* No "Your Account" chip when the user has their own subscription —
              the subtitle already says so, and the green badge looked cramped
              on mobile. Reconnect / Platform / Not Connected remain informative. */}
          {hasOwnSub ? null : hasExpiredSub ? (
            <Badge variant="warning">
              Reconnect Needed
            </Badge>
          ) : layer.platform_available ? (
            <Badge variant="info">
              Platform
            </Badge>
          ) : (
            <Badge variant="warning">
              Not Connected
            </Badge>
          )}
          <svg className={`w-4 h-4 text-tertiary transition-transform ${expanded ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>

      {expanded && (
        <div className="px-4 pb-4 border-t border-subtle pt-3 space-y-3">
          {/* Existing subscriptions */}
          {userSubs.map(sub => (
            <div key={sub.id} className={`${CARD_BORDERED} py-2 px-3`}>
              <div className="flex items-center justify-between gap-2">
                {/* min-w-0 + wrap keep long labels/emails inside the viewport
                    on mobile — without it the badge and Remove button get
                    pushed past the right edge. */}
                <div className="min-w-0 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className="text-sm font-medium text-primary">{subLabel(sub)}</span>
                  {sub.auth_type === 'api_key' && (
                    <span className={`${BADGE_BASE} ${BADGE_TONE.neutral} shrink-0`}>
                      API key
                    </span>
                  )}
                  {sub.oauth_email && <span className="text-xs text-tertiary truncate max-w-full">{sub.oauth_email}</span>}
                  <span className={`${BADGE_BASE} ${BADGE_TONE[STATUS_CHIP[sub.status] || STATUS_CHIP[ENGINE_SUBSCRIPTION_STATUS.DISABLED]]} shrink-0`}>
                    {sub.status}
                  </span>
                </div>
                <div className="shrink-0 flex items-center gap-2">
                  {sub.status === ENGINE_SUBSCRIPTION_STATUS.EXPIRED && hasLogin && (
                    <Button
                      variant="primary"
                      size="xs"
                      onClick={() => setConnect({ reconnect: sub })}
                    >
                      Reconnect
                    </Button>
                  )}
                  <button
                    onClick={() => handleDelete(sub)}
                    className="text-xs text-status-danger-fg hover:underline"
                  >
                    Remove
                  </button>
                </div>
              </div>
              {sub.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE && sub.auth_type !== 'api_key' && (
                <SubscriptionWindowBars windows={sub.windows} />
              )}
              {sub.status === ENGINE_SUBSCRIPTION_STATUS.EXPIRED && (
                <p className="mt-1 text-xs text-status-warning-fg">
                  This account's login expired. Reconnect the same account to revive it.
                </p>
              )}
              {sub.status === ENGINE_SUBSCRIPTION_STATUS.DISABLED && (
                <p className="mt-1 text-xs text-tertiary">
                  Disabled by an administrator.
                </p>
              )}
              {/* Per-account scope toggles. "Personal use" is for EVERY role —
                  with several accounts connected it benches one from the
                  owner's own sessions without disconnecting it (default on).
                  "Shared with agents" (contribute to the shared platform pool) stays
                  admin-only, mirroring the server-side gate. */}
              {/* Scope toggles only mean something on a usable row — an
                  expired/disabled account can't serve sessions either way. */}
              <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 ${sub.status !== ENGINE_SUBSCRIPTION_STATUS.ACTIVE ? 'opacity-50' : ''}`}>
                <span className="inline-flex items-center">
                  <label className={`flex items-center gap-1 text-xs text-tertiary ${sub.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE ? 'cursor-pointer' : 'cursor-not-allowed'}`}>
                    <input
                      type="checkbox"
                      checked={sub.use_personal}
                      disabled={sub.status !== ENGINE_SUBSCRIPTION_STATUS.ACTIVE}
                      onChange={(e) => {
                        const on = e.target.checked
                        void run(`${sub.id}:personal`, () => updateSub.mutateAsync({ layer: layer.name, id: sub.id, use_personal: on }), { failTitle: 'The account setting was not saved' })
                      }}
                      className="accent-action"
                    />
                    Personal use
                  </label>
                  <InfoTip label="Personal use">Use this account for your own chats</InfoTip>
                  <SaveState state={stateOf(`${sub.id}:personal`)} />
                </span>
                {isAdmin && (
                  <span className="inline-flex items-center">
                    <label className={`flex items-center gap-1 text-xs text-tertiary ${sub.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE ? 'cursor-pointer' : 'cursor-not-allowed'}`}>
                      <input
                        type="checkbox"
                        checked={sub.contribute_platform}
                        disabled={sub.status !== ENGINE_SUBSCRIPTION_STATUS.ACTIVE}
                        onChange={(e) => {
                          const on = e.target.checked
                          void run(`${sub.id}:shared`, () => updateSub.mutateAsync({ layer: layer.name, id: sub.id, contribute_platform: on }), { failTitle: 'The account setting was not saved' })
                        }}
                        className="accent-action"
                      />
                      Shared with agents
                    </label>
                    <InfoTip label="Shared with agents">Add this account to the shared subscriptions your agents use</InfoTip>
                    <SaveState state={stateOf(`${sub.id}:shared`)} />
                  </span>
                )}
              </div>
            </div>
          ))}
          <BalanceHint oauthCount={userSubs.filter((s) => s.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE && s.auth_type !== 'api_key').length} />

          {/* Platform status */}
          {!hasOwnSub && hasExpiredSub && (
            <p className="text-sm text-tertiary">
              {account} logins have a limited lifetime. Reconnecting the
              same account above revives it in place.
            </p>
          )}
          {!hasOwnSub && !hasExpiredSub && layer.platform_available && (
            <p className="text-sm text-tertiary">
              You're using platform API credentials provided by your administrator.
              Connect your own account to use your personal subscription instead.
            </p>
          )}
          {!hasOwnSub && !hasExpiredSub && !layer.platform_available && !layer.allow_platform_auth && (
            <p className="text-sm text-tertiary">
              Platform subscriptions are disabled for your account. Connect your own subscription to use this engine.
            </p>
          )}
          {!hasOwnSub && !hasExpiredSub && !layer.platform_available && layer.allow_platform_auth && (
            <p className="text-sm text-tertiary">
              No platform subscriptions are configured. Connect your own subscription to use this engine.
            </p>
          )}

          {/* Connect button, and the pay-as-you-go alternative */}
          {hasLogin && !showApiKey && (
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="primary"
                size="md"
                onClick={() => setConnect({ reconnect: null })}
              >
                {userSubs.length > 0 ? 'Add another account' : `Connect your ${account} account`}
              </Button>
              <button
                ref={apiKeyButtonRef}
                onClick={() => setShowApiKey(true)}
                className="text-xs text-link hover:underline"
              >
                Use an API key instead
              </button>
            </div>
          )}
          {showApiKey && (
            <InPlaceApiKeyForm
              layer={layer.name} providers={keyProviderList}
              mutation={addKey} onDone={() => setShowApiKey(false)} returnTo={apiKeyButtonRef}
            />
          )}
          {connect && (
            <EngineConnectDialog
              layer={layer}
              reconnect={connect.reconnect}
              adding={userSubs.length > 0}
              onClose={() => setConnect(null)}
            />
          )}
        </div>
      )}
    </div>
  )
}

// The connect flow, inside a dialog mounted only while it is open: closing
// it unmounts it, which stops the device-code poll, the popup timer and the
// native auth flag; `aliveRef` drops every answer that lands after that.
// A Reconnect opens on the same first step, which names the account to sign
// in as before the vendor's page opens: signing in as another account is
// how a Reconnect fails to revive its row.
function EngineConnectDialog({ layer, reconnect, adding, onClose }: {
  layer: UserLayerInfo
  reconnect: Subscription | null
  adding: boolean
  onClose: () => void
}) {
  const engine = layer.capabilities
  const isDeviceCode = oauthFlow(engine) === 'device_code'
  const vendor = engine.identity.vendor_label
  const account = accountLabel(engine)
  const startClaude = useStartClaudeOAuth()
  const exchangeClaude = useExchangeClaudeOAuth()
  const startOpenAI = useStartOpenAIOAuth()
  const checkStatus = useOpenAIOAuthStatus()
  const finishOpenAI = useFinishOpenAIOAuth()
  // 'waiting': the popup opened, the paste box follows. 'retry': a Claude
  // paste was refused and a fresh flow is ready; the popup opens only on the
  // Try again click (a window.open outside a gesture is blocked by
  // browsers). 'other-account': a Reconnect signed in as another account,
  // which the dialog says until Done.
  const [oauthStep, setOauthStep] = useState<'idle' | 'waiting' | 'code' | 'retry' | 'other-account'>('idle')
  const [oauthState, setOauthState] = useState('')
  const [claudeAuthUrl, setClaudeAuthUrl] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [userCode, setUserCode] = useState('')
  const [authUrl, setAuthUrl] = useState('')
  // Nothing closes the dialog while an exchange or a finish runs (the account
  // would be stored behind a closed dialog). A start may hang: Cancel stays,
  // and the liveness guard drops its late answer.
  const busy = exchangeClaude.isPending || finishOpenAI.isPending
  const starting = startClaude.isPending || startOpenAI.isPending
  const startRef = useRef<HTMLButtonElement>(null)
  const stepRef = useRef<HTMLDivElement>(null)

  const aliveRef = useRef(true)
  // A status call slower than the poll's period must not overlap the next:
  // two "completed" answers would finish twice.
  const pollingRef = useRef(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const stopPoll = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      stopPoll()
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [stopPoll])

  // Block an install switch while a step holds state the switch would lose.
  useEffect(() => {
    setNativeAuthInProgress(oauthStep !== 'idle' && oauthStep !== 'other-account')
    return () => setNativeAuthInProgress(false)
  }, [oauthStep])

  // A step change unmounts the control that held the focus: give it to the
  // new step's field (on a phone the field stays the next tap) or button.
  useLayoutEffect(() => {
    const el = stepRef.current
    const active = document.activeElement
    // The step region holds the focus only between two steps.
    if (!el || (el.contains(active) && active !== el)) return
    const field = primaryPointerCoarse() ? null : el.querySelector<HTMLElement>('input:not([disabled])')
    const target = field ?? el.querySelector<HTMLElement>('button:not([disabled])') ?? el
    target.focus({ preventScroll: true })
  }, [oauthStep])

  // After a Reconnect, verify the revived row IS the one the user clicked:
  // signing into a different account creates a fresh row (or refreshes
  // another connected one) and leaves the expired one behind, which without
  // this reads as "nothing happened". `created` tells the two apart:
  // undefined for the ChatGPT flow, which has no such flag. False when the
  // dialog must stay open to say so.
  const reconnectedRightAccount = (sub?: Subscription, created?: boolean): boolean => {
    if (!reconnect || !sub || sub.id === reconnect.id) return true
    const who = sub.oauth_email || 'a different account'
    const dead = reconnect.label || reconnect.oauth_email || 'the expired account'
    setNotice(
      created === false
        ? `You signed in as ${who}, which is already connected. “${dead}” ` +
          `is still expired. Reconnect that exact account to revive it, or remove it.`
        : `You signed in as ${who}, so a new ` +
          `subscription was added. “${dead}” ` +
          `is still expired. Reconnect that exact account to revive it, or remove it.`,
    )
    setOauthStep('other-account')
    return false
  }

  // A Connect / Add Another Account paste that matched an account already
  // connected: say so and stay on the first step, instead of closing as if
  // a second account had been added.
  const connectedNewAccount = (result: ClaudeExchangeResult): boolean => {
    if (result.created || result.previous_status !== ENGINE_SUBSCRIPTION_STATUS.ACTIVE) return true
    setNotice(alreadyConnectedMessage(result.subscription?.oauth_email || ''))
    return false
  }

  const openClaudePopup = async (url: string) => {
    const { openOAuthWindow } = await import('../lib/oauth')
    if (!aliveRef.current) return
    const opened = await openOAuthWindow(url, 'claude-oauth')
    if (!aliveRef.current) return
    if (!opened) {
      setError('The login popup was blocked. Allow popups for this site, then try again.')
      setOauthStep('retry')
      return
    }
    setOauthStep('waiting')
    timerRef.current = setTimeout(() => setOauthStep('code'), 2000)
  }

  const handleStartOAuth = async () => {
    setError('')
    setNotice('')
    try {
      if (isDeviceCode) {
        // Device-code flow: a verification URL + one-time code, polled to completion
        const result = await startOpenAI.mutateAsync({ layer: layer.name, ownerType: 'user' })
        if (!aliveRef.current) return
        setAuthUrl(result.url)
        setUserCode(result.user_code)
        setOauthStep('code')
        // Poll for completion (replace any prior poll first)
        stopPoll()
        pollRef.current = setInterval(async () => {
          if (pollingRef.current) return
          pollingRef.current = true
          try {
            const status = await checkStatus.mutateAsync({ loginId: result.login_id })
            if (!aliveRef.current) return
            if (status.status === 'completed') {
              stopPoll()
              try {
                const fin = await finishOpenAI.mutateAsync({ loginId: result.login_id, layer: layer.name })
                if (!aliveRef.current) return
                if (!reconnectedRightAccount(fin?.subscription)) return
              } catch (err) {
                if (!aliveRef.current) return
                if (!loginAlreadyFinished(err)) {
                  setError((err as Error).message)
                  setOauthStep('idle')
                  return
                }
              }
              onClose()
            } else if (status.status === 'failed') {
              stopPoll()
              setError(status.message || 'Login failed')
              setOauthStep('idle')
            }
          } catch (err) {
            // 404 = login session consumed (already finished) — stop polling
            if (err instanceof Error && (err.message.includes('404') || err.message.includes('not found'))) {
              stopPoll()
            }
          } finally {
            pollingRef.current = false
          }
        }, 2000)
      } else {
        // Code-paste flow: a popup whose page shows a code the user pastes back
        const { url, state } = await startClaude.mutateAsync({ layer: layer.name, ownerType: 'user' })
        if (!aliveRef.current) return
        setOauthState(state)
        setClaudeAuthUrl(url)
        await openClaudePopup(url)
      }
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message)
    }
  }

  const handleExchange = async () => {
    if (!code.trim() || !oauthState) return
    setError('')
    try {
      const result = await exchangeClaude.mutateAsync({ code: code.trim(), state: oauthState, layer: layer.name })
      if (!aliveRef.current) return
      setCode('')
      setOauthStep('idle')
      const done = reconnect
        ? reconnectedRightAccount(result?.subscription, result?.created)
        : connectedNewAccount(result)
      if (done) onClose()
    } catch (e) {
      if (!aliveRef.current) return
      setError((e as Error).message)
      // The state was consumed by the refused paste: prepare a fresh flow so
      // one click retries, without a popup until that click.
      if (e instanceof ClaudeExchangeError && e.status === 403) return
      try {
        const { url, state } = await startClaude.mutateAsync({ layer: layer.name, ownerType: 'user' })
        if (!aliveRef.current) return
        setOauthState(state)
        setClaudeAuthUrl(url)
        setCode('')
        setOauthStep('retry')
      } catch (e2) {
        if (aliveRef.current) setError(`${(e as Error).message} ${(e2 as Error).message}`)
      }
    }
  }

  const who = reconnect ? (reconnect.oauth_email || reconnect.label) : ''
  const title = reconnect
    ? `Reconnect ${who || `your ${account} account`}`
    : adding ? `Add another ${account} account` : `Connect your ${account} account`

  return (
    <Dialog
      onClose={onClose}
      title={title}
      placement="sheet"
      dismissible="escape"
      busy={busy}
      initialFocus={startRef}
    >
      <div ref={stepRef} tabIndex={-1} className="px-4 py-3 space-y-3 outline-none">
        {oauthStep === 'idle' && (
          <div className="space-y-2">
            <p className="text-sm text-primary">
              {reconnect && who
                ? isDeviceCode
                  ? `Click below to sign in again as ${who}.`
                  : `Click below to sign in again as ${who}. A popup will open for login.`
                : isDeviceCode
                  ? `Click below to sign in with your ${account} account.`
                  : `Click below to authenticate with your ${vendor} account. A popup will open for login.`}
            </p>
            {!isDeviceCode && (
              <p className="text-xs text-tertiary">{CLAUDE_SECOND_ACCOUNT_HINT}</p>
            )}
            <div className="flex gap-2">
              <Button
                variant="primary"
                size="sm"
                ref={startRef}
                onClick={() => { void handleStartOAuth() }}
                disabled={starting}
              >
                {starting ? 'Starting...' : 'Connect'}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onClose}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        {oauthStep === 'code' && isDeviceCode && (
          <div className="space-y-3">
            <p className="text-sm font-medium text-primary">Sign in with {account}</p>
            <div className="space-y-1">
              <p className="text-xs text-secondary">1. Open this link and sign in:</p>
              <div className="flex items-center gap-2">
                <a href={authUrl} target="_blank" rel="noopener noreferrer" className="text-sm text-link hover:underline truncate">{authUrl}</a>
                <Button variant="primary" size="xs" className="shrink-0" onClick={() => window.open(authUrl, '_blank', 'noopener,noreferrer')}>Open</Button>
              </div>
            </div>
            <div className="space-y-1">
              <p className="text-xs text-secondary">2. Enter this one-time code:</p>
              <div className="flex items-center gap-2">
                <span className="px-4 py-2 text-lg font-mono font-bold tracking-widest bg-surface-sunken border border-subtle rounded-control text-primary select-all">{userCode}</span>
                <Button variant="secondary" size="xs" onClick={() => navigator.clipboard.writeText(userCode)}>Copy</Button>
              </div>
            </div>
            <div role="status" className="flex items-center gap-2 text-xs text-secondary">
              <svg className="animate-spin h-3.5 w-3.5 text-secondary" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" /></svg>
              Waiting for authentication...
            </div>
            <Button variant="ghost" size="xs" onClick={onClose} disabled={busy}>Cancel</Button>
          </div>
        )}

        {oauthStep === 'retry' && !isDeviceCode && (
          <div className="space-y-2">
            <p className="text-xs text-tertiary">
              Click Try again to open the login popup once more and paste the new code.
            </p>
            <div className="flex gap-2">
              <Button
                variant="primary"
                size="sm"
                onClick={() => { setError(''); void openClaudePopup(claudeAuthUrl) }}
              >
                Try again
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onClose}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        {(oauthStep === 'waiting' || oauthStep === 'code') && !isDeviceCode && (
          <div className="space-y-2">
            <p className="text-sm text-primary" role="status">
              {oauthStep === 'waiting'
                ? `A popup window opened for ${vendor} login. After you authenticate, a code will be shown on the page.`
                : `Copy the authorization code from the ${vendor} page and paste it below.`}
            </p>
            <p className="text-xs text-tertiary">{CLAUDE_SECOND_ACCOUNT_HINT}</p>
            {oauthStep === 'code' && (
              <>
                <Input
                  type="text"
                  aria-label="Authorization code"
                  placeholder="Paste authorization code here"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  className="font-mono"
                />
                <div className="flex gap-2">
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={() => { void handleExchange() }}
                    disabled={!code.trim() || exchangeClaude.isPending}
                  >
                    {exchangeClaude.isPending ? 'Connecting...' : 'Connect'}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={onClose}
                    disabled={busy}
                  >
                    Cancel
                  </Button>
                </div>
              </>
            )}
          </div>
        )}

        {oauthStep === 'other-account' && (
          <div className="flex gap-2">
            <Button
              variant="primary"
              size="sm"
              onClick={onClose}
            >
              Done
            </Button>
          </div>
        )}

        {notice && <p className="text-xs text-status-warning-fg" role="status">{notice}</p>}
        {error && <p className="text-xs text-status-danger-fg" role="alert">{error}</p>}
      </div>
    </Dialog>
  )
}

// "Use an API key instead" opens in place of its button row: the form takes
// the focus on a desktop and gives it back to the button when it closes.
function InPlaceApiKeyForm({ layer, providers, mutation, onDone, returnTo }: {
  layer: string
  providers: ReturnType<typeof keyProviders>
  mutation: ApiKeyMutation
  onDone: () => void
  returnTo: RefObject<HTMLElement | null>
}) {
  return (
    <InvokedForm onCancel={onDone} fallback={returnTo}>
      {(close) => (
        <ApiKeyForm
          layer={layer} providers={providers} ownerType="user"
          mutation={mutation} onDone={() => close(onDone)}
        />
      )}
    </InvokedForm>
  )
}

export function ExecutionLayersSection() {
  const { data: layers, isLoading } = useUserExecutionLayers()

  if (isLoading) return null
  if (!layers || layers.length === 0) return null

  // The engines a user can connect an account to — those whose descriptor
  // takes a login — in engine order.
  const oauthLayers = sortEngineRows(layers.filter(l => supportsOAuth(l.capabilities)))
  if (oauthLayers.length === 0) return null

  return (
    <div className="mb-8">
      <h2 className={`${PAGE_TITLE} mb-3`}>AI Engines</h2>
      <p className="text-sm text-secondary mb-4">
        Connect your subscriptions. They’ll be used to run your own chats and agents.
      </p>
      <div className="space-y-3">
        {oauthLayers.map(layer => (
          <UserLayerCard key={layer.name} layer={layer} />
        ))}
      </div>
    </div>
  )
}
