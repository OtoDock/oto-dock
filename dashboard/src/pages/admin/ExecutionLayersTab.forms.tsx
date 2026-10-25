/**
 * Add/connect forms for the Execution Layers tab split.
 *
 * Of the split's files it imports only the leaf module .labels (PROVIDER_LABELS).
 */

import { useState, useEffect, useCallback, useLayoutEffect, useRef } from 'react'
import { setNativeAuthInProgress } from '../../lib/nativeBridge'
import {
  useAddSubscription,
  useAddModel,
  useStartClaudeOAuth,
  useExchangeClaudeOAuth,
  useStartOpenAIOAuth,
  useOpenAIOAuthStatus,
  useFinishOpenAIOAuth,
  loginAlreadyFinished,
  useBulkAddModels,
  alreadyConnectedMessage,
  ClaudeExchangeError,
  CLAUDE_SECOND_ACCOUNT_HINT,
  type DiscoveredModel,
} from '../../api/executionLayers'
import type { EngineDescriptor, EngineProvider } from '../../api/engineDescriptor'
import { accountLabel, oauthFlow } from '../../lib/engines'
import { ENGINE_SUBSCRIPTION_STATUS } from '../../lib/status/engineSubscription'
import { Badge } from '../../components/ui/SettingsControls'
import InfoTip from '../../components/ui/InfoTip'
import { CopyButton } from '../../components/CopyButton'
import { ApiKeyForm } from '../../components/engines/AddApiKeyForm'
import Dialog from '../../components/ui/Dialog'
import Button from '../../components/ui/Button'
import { Input } from '../../components/ui/Field'
import { primaryPointerCoarse, useInvokedForm } from '../../components/common/useInvokedForm'
import InvokedForm from '../../components/common/InvokedForm'
import { PROVIDER_LABELS } from './ExecutionLayersTab.labels'
import { CARD_BORDERED, FIELD, FIELD_BORDER, FIELD_REGULAR } from '../../components/ui/classes'

// ---------------------------------------------------------------------------
// Add API Key Form — the admin binding of the shared form. `providers` are
// the engine's key-taking providers (lib/engines keyProviders): a select
// appears only when there is more than one.
// ---------------------------------------------------------------------------

export function AddApiKeyForm({ layer, providers, onDone }: { layer: string; providers: EngineProvider[]; onDone: () => void }) {
  const addMut = useAddSubscription()
  return (
    <InvokedForm onCancel={onDone}>
      {(close) => (
        <ApiKeyForm layer={layer} providers={providers} onDone={() => close(onDone)} mutation={addMut} ownerType="platform" />
      )}
    </InvokedForm>
  )
}

// ---------------------------------------------------------------------------
// Connect an OAuth account, in a dialog of its own. The engine's descriptor
// decides the flow and the words: a device-code login (a verification URL +
// one-time code the form polls on) or a code-paste login (a popup whose page
// shows a code); the account and vendor labels are the engine's own. The
// caller unmounts it to close it, which stops the poll and the popup timer;
// `aliveRef` drops every answer that lands after that.
// ---------------------------------------------------------------------------

export function ConnectOAuth({ engine, ownerType, onDone }: { engine: EngineDescriptor; ownerType: 'platform' | 'user'; onDone: () => void }) {
  const layer = engine.name
  const deviceCode = oauthFlow(engine) === 'device_code'
  const account = accountLabel(engine)
  const vendor = engine.identity.vendor_label
  // 'retry': a paste was refused and a fresh flow is ready; the popup opens
  // only on the Try again click (a window.open outside a gesture is blocked).
  // 'done': the paste matched an account already connected (or revived an
  // expired one), which the form says instead of closing quietly.
  const [step, setStep] = useState<'idle' | 'waiting' | 'code' | 'device-code' | 'retry' | 'done'>('idle')
  const [code, setCode] = useState('')
  const [oauthState, setOauthState] = useState('')
  const [notice, setNotice] = useState('')

  // Block an install switch while this auth flow holds in-WebView state (the code
  // paste / device-code step would be lost on a switch).
  useEffect(() => {
    setNativeAuthInProgress(step !== 'idle')
    return () => setNativeAuthInProgress(false)
  }, [step])
  const [userCode, setUserCode] = useState('')
  const [authUrl, setAuthUrl] = useState('')
  const [error, setError] = useState('')

  // Device-code poll handle — stopped on unmount (every Cancel button calls
  // onDone, which unmounts this form) so it stops hitting the status endpoint.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const aliveRef = useRef(true)
  // A status call slower than the poll's period must not overlap the next:
  // two "completed" answers would finish twice.
  const pollingRef = useRef(false)
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

  // Claude hooks (code-paste flow)
  const startClaude = useStartClaudeOAuth()
  const exchangeClaude = useExchangeClaudeOAuth()

  // OpenAI hooks (device code flow)
  const startOpenAI = useStartOpenAIOAuth()
  const checkStatus = useOpenAIOAuthStatus()
  const finishOpenAI = useFinishOpenAIOAuth()


  // --- Claude flow (code-paste) ---
  const [authUrlClaude, setAuthUrlClaude] = useState('')

  const openClaudePopup = useCallback(async (url: string) => {
    setStep('waiting')
    const { openOAuthWindow } = await import('../../lib/oauth')
    if (!aliveRef.current) return
    const opened = await openOAuthWindow(url, 'claude-oauth')
    if (!aliveRef.current) return
    if (!opened) {
      setError('The login popup was blocked. Allow popups for this site, then try again.')
      setStep('retry')
      return
    }
    timerRef.current = setTimeout(() => setStep('code'), 2000)
  }, [])

  const handleStartClaude = useCallback(async () => {
    setError('')
    setNotice('')
    try {
      const { url, state } = await startClaude.mutateAsync({ layer, ownerType })
      if (!aliveRef.current) return
      setOauthState(state)
      setAuthUrlClaude(url)
      await openClaudePopup(url)
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message)
    }
  }, [layer, ownerType, startClaude, openClaudePopup])

  const handleExchangeClaude = useCallback(async () => {
    if (!code.trim() || !oauthState) return
    setError('')
    try {
      const result = await exchangeClaude.mutateAsync({ code: code.trim(), state: oauthState, layer })
      if (!aliveRef.current) return
      if (result.created) {
        onDone()
        return
      }
      // A match: the same account again, or an expired row revived through
      // this same button (the Setup form has no Reconnect of its own).
      const email = result.subscription?.oauth_email || ''
      setCode('')
      setNotice(
        result.previous_status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE
          ? alreadyConnectedMessage(email)
          : `Reconnected ${email || 'the account'}: its login is fresh again.`,
      )
      setStep('done')
    } catch (e) {
      if (!aliveRef.current) return
      setError((e as Error).message)
      // The state was consumed by the refused paste: prepare a fresh flow
      // so one click retries, without a popup until that click.
      if (e instanceof ClaudeExchangeError && e.status === 403) return
      try {
        const { url, state } = await startClaude.mutateAsync({ layer, ownerType })
        if (!aliveRef.current) return
        setOauthState(state)
        setAuthUrlClaude(url)
        setCode('')
        setStep('retry')
      } catch (e2) {
        if (aliveRef.current) setError(`${(e as Error).message} ${(e2 as Error).message}`)
      }
    }
  }, [code, oauthState, layer, ownerType, exchangeClaude, startClaude, onDone])

  // --- OpenAI flow (device code) ---
  const handleStartOpenAI = useCallback(async () => {
    setError('')
    try {
      const result = await startOpenAI.mutateAsync({ layer, ownerType })
      if (!aliveRef.current) return
      setAuthUrl(result.url)
      setUserCode(result.user_code)
      setStep('device-code')
      // Poll for completion (replace any prior poll first)
      stopPoll()
      const poll = setInterval(async () => {
        if (pollingRef.current) return
        pollingRef.current = true
        try {
          const status = await checkStatus.mutateAsync({ loginId: result.login_id })
          if (!aliveRef.current) return
          if (status.status === 'completed') {
            stopPoll()
            try {
              await finishOpenAI.mutateAsync({ loginId: result.login_id, layer })
              if (!aliveRef.current) return
            } catch (err) {
              if (!aliveRef.current) return
              if (!loginAlreadyFinished(err)) {
                setError((err as Error).message)
                setStep('idle')
                return
              }
            }
            onDone()
          } else if (status.status === 'failed') {
            stopPoll()
            setError(status.message || 'Login failed')
            setStep('idle')
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
      pollRef.current = poll
    } catch (e) {
      if (aliveRef.current) setError((e as Error).message)
    }
  }, [layer, ownerType, startOpenAI, checkStatus, finishOpenAI, onDone, stopPoll])

  const handleStart = deviceCode ? handleStartOpenAI : handleStartClaude
  const isStarting = deviceCode ? startOpenAI.isPending : startClaude.isPending
  // Nothing closes the dialog while an exchange or a finish runs; a start may
  // hang, so Cancel stays and the liveness guard drops its late answer.
  const busy = exchangeClaude.isPending || finishOpenAI.isPending
  const startRef = useRef<HTMLButtonElement>(null)
  const stepRef = useRef<HTMLDivElement>(null)

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
  }, [step])

  const inDialog = (body: React.ReactNode) => (
    <Dialog
      onClose={onDone}
      title={`Connect a ${account} account`}
      placement="sheet"
      dismissible="escape"
      busy={busy}
      initialFocus={startRef}
    >
      <div ref={stepRef} tabIndex={-1} className="px-4 py-3 space-y-3 outline-none">
        {body}
      </div>
    </Dialog>
  )

  // --- Idle state ---
  if (step === 'idle') {
    return inDialog(
      <>
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            ref={startRef}
            onClick={handleStart}
            disabled={isStarting}
          >
            {isStarting ? 'Starting...' : `Connect ${account} Account`}
          </Button>
          <Button variant="ghost" size="sm" onClick={onDone} disabled={busy}>
            Cancel
          </Button>
        </div>
        {error && <p className="text-xs text-status-danger-fg" role="alert">{error}</p>}
      </>,
    )
  }

  // --- OpenAI device code state ---
  if (step === 'device-code') {
    return inDialog(
      <>
        <p className="text-sm font-medium text-primary">Sign in with {account}</p>
        <div className="space-y-2">
          <p className="text-xs text-secondary">1. Open this link and sign in to your account:</p>
          <div className="flex items-center gap-2">
            <a href={authUrl} target="_blank" rel="noopener noreferrer" className="text-sm text-link hover:underline truncate">{authUrl}</a>
            <Button
              variant="primary"
              size="xs"
              className="shrink-0"
              onClick={() => window.open(authUrl, '_blank', 'noopener,noreferrer')}
            >
              Open
            </Button>
          </div>
        </div>
        <div className="space-y-2">
          <p className="text-xs text-secondary">2. Enter this one-time code:</p>
          <div className="flex items-center gap-2">
            <span className="px-4 py-2 text-lg font-mono font-bold tracking-widest bg-surface-sunken border border-subtle rounded-control text-primary select-all">
              {userCode}
            </span>
            <CopyButton text={userCode} className="shrink-0" />
          </div>
        </div>
        <div role="status" className="flex items-center gap-2 text-xs text-secondary">
          <svg className="animate-spin h-3.5 w-3.5 text-link" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
          </svg>
          Waiting for authentication...
        </div>
        <Button variant="ghost" size="xs" onClick={onDone} disabled={busy}>
          Cancel
        </Button>
        {error && <p className="text-xs text-status-danger-fg" role="alert">{error}</p>}
      </>,
    )
  }

  // --- Claude: the paste matched an account already connected ---
  if (step === 'done') {
    return inDialog(
      <>
        <p className="text-sm text-status-warning-fg" data-testid="claude-connect-notice">{notice}</p>
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            onClick={handleStartClaude}
            disabled={startClaude.isPending}
          >
            Start again
          </Button>
          <Button variant="ghost" size="sm" onClick={onDone} disabled={busy}>
            Done
          </Button>
        </div>
      </>,
    )
  }

  // --- Claude: a paste was refused; a fresh flow waits for one click ---
  if (step === 'retry') {
    return inDialog(
      <>
        <p className="text-xs text-status-danger-fg" role="alert">{error}</p>
        <p className="text-xs text-tertiary">
          Click Try again to open the login popup once more and paste the new code.
        </p>
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            onClick={() => { setError(''); void openClaudePopup(authUrlClaude) }}
          >
            Try again
          </Button>
          <Button variant="ghost" size="sm" onClick={onDone} disabled={busy}>
            Cancel
          </Button>
        </div>
      </>,
    )
  }

  // --- Claude code-paste states ---
  return inDialog(
    <>
      <div>
        <p className="text-sm font-medium text-primary mb-1" role="status">
          {step === 'waiting' ? 'Authenticating...' : 'Enter authorization code'}
        </p>
        <p className="text-xs text-tertiary">
          {step === 'waiting'
            ? `A popup window opened for ${vendor} login. After you authenticate, a code will be shown on the page.`
            : `Copy the authorization code from the ${vendor} page and paste it below.`}
          {' '}{CLAUDE_SECOND_ACCOUNT_HINT}
        </p>
      </div>
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
          onClick={handleExchangeClaude}
          disabled={!code.trim() || exchangeClaude.isPending}
        >
          {exchangeClaude.isPending ? 'Connecting...' : 'Connect'}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDone} disabled={busy}>
          Cancel
        </Button>
      </div>
      {error && <p className="text-xs text-status-danger-fg" role="alert">{error}</p>}
    </>,
  )
}

// ---------------------------------------------------------------------------
// Add Custom Model Form
// ---------------------------------------------------------------------------

export function AddModelForm({ layer, provider, xhighEditable, onDone }: {
  layer: string
  provider: string
  /** The provider declares xhigh per model (`effort_per_model`): its adapter
   *  reads `supports_xhigh` and falls back to max without it. */
  xhighEditable: boolean
  onDone: () => void
}) {
  const [modelId, setModelId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [showPricing, setShowPricing] = useState(false)
  const [contextWindow, setContextWindow] = useState('')
  const [pInput, setPInput] = useState('')
  const [pOutput, setPOutput] = useState('')
  const [pCacheWrite, setPCacheWrite] = useState('')
  const [pCacheRead, setPCacheRead] = useState('')
  const [reasoning, setReasoning] = useState(false)
  const [xhigh, setXhigh] = useState(false)
  const addMut = useAddModel()
  const formRef = useRef<HTMLDivElement>(null)
  const form = useInvokedForm(formRef)

  const handleSubmit = () => {
    if (!modelId.trim() || !displayName.trim()) return
    addMut.mutate(
      {
        layer,
        model_id: modelId.trim(),
        display_name: displayName.trim(),
        provider,
        context_window: contextWindow ? parseInt(contextWindow) : undefined,
        pricing_input: pInput ? parseFloat(pInput) : undefined,
        pricing_output: pOutput ? parseFloat(pOutput) : undefined,
        pricing_cache_write: pCacheWrite ? parseFloat(pCacheWrite) : undefined,
        pricing_cache_read: pCacheRead ? parseFloat(pCacheRead) : undefined,
        supports_reasoning: reasoning,
        supports_xhigh: xhigh,
      },
      { onSuccess: () => { setModelId(''); setDisplayName(''); form.close(onDone) } },
    )
  }

  const inputClass = `${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER}`

  return (
    <div
      ref={formRef}
      onKeyDown={(e) => form.onKeyDown(e, onDone)}
      className="mt-2 p-3 bg-surface-raised shadow-elevation-2 rounded-card space-y-2"
    >
      <p className="text-xs font-medium text-secondary">
        Add custom model for {PROVIDER_LABELS[provider] || provider}
      </p>
      <input
        type="text"
        placeholder="Model ID, e.g. gpt-5.4-2026-03-05"
        value={modelId}
        onChange={(e) => setModelId(e.target.value)}
        className={inputClass + ' font-mono'}
      />
      <input
        type="text"
        placeholder="Display Name, e.g. GPT-5.4"
        value={displayName}
        onChange={(e) => setDisplayName(e.target.value)}
        className={inputClass}
      />
      <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
        <input
          type="checkbox"
          checked={reasoning}
          onChange={(e) => setReasoning(e.target.checked)}
          className="accent-action"
        />
        Supports reasoning, with effort and thinking settings
      </label>
      {/* The checkbox exists where the provider's entry declares xhigh per
          model (Anthropic: an older model rejects it and the adapter sends
          max instead). A provider whose ladder tops at xhigh takes it on
          every model and never reads the flag — no checkbox, the flag stays
          false in the payload and nothing consults it. */}
      {xhighEditable && (
        <div className="text-xs text-secondary">
          <label className="cursor-pointer">
            <input
              type="checkbox"
              checked={xhigh}
              onChange={(e) => setXhigh(e.target.checked)}
              className="accent-action mr-2 align-middle"
            />
            Supports xhigh effort on Opus 4.7 and later
          </label>
          <InfoTip label="Supports xhigh effort on Opus 4.7 and later">Falls back to max on unsupported models.</InfoTip>
        </div>
      )}

      {/* Collapsible pricing section */}
      <button
        type="button"
        onClick={() => setShowPricing(!showPricing)}
        className="text-xs text-secondary hover:text-primary transition-colors flex items-center gap-1"
      >
        <svg
          className={`w-3 h-3 transition-transform ${showPricing ? 'rotate-90' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
        Pricing and context, optional
      </button>
      {showPricing && (
        <div className="space-y-2 pl-4 border-l-2 border-subtle">
          <input
            type="number"
            placeholder="Context window in tokens, e.g. 128000"
            value={contextWindow}
            onChange={(e) => setContextWindow(e.target.value)}
            className={inputClass}
          />
          <div className="grid grid-cols-2 gap-2">
            <input
              type="number" step="0.01"
              placeholder="Input $/1M tokens"
              value={pInput}
              onChange={(e) => setPInput(e.target.value)}
              className={inputClass}
            />
            <input
              type="number" step="0.01"
              placeholder="Output $/1M tokens"
              value={pOutput}
              onChange={(e) => setPOutput(e.target.value)}
              className={inputClass}
            />
            <input
              type="number" step="0.01"
              placeholder="Cache write $/1M"
              value={pCacheWrite}
              onChange={(e) => setPCacheWrite(e.target.value)}
              className={inputClass}
            />
            <input
              type="number" step="0.01"
              placeholder="Cache read $/1M"
              value={pCacheRead}
              onChange={(e) => setPCacheRead(e.target.value)}
              className={inputClass}
            />
          </div>
        </div>
      )}

      <div className="flex gap-2">
        <Button variant="primary" size="sm"
          onClick={handleSubmit}
          disabled={!modelId.trim() || !displayName.trim() || addMut.isPending}
        >
          {addMut.isPending ? 'Adding...' : 'Add Model'}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => form.close(onDone)}>
          Cancel
        </Button>
      </div>
      {addMut.isError && <p className="text-xs text-status-danger-fg">{(addMut.error as Error).message}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Discover Models Panel
// ---------------------------------------------------------------------------

export function DiscoverModelsPanel({
  layer,
  provider,
  discoveredModels,
  existingModelIds,
  onDone,
  layers,
}: {
  layer: string
  provider: string
  discoveredModels: DiscoveredModel[]
  existingModelIds: Set<string>
  onDone: () => void
  /** Engines the models are added to (a shared local endpoint adds to every
   *  engine it is enabled for). Defaults to `layer`. */
  layers?: string[]
}) {
  const [selected, setSelected] = useState<Set<string>>(() => {
    // Pre-select models that aren't already added
    return new Set(
      discoveredModels
        .filter((m) => !existingModelIds.has(m.model_id))
        .map((m) => m.model_id),
    )
  })
  const [filter, setFilter] = useState('')
  const bulkAdd = useBulkAddModels()

  const filtered = filter
    ? discoveredModels.filter(
        (m) =>
          m.model_id.toLowerCase().includes(filter.toLowerCase()) ||
          m.display_name.toLowerCase().includes(filter.toLowerCase()),
      )
    : discoveredModels

  const newModels = filtered.filter((m) => !existingModelIds.has(m.model_id))
  const selectedCount = newModels.filter((m) => selected.has(m.model_id)).length

  const toggleModel = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAll = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      newModels.forEach((m) => next.add(m.model_id))
      return next
    })
  }

  const deselectAll = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      newModels.forEach((m) => next.delete(m.model_id))
      return next
    })
  }

  const handleAdd = () => {
    const toAdd = discoveredModels.filter(
      (m) => selected.has(m.model_id) && !existingModelIds.has(m.model_id),
    )
    if (toAdd.length === 0) return
    bulkAdd.mutate(
      { layer, models: toAdd, provider, ...(layers?.length ? { layers } : {}) },
      { onSuccess: () => onDone() },
    )
  }

  return (
    <div className="mt-3 p-4 bg-surface-raised shadow-elevation-2 rounded-card">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h4 className="text-sm font-semibold text-primary">
            Discovered Models
          </h4>
          <p className="text-xs text-tertiary mt-0.5">
            {discoveredModels.length} model{discoveredModels.length !== 1 ? 's' : ''} found from {provider}
          </p>
        </div>
        <button
          onClick={onDone}
          className="text-secondary hover:text-primary transition-colors p-1"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* Search + select controls */}
      <div className="flex items-center gap-3 mb-2">
        <div className="flex-1 relative">
          <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-tertiary" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <Input
            type="text"
            placeholder="Filter models..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="pl-8"
          />
        </div>
        <div className="flex items-center gap-2 text-xs shrink-0">
          <button onClick={selectAll} className="text-link hover:text-link-hover transition-colors">
            Select all
          </button>
          <span className="text-tertiary">·</span>
          <button onClick={deselectAll} className="text-link hover:text-link-hover transition-colors">
            None
          </button>
          <span className="text-tertiary ml-1">{selectedCount} selected</span>
        </div>
      </div>

      {/* Model list */}
      <div className={`${CARD_BORDERED} max-h-64 overflow-y-auto divide-y divide-subtle`}>
        {filtered.length === 0 && (
          <p className="text-sm text-tertiary py-4 text-center">No models match your filter.</p>
        )}
        {filtered.map((m) => {
          const exists = existingModelIds.has(m.model_id)
          return (
            <label
              key={m.model_id}
              className={`flex items-center gap-3 px-3 py-2 text-sm cursor-pointer transition-colors
                ${exists ? 'opacity-50 cursor-default' : 'hover:bg-surface-hover'}`}
            >
              <input
                type="checkbox"
                checked={exists || selected.has(m.model_id)}
                disabled={exists}
                onChange={() => toggleModel(m.model_id)}
                className="accent-action"
              />
              <span className="font-mono text-primary flex-1 truncate">{m.model_id}</span>
              {m.display_name !== m.model_id && (
                <span className="text-xs text-tertiary truncate max-w-[200px]">{m.display_name}</span>
              )}
              {exists && <Badge>added</Badge>}
            </label>
          )
        })}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 mt-3">
        <Button variant="primary" size="sm"
          onClick={handleAdd}
          disabled={selectedCount === 0 || bulkAdd.isPending}
        >
          {bulkAdd.isPending
            ? 'Adding...'
            : `Add ${selectedCount} Model${selectedCount !== 1 ? 's' : ''}`}
        </Button>
        <Button variant="ghost" size="sm"
          onClick={onDone}
        >
          Cancel
        </Button>
        {bulkAdd.isError && (
          <p className="text-xs text-status-danger-fg">{(bulkAdd.error as Error).message}</p>
        )}
      </div>
    </div>
  )
}
