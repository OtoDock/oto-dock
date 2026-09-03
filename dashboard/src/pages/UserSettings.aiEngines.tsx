import { useState, useEffect, useCallback, useRef } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { fetchCurrentUser } from '../api/auth'
import { setNativeAuthInProgress } from '../lib/nativeBridge'
import {
  useUserExecutionLayers,
  useUserDeleteSubscription,
  useUserUpdateSubscription,
  useStartClaudeOAuth,
  useExchangeClaudeOAuth,
  useStartOpenAIOAuth,
  useOpenAIOAuthStatus,
  useFinishOpenAIOAuth,
  type UserLayerInfo,
  type Subscription,
} from '../api/executionLayers'

// ---------------------------------------------------------------------------
// Execution Layers Section
// ---------------------------------------------------------------------------

// Mirrors the admin tab's STATUS_VARIANT — an expired row must never wear the
// green pill it used to.
const STATUS_CHIP: Record<string, string> = {
  active: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  expired: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  disabled: 'bg-gray-200 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
}

function UserLayerCard({ layer }: { layer: UserLayerInfo }) {
  const [expanded, setExpanded] = useState(false)
  const [showConnect, setShowConnect] = useState(false)
  const [oauthStep, setOauthStep] = useState<'idle' | 'code'>('idle')
  const [oauthState, setOauthState] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // The expired row a Reconnect click is trying to revive — the backend
  // matches by account identity, so if the user signs into a DIFFERENT
  // account we must say so instead of leaving the dead row unexplained.
  const [reconnectFor, setReconnectFor] = useState<Subscription | null>(null)

  // Block an install switch while the code-paste step is open (it would be lost).
  useEffect(() => {
    setNativeAuthInProgress(oauthStep !== 'idle')
    return () => setNativeAuthInProgress(false)
  }, [oauthStep])
  const isOpenAI = layer.name === 'codex-cli'
  const startClaude = useStartClaudeOAuth()
  const exchangeClaude = useExchangeClaudeOAuth()
  const startOpenAI = useStartOpenAIOAuth()
  const checkStatus = useOpenAIOAuthStatus()
  const finishOpenAI = useFinishOpenAIOAuth()
  const deleteSub = useUserDeleteSubscription()
  const updateSub = useUserUpdateSubscription()
  const { user, setUser } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [userCode, setUserCode] = useState('')
  const [authUrl, setAuthUrl] = useState('')

  // Device-code poll handle — kept in a ref so we can stop it from the Cancel
  // button and on unmount (otherwise it keeps hitting the status endpoint after
  // the card is closed / navigated away).
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const stopPoll = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])
  useEffect(() => stopPoll, [stopPoll])

  const userSubs = layer.user_subscriptions || []
  // "Your subscription" claims key on ACTIVE rows only — an expired row still
  // lists (so it can be reconnected) but must not report a working setup.
  const hasOwnSub = userSubs.some(s => s.status === 'active')
  const hasExpiredSub = userSubs.some(s => s.status === 'expired')
  const supportsOAuth = layer.name === 'claude-code-cli' || layer.name === 'codex-cli'

  // Keep the auth user's `has_own_engine` fresh so the global "connect an AI
  // engine" banner clears the instant the user connects one here. It derives
  // from /auth/me (fetched only on load), so without this the banner lingered
  // until a full page reload. Fires only when THIS engine layer's own-sub set
  // flips (not on mount), covering every connect path (OAuth + API key + delete).
  const prevHasOwnSub = useRef(hasOwnSub)
  useEffect(() => {
    const flipped = hasOwnSub !== prevHasOwnSub.current
    prevHasOwnSub.current = hasOwnSub
    if (!flipped || !supportsOAuth) return
    void (async () => {
      const u = await fetchCurrentUser()
      if (u) setUser(u)
    })()
  }, [hasOwnSub, supportsOAuth, setUser])

  // After a Reconnect-driven exchange, verify the revived row IS the one the
  // user clicked — signing into a different account creates a fresh row and
  // leaves the expired one behind, which without this reads as "nothing
  // happened".
  const finishReconnect = useCallback((sub?: Subscription) => {
    setReconnectFor(prev => {
      if (prev && sub && sub.id !== prev.id) {
        setNotice(
          `You signed in as ${sub.oauth_email || 'a different account'}, so a new ` +
          `subscription was added — “${prev.label || prev.oauth_email || 'the expired account'}” ` +
          `is still expired. Reconnect that exact account to revive it, or remove it.`,
        )
      }
      return null
    })
  }, [])

  const handleStartOAuth = useCallback(async () => {
    setError('')
    try {
      if (isOpenAI) {
        // OpenAI device code flow
        const result = await startOpenAI.mutateAsync({ layer: layer.name, ownerType: 'user' })
        setAuthUrl(result.url)
        setUserCode(result.user_code)
        setOauthStep('code')
        // Poll for completion (replace any prior poll first)
        stopPoll()
        const poll = setInterval(async () => {
          try {
            const status = await checkStatus.mutateAsync({ loginId: result.login_id })
            if (status.status === 'completed') {
              stopPoll()
              try {
                const fin = await finishOpenAI.mutateAsync({ loginId: result.login_id, layer: layer.name })
                finishReconnect(fin?.subscription)
              } catch { /* finish may 404 if already consumed — subscription still saved */ }
              setShowConnect(false)
              setOauthStep('idle')
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
          }
        }, 2000)
        pollRef.current = poll
      } else {
        // Claude code-paste flow
        const { url, state } = await startClaude.mutateAsync({ layer: layer.name, ownerType: 'user' })
        setOauthState(state)
        const { openOAuthWindow } = await import('../lib/oauth')
        await openOAuthWindow(url, 'claude-oauth')
        setTimeout(() => setOauthStep('code'), 2000)
      }
    } catch (e) {
      setError((e as Error).message)
    }
  }, [layer.name, isOpenAI, startOpenAI, startClaude, checkStatus, finishOpenAI, stopPoll, finishReconnect])

  const handleExchange = useCallback(async () => {
    if (!code.trim() || !oauthState) return
    setError('')
    try {
      const result = await exchangeClaude.mutateAsync({ code: code.trim(), state: oauthState, layer: layer.name })
      finishReconnect(result?.subscription)
      setShowConnect(false)
      setOauthStep('idle')
      setCode('')
    } catch (e) {
      setError((e as Error).message)
    }
  }, [code, oauthState, layer.name, exchangeClaude, finishReconnect])

  const handleReconnect = useCallback((sub: Subscription) => {
    setReconnectFor(sub)
    setNotice('')
    setShowConnect(true)
    void handleStartOAuth()
  }, [handleStartOAuth])

  const handleDelete = (sub: Subscription) => {
    if (confirm('Remove your subscription?')) {
      deleteSub.mutate({ layer: layer.name, id: sub.id })
    }
  }

  return (
    <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface">
      <button
        className="w-full flex items-center justify-between p-4 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-p-bg text-p-text-secondary border border-p-border-light">
              {isOpenAI ? 'OpenAI' : 'Anthropic'}
            </span>
            <span className="font-medium text-p-text">
              {layer.display_name.replace(/^(OpenAI|Anthropic)\s+/i, '')}
            </span>
          </div>
          <div className="text-sm text-p-text-secondary">
            {hasOwnSub
              ? 'Using your subscription'
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
            <span className="text-xs px-2 py-0.5 rounded-lg bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
              Reconnect Needed
            </span>
          ) : layer.platform_available ? (
            <span className="text-xs px-2 py-0.5 rounded-lg bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
              Platform
            </span>
          ) : (
            <span className="text-xs px-2 py-0.5 rounded-lg bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
              Not Connected
            </span>
          )}
          <svg className={`w-4 h-4 text-p-text-light transition-transform ${expanded ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>

      {expanded && (
        <div className="px-4 pb-4 border-t border-p-border-light pt-3 space-y-3">
          {/* Existing subscriptions */}
          {userSubs.map(sub => (
            <div key={sub.id} className="py-2 px-3 rounded-lg bg-p-bg">
              <div className="flex items-center justify-between gap-2">
                {/* min-w-0 + wrap keep long labels/emails inside the viewport
                    on mobile — without it the badge and Remove button get
                    pushed past the right edge. */}
                <div className="min-w-0 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className="text-sm font-medium text-p-text">{sub.label || 'Claude Subscription'}</span>
                  {sub.oauth_email && <span className="text-xs text-p-text-light truncate max-w-full">{sub.oauth_email}</span>}
                  <span className={`shrink-0 text-xs px-1.5 py-0.5 rounded-sm ${STATUS_CHIP[sub.status] || STATUS_CHIP.disabled}`}>
                    {sub.status}
                  </span>
                </div>
                <div className="shrink-0 flex items-center gap-2">
                  {sub.status === 'expired' && supportsOAuth && (
                    <button
                      onClick={() => handleReconnect(sub)}
                      className="text-xs px-2 py-1 rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors"
                    >
                      Reconnect
                    </button>
                  )}
                  <button
                    onClick={() => handleDelete(sub)}
                    className="text-xs text-red-500 hover:text-red-600 transition-colors"
                  >
                    Remove
                  </button>
                </div>
              </div>
              {sub.status === 'expired' && (
                <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
                  This account's login expired — reconnect the same account to revive it.
                </p>
              )}
              {sub.status === 'disabled' && (
                <p className="mt-1 text-xs text-p-text-light">
                  Disabled by an administrator.
                </p>
              )}
              {/* Per-account scope toggles. "Personal use" is for EVERY role —
                  with several accounts connected it benches one from the
                  owner's own sessions without disconnecting it (default on).
                  "Agent pool" (contribute to the shared platform pool) stays
                  admin-only, mirroring the server-side gate. */}
              {/* Scope toggles only mean something on a usable row — an
                  expired/disabled account can't serve sessions either way. */}
              <div className={`flex items-center gap-3 mt-1.5 ${sub.status !== 'active' ? 'opacity-50' : ''}`}>
                <label className={`flex items-center gap-1 text-xs text-p-text-light ${sub.status === 'active' ? 'cursor-pointer' : 'cursor-not-allowed'}`} title="Use this account for your own chats">
                  <input
                    type="checkbox"
                    checked={sub.use_personal}
                    disabled={sub.status !== 'active'}
                    onChange={(e) => updateSub.mutate({ layer: layer.name, id: sub.id, use_personal: e.target.checked })}
                  />
                  Personal use
                </label>
                {isAdmin && (
                  <label className={`flex items-center gap-1 text-xs text-p-text-light ${sub.status === 'active' ? 'cursor-pointer' : 'cursor-not-allowed'}`} title="Contribute this account to the shared agent pool">
                    <input
                      type="checkbox"
                      checked={sub.contribute_platform}
                      disabled={sub.status !== 'active'}
                      onChange={(e) => updateSub.mutate({ layer: layer.name, id: sub.id, contribute_platform: e.target.checked })}
                    />
                    Agent pool
                  </label>
                )}
              </div>
            </div>
          ))}

          {/* Platform status */}
          {!hasOwnSub && hasExpiredSub && (
            <p className="text-sm text-p-text-light">
              {isOpenAI ? 'ChatGPT' : 'Claude'} logins have a limited lifetime — reconnecting the
              same account above revives it in place.
            </p>
          )}
          {!hasOwnSub && !hasExpiredSub && layer.platform_available && (
            <p className="text-sm text-p-text-light">
              You're using platform API credentials provided by your administrator.
              Connect your own account to use your personal subscription instead.
            </p>
          )}
          {!hasOwnSub && !hasExpiredSub && !layer.platform_available && !layer.allow_platform_auth && (
            <p className="text-sm text-p-text-light">
              Platform subscriptions are disabled for your account. Connect your own subscription to use this layer.
            </p>
          )}
          {!hasOwnSub && !hasExpiredSub && !layer.platform_available && layer.allow_platform_auth && (
            <p className="text-sm text-p-text-light">
              No platform subscriptions are configured. Connect your own subscription to use this layer.
            </p>
          )}

          {/* Connect button */}
          {supportsOAuth && !showConnect && (
            <button
              onClick={() => setShowConnect(true)}
              className="px-4 py-2 text-sm rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors"
            >
              {userSubs.length > 0 ? 'Add Another Account' : `Connect Your ${isOpenAI ? 'ChatGPT' : 'Claude'} Account`}
            </button>
          )}

          {/* OAuth flow */}
          {showConnect && oauthStep === 'idle' && (
            <div className="p-3 bg-p-bg rounded-lg border border-p-border-light space-y-2">
              <p className="text-sm text-p-text">
                {isOpenAI
                  ? 'Click below to sign in with your ChatGPT account.'
                  : 'Click below to authenticate with your Anthropic account. A popup will open for login.'}
              </p>
              <div className="flex gap-2">
                <button
                  onClick={handleStartOAuth}
                  disabled={isOpenAI ? startOpenAI.isPending : startClaude.isPending}
                  className="px-3 py-1.5 text-sm rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors disabled:opacity-40"
                >
                  {(isOpenAI ? startOpenAI.isPending : startClaude.isPending) ? 'Starting...' : 'Connect'}
                </button>
                <button
                  onClick={() => { setShowConnect(false); setReconnectFor(null) }}
                  className="px-3 py-1.5 text-sm rounded-lg text-p-text-secondary hover:bg-p-bg-hover transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {showConnect && oauthStep === 'code' && isOpenAI && (
            <div className="p-3 bg-p-bg rounded-lg border border-p-border-light space-y-3">
              <p className="text-sm font-medium text-p-text">Sign in with ChatGPT</p>
              <div className="space-y-1">
                <p className="text-xs text-p-text-secondary">1. Open this link and sign in:</p>
                <div className="flex items-center gap-2">
                  <a href={authUrl} target="_blank" rel="noopener noreferrer" className="text-sm text-brand hover:underline truncate">{authUrl}</a>
                  <button onClick={() => window.open(authUrl, '_blank')} className="shrink-0 px-2 py-1 text-xs rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors">Open</button>
                </div>
              </div>
              <div className="space-y-1">
                <p className="text-xs text-p-text-secondary">2. Enter this one-time code:</p>
                <div className="flex items-center gap-2">
                  <span className="px-4 py-2 text-lg font-mono font-bold tracking-widest bg-white dark:bg-gray-800 border border-p-border-light rounded-lg text-p-text select-all">{userCode}</span>
                  <button onClick={() => navigator.clipboard.writeText(userCode)} className="px-2 py-1 text-xs rounded-lg text-p-text-secondary hover:bg-p-bg-hover transition-colors border border-p-border-light">Copy</button>
                </div>
              </div>
              <div className="flex items-center gap-2 text-xs text-p-text-secondary">
                <svg className="animate-spin h-3.5 w-3.5 text-brand" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" /></svg>
                Waiting for authentication...
              </div>
              <button onClick={() => { stopPoll(); setShowConnect(false); setOauthStep('idle'); setReconnectFor(null) }} className="text-xs text-p-text-secondary hover:text-p-text transition-colors">Cancel</button>
            </div>
          )}

          {showConnect && oauthStep === 'code' && !isOpenAI && (
            <div className="p-3 bg-p-bg rounded-lg border border-p-border-light space-y-2">
              <p className="text-sm text-p-text">
                Copy the authorization code from the Anthropic page and paste it below.
              </p>
              <input
                type="text"
                placeholder="Paste authorization code here"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                autoFocus
                className="w-full px-3 py-1.5 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 font-mono"
              />
              <div className="flex gap-2">
                <button
                  onClick={handleExchange}
                  disabled={!code.trim() || exchangeClaude.isPending}
                  className="px-3 py-1.5 text-sm rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors disabled:opacity-40"
                >
                  {exchangeClaude.isPending ? 'Connecting...' : 'Connect'}
                </button>
                <button
                  onClick={() => { setShowConnect(false); setOauthStep('idle'); setCode(''); setReconnectFor(null) }}
                  className="px-3 py-1.5 text-sm rounded-lg text-p-text-secondary hover:bg-p-bg-hover transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {notice && <p className="text-xs text-amber-600 dark:text-amber-400">{notice}</p>}
          {error && <p className="text-xs text-red-500">{error}</p>}
        </div>
      )}
    </div>
  )
}

export function ExecutionLayersSection() {
  const { data: layers, isLoading } = useUserExecutionLayers()

  if (isLoading) return null
  if (!layers || layers.length === 0) return null

  // Only show layers that support OAuth (claude-code-cli for now)
  const oauthLayers = layers.filter(l => l.name === 'claude-code-cli' || l.name === 'codex-cli')
  if (oauthLayers.length === 0) return null

  return (
    <div className="mb-8">
      <h2 className="text-lg font-medium text-p-text mb-3">AI Engines</h2>
      <p className="text-sm text-p-text-secondary mb-4">
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
