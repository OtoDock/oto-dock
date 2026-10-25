/**
 * Admin Phone Servers tab — telephony servers, call routes, call prompts,
 * per-language phrases, the (call-only) turn classifier, and infrastructure.
 * STT/TTS providers + chat audio live in the Audio tab.
 */

import { useState, type ReactNode } from 'react'
import {
  usePhoneRoutes, useCreatePhoneRoute, useUpdatePhoneRoute, useDeletePhoneRoute,
  useDeleteRoutePin,
  usePhoneServers, useCreatePhoneServer,
  usePhoneSettings, useSavePhoneSettings, usePhonePromptDefaults, useRouteMcpPreview,
  useExternalData, useSaveExternalData, useForgetExternalData,
  type PhoneRoute, type PhoneRouteCreate, type PhoneServerCreate, type RouteMode,
  PIN_DIGITS, isRoutePin, PHONE_INBOUND, PHONE_OUTBOUND,
} from '../../api/phone'

import CallLogModal from '../../components/phone/CallLogModal'
import {
  useAudioProviders, type AudioProvider,
  useTurnClassifier,
} from '../../api/audio'
import { useAgents, useAgentUsers } from '../../api/agents'
import { useTriggers, useCreateTrigger } from '../../api/triggers'
import { SectionCard, SettingRow, Badge, SaveState } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import Switch from '../../components/ui/Switch'
import InfoTip from '../../components/ui/InfoTip'
import { PhoneServerPill } from '../../components/phone/PhoneServerPill'
import { AdvancedTuningCard } from '../../components/audio/AdvancedTuning'
import { LanguageCandidatesRow } from '../../components/audio/LanguageCandidatesRow'
import { LANGUAGES, baseLang } from '../../audio/lang'
import { formatBytes } from './PlatformPage.format'
import { useDialogs } from '../../components/ui/dialogs'
import Dialog from '../../components/ui/Dialog'
import Button from '../../components/ui/Button'
import { emitAppToast } from '../../lib/appToasts'
import { errorText, toastError } from '../../lib/http/apiJson'
import { Input, Select } from '../../components/ui/Field'
import { BUTTON_BASE, BUTTON_SIZE, CARD_BORDERED, DIALOG_PANEL, FIELD, FIELD_REGULAR, FIELD_BORDER, SEGMENT } from '../../components/ui/classes'

// ---------------------------------------------------------------------------
// Servers
// ---------------------------------------------------------------------------

type AdapterType = NonNullable<PhoneServerCreate['adapter_type']>

// 3CX is deliberately absent: deferred until customers ask (backend keeps a
// graceful stub for any legacy row, but new ones can't be created).
// Ordered easiest-first (operator decision 2026-08-14): Twilio needs zero
// infrastructure, FreePBX auto-provisions against an existing box, manual
// Asterisk is the expert path.
const ADAPTER_OPTIONS: { value: AdapterType; label: string; hint: string }[] = [
  { value: 'twilio', label: 'Twilio', hint: 'Buy a number and paste your account SID and auth token. Calls come in through your public dashboard URL.' },
  { value: 'asterisk_freepbx', label: 'FreePBX, automated', hint: 'FreePBX with GraphQL and AMI. OtoDock sets up each route for you.' },
  { value: 'asterisk_manual', label: 'Asterisk, manual', hint: 'Any Asterisk. You install a dialplan snippet once. Each route then shows the exact AstDB command to run. No API access needed.' },
]

function AddServerForm() {
  const create = useCreatePhoneServer()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [host, setHost] = useState('')
  const [adapter, setAdapter] = useState<AdapterType>('twilio')
  const [accountSid, setAccountSid] = useState('')
  const [authToken, setAuthToken] = useState('')
  const isTwilio = adapter === 'twilio'

  const reset = () => {
    setName(''); setHost(''); setAdapter('twilio')
    setAccountSid(''); setAuthToken(''); setOpen(false)
  }

  const submit = () => {
    if (!name.trim()) return
    // Twilio has no host — its coordinates are the account SID + auth token,
    // and calls enter through the install's public dashboard URL.
    const payload = isTwilio
      ? {
          name: name.trim(), adapter_type: adapter, host: '',
          config: { account_sid: accountSid.trim() },
          ...(authToken.trim() ? { twilio_auth_token: authToken.trim() } : {}),
        }
      : { name: name.trim(), adapter_type: adapter, host: host.trim() }
    create.mutate(payload, {
      onSuccess: reset,
      onError: toastError('The phone server was not added'),
    })
  }

  if (!open) return <button onClick={() => setOpen(true)} className="text-xs text-link hover:underline">+ Add phone server</button>
  const hint = ADAPTER_OPTIONS.find(o => o.value === adapter)?.hint
  return (
    <div className="p-3 rounded-card bg-surface-raised shadow-elevation-2 space-y-2">
      <div>
        <label className="block text-xs font-medium text-primary mb-1">Server type</label>
        <Select value={adapter} onChange={e => setAdapter(e.target.value as AdapterType)}>
          {ADAPTER_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </Select>
        {hint && <p className="text-xs text-tertiary mt-1">{hint}</p>}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <Input value={name} onChange={e => setName(e.target.value)} placeholder={isTwilio ? 'Name, e.g. Twilio' : 'Name, e.g. Main PBX'} />
        {!isTwilio && (
          <Input value={host} onChange={e => setHost(e.target.value)} placeholder="Host, like pbx.example.com" />
        )}
        {isTwilio && (
          <Input value={accountSid} onChange={e => setAccountSid(e.target.value)} placeholder="Account SID, like ACxxxxxxxx…"
            className="font-mono" />
        )}
      </div>
      {isTwilio && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <Input type="password" value={authToken} onChange={e => setAuthToken(e.target.value)} placeholder="Auth token from the Twilio console"
            className="font-mono" />
        </div>
      )}
      <div className="flex gap-2 justify-end">
        <Button variant="ghost" size="xs" onClick={reset}>Cancel</Button>
        <Button
          variant="primary"
          size="xs"
          onClick={submit}
          disabled={!name.trim() || create.isPending}
        >
          {create.isPending ? 'Adding...' : 'Add'}
        </Button>
      </div>
    </div>
  )
}

function ServersSection() {
  const { data: servers, isLoading } = usePhoneServers()
  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>
  return (
    <div className="space-y-3">
      {(servers || []).length === 0
        ? <p className="text-sm text-secondary">No phone servers configured.</p>
        : servers!.map(s => <PhoneServerPill key={s.id} server={s} />)}
      <AddServerForm />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

const EMPTY_ROUTE: PhoneRouteCreate = {
  direction: PHONE_INBOUND, name: '', agent: '', language: 'en', language_detect: true, llm_mode: 'proxy',
  transfer_targets: [],
  phone_server_id: null, stt_provider_id: null, tts_provider_id: null, voice_id: '',
  greeting: '', phone_context_override: '',
  backchannel_mode: 'on', thinking_filler_mode: 'on',
  background_sound: 'off',
  enabled: true, audiosocket_uuid: null, did: '', ami_caller_id: '', ami_outbound_context: '', dial_prefix: '',
  trigger_slug: null,
  identity_mode: 'caller', identity_user_sub: null, remember_callers: true,
}

// Per-route filler toggle: the wire value is 'on'/'off' (RouteMode), the
// control is a plain switch.
function ModeToggle({ label, value, onChange }: { label: string; value: RouteMode; onChange: (v: RouteMode) => void }) {
  return <Switch aria-label={label} checked={value !== 'off'} onChange={v => onChange(v ? 'on' : 'off')} />
}

// The modal's PIN intent: a value being set rides the route save as `pin`
// (the server stores it before any write); a removal goes to the write-only
// DELETE endpoint after the route saves. The value never enters `form`.
export type PinIntent = { dirty: boolean; enabled: boolean; value: string }

// Whether a saved form ends as a user-mode inbound route with no PIN: the
// state the server refuses without an explicit acknowledgement.
function endsWithoutPin(form: { identity_mode: string; direction: string }, pin: PinIntent, hasPin: boolean) {
  return form.identity_mode === 'user' && form.direction === PHONE_INBOUND
    && !(pin.enabled && (pin.value !== '' || hasPin))
}

// Where a PIN belongs (the server's rule): every inbound route, and an
// outbound route tied to a platform user (the person who answers enters it
// before the agent speaks).
function pinAllowed(form: { identity_mode: string; direction: string }) {
  return form.direction === PHONE_INBOUND || form.identity_mode === 'user'
}

// Who the caller IS on a route (docs/PHONE-CONFIG.md "Route identity and
// role"). `caller` never reaches a user's session (on a Shared-only agent it
// is simply the agent's shared space with no per-caller memory) and always
// runs as a viewer of the shared space; `user` runs the tied user's own
// session and is the mode that needs a PIN.
const IDENTITY_OPTIONS: { value: PhoneRoute['identity_mode']; label: string; hint?: string; info: string }[] = [
  { value: 'caller', label: 'Per caller, recommended', info: 'Each phone number gets its own private notes and files. This needs an agent with personal spaces. Nothing a caller says reaches other callers or the team\'s shared memory. Callers read the agent\'s shared files as viewers and change nothing. On a shared-only agent, callers work in the shared space with no per-caller memory.' },
  { value: 'user', label: 'A platform user', hint: 'A call never gets more than manager rights.', info: 'Calls run as that user\'s own session, with their memory, schedules, meetings and delegation.' },
]

const IDENTITY_LABEL: Record<PhoneRoute['identity_mode'], string> = {
  caller: 'per caller', shared: 'shared', user: 'user',
}

// "Available on this line / Not on calls" — the same resolver the live
// session uses, minus processes. Re-fetched as the agent or mode changes.
function McpPreview({ agent, identityMode }: { agent: string; identityMode: PhoneRoute['identity_mode'] }) {
  const { data, isLoading, isError } = useRouteMcpPreview(agent, identityMode)
  if (!agent) return null
  if (isError) return null
  if (isLoading || !data) return <p className="text-xs text-tertiary">Checking the tools on this line…</p>
  const name = (m: { name: string; label: string }) => m.label || m.name
  return (
    <div className="text-xs space-y-0.5" data-testid="route-mcp-preview">
      <p className="text-secondary">
        <span className="font-medium">Available on this line:</span>{' '}
        {data.attached.length ? data.attached.map(name).join(', ') : 'no MCP tools'}
      </p>
      {data.excluded.length > 0 && (
        <p className="text-tertiary">
          <span className="font-medium">Not on calls:</span>{' '}
          {data.excluded.map((m, i) => (
            <span key={m.name} title={m.reason}>{i > 0 ? ', ' : ''}{name(m)}</span>
          ))}
        </p>
      )}
    </div>
  )
}

const FieldLabel = ({ label, large, aside, children }: { label: string; large?: boolean; aside?: ReactNode; children: ReactNode }) => (
  <div className="mb-1">
    <label className={`${large ? 'text-sm' : 'text-xs'} font-medium text-primary`}>{label}</label>
    <InfoTip label={label}>{children}</InfoTip>
    {aside}
  </div>
)

function RouteModal({
  route, agents, languages, servers, providers, onSave, onClose, saving, error,
}: {
  route: PhoneRouteCreate & { id?: string; pin_configured?: boolean }
  agents: string[]
  languages: string[]
  servers: { id: number; name: string; adapter_type?: string }[]
  providers: AudioProvider[]
  onSave: (data: PhoneRouteCreate & { id?: string }, pin: PinIntent, acknowledged: boolean) => void
  onClose: () => void
  saving: boolean
  /** Why the last save failed: the editor stays open with it. */
  error: string
}) {
  const dialogs = useDialogs()
  const [form, setForm] = useState(route)
  const selectedServerAdapter = servers.find(sv => sv.id === form.phone_server_id)?.adapter_type
  const set = (field: string, value: unknown) => setForm(prev => ({ ...prev, [field]: value }))
  // Candidates for a user-tied route: the users attached to the agent (a
  // platform admin qualifies too — the server accepts any admin sub; the
  // currently tied sub stays selectable while editing).
  const { data: agentUsers } = useAgentUsers(form.identity_mode === 'user' ? form.agent : '')
  const userChoices = (agentUsers || []).slice()
  if (form.identity_user_sub && !userChoices.some(u => u.sub === form.identity_user_sub)) {
    userChoices.push({ sub: form.identity_user_sub, name: form.identity_user_sub, email: '', role: '' })
  }
  // PIN state lives OUTSIDE `form`: the value is added to the save request
  // at the last moment, and extra keys would false-positive the dirty-close
  // guard (it compares `form` with the row).
  const hasPin = !!route.pin_configured
  const [pinEnabled, setPinEnabled] = useState(hasPin)
  const [pinValue, setPinValue] = useState('')
  const pinDirty = pinEnabled !== hasPin || pinValue !== ''
  const pinFits = pinAllowed(form)
  const pinValid =
    !pinFits || !pinEnabled
    || isRoutePin(pinValue) || (hasPin && pinValue === '')
  const { data: agentTriggers } = useTriggers({ agent: form.agent, scope: 'agent' })
  const createTrigger = useCreateTrigger()
  const [showCreateTrigger, setShowCreateTrigger] = useState(false)
  const [newTriggerName, setNewTriggerName] = useState('')

  const sttProviders = providers.filter(p => p.provider_type === 'stt' && p.enabled_for_calls)
  const ttsProviders = providers.filter(p => p.provider_type === 'tts' && p.enabled_for_calls)

  const handleCreateTrigger = () => {
    const name = newTriggerName.trim()
    if (!name || !form.agent) return
    createTrigger.mutate(
      {
        name, scope: 'agent', agent: form.agent,
        notify: { enabled: true, severity: 'info', title: 'Phone route trigger: {{phone}}', body: 'Inbound call landed via phone route binding. DID: {{did}}.', target_scope: 'agent' },
        enabled: true,
      },
      { onSuccess: (created) => { set('trigger_slug', created.slug); setNewTriggerName(''); setShowCreateTrigger(false) } },
    )
  }

  const identityValid = form.identity_mode !== 'user' || !!form.identity_user_sub
  const canSave = !saving && !!form.agent && !!form.phone_server_id && pinValid && identityValid

  // Backdrop click closes the modal; unsaved edits get a confirm first.
  const dirty = JSON.stringify(form) !== JSON.stringify(route) || pinDirty
  const requestClose = async () => {
    if (!dirty || await dialogs.confirm({ title: 'Discard the unsaved changes?', confirmLabel: 'Discard', tone: 'danger' })) onClose()
  }

  // A user-mode inbound route saved without a PIN is an explicit choice: the
  // admin confirms it in words and the request carries the acknowledgement
  // (the server refuses the save without it).
  const save = async () => {
    const pin: PinIntent = { dirty: pinDirty, enabled: pinEnabled, value: pinValue }
    const noPin = endsWithoutPin(form, pin, hasPin)
    if (noPin) {
      const picked = userChoices.find(u => u.sub === form.identity_user_sub)
      const who = picked && picked.sub !== picked.name
        ? (picked.name || picked.email || picked.sub)
        : `the tied user (${form.identity_user_sub})`
      if (!(await dialogs.confirm({
        title: 'Save without a PIN?',
        body: `Anyone who calls this route will act as ${who}, with all their tools and credentials.`,
        confirmLabel: 'Save without a PIN',
        tone: 'danger',
      }))) return
    }
    onSave(form, pin, noPin)
  }

  return (
    <Dialog
      onClose={() => { void requestClose() }}
      busy={saving}
      aria-labelledby="route-modal-title"
      panelClassName={`${DIALOG_PANEL} max-w-lg`}
    >
        <div className="p-5 space-y-4">
          <h3 id="route-modal-title" className="text-base font-semibold text-primary">{form.id ? 'Edit Route' : 'New Route'}</h3>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-primary mb-1">Name</label>
              <Input value={form.name} onChange={e => set('name', e.target.value)} placeholder="e.g., Main Line" />
            </div>
            <div>
              <label className="block text-xs font-medium text-primary mb-1">Direction</label>
              <div role="group" aria-label="Direction" className="flex gap-2">
                {([PHONE_INBOUND, PHONE_OUTBOUND] as const).map(d => (
                  <button key={d} type="button" onClick={() => set('direction', d)} aria-pressed={form.direction === d}
                    className={`${BUTTON_BASE} ${BUTTON_SIZE.sm} flex-1 ${form.direction === d ? SEGMENT.on : SEGMENT.off}`}>{d}</button>
                ))}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-primary mb-1">Agent</label>
              <Select value={form.agent} onChange={e => set('agent', e.target.value)}>
                <option value="">Select an agent</option>
                {agents.map(a => <option key={a} value={a}>{a}</option>)}
              </Select>
            </div>
            <div>
              <label className="block text-xs font-medium text-primary mb-1">Language</label>
              <Select value={form.language} onChange={e => set('language', e.target.value)}>
                {languages.map(l => <option key={l} value={l}>{l.toUpperCase()}</option>)}
                {languages.length === 0 && <option value="en">EN</option>}
              </Select>
            </div>
          </div>

          <SettingRow label="Detect the caller's language"
            info="The greeting plays in the language above. The caller's first words then pick the language for the rest of the call. This needs a voice for that language on the line. Off: every call runs in the language above.">
            <Switch aria-label="Detect the caller's language" checked={form.language_detect ?? false} onChange={v => set('language_detect', v)} />
          </SettingRow>

          <div>
            <label className="block text-xs font-medium text-primary mb-1">Phone Server</label>
            <Select value={form.phone_server_id ?? ''} onChange={e => set('phone_server_id', e.target.value ? Number(e.target.value) : null)}>
              <option value="">Select a server</option>
              {servers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
            {servers.length === 0 && <p className="text-xs text-status-warning-fg mt-1">Add a phone server first, under Phone Servers above.</p>}
          </div>

          <div className="border border-subtle rounded-card p-3 space-y-3">
            <div>
              <span className="text-sm font-medium text-primary">Caller identity</span>
              <p className="text-xs text-tertiary mt-0.5">Who the agent is talking to on this line, and what the call may touch.</p>
            </div>
            <div className="space-y-1.5" role="radiogroup" aria-label="Caller identity">
              {IDENTITY_OPTIONS.map(o => (
                <div key={o.value} className="relative flex items-start gap-2 text-sm text-primary">
                  <input type="radio" id={`identity-${o.value}`} name="identity_mode" value={o.value} checked={form.identity_mode === o.value}
                    aria-describedby={o.hint ? `identity-${o.value}-hint` : undefined} onChange={() => set('identity_mode', o.value)} className="mt-0.5 cursor-pointer" />
                  <div className="min-w-0">
                    <label htmlFor={`identity-${o.value}`} className="font-medium cursor-pointer after:absolute after:inset-0">{o.label}</label>
                    <span className="relative z-10"><InfoTip label={o.label}>{o.info}</InfoTip></span>
                    {o.hint && <span id={`identity-${o.value}-hint`} className="block text-xs text-tertiary">{o.hint}</span>}
                  </div>
                </div>
              ))}
            </div>
            {form.identity_mode === 'user' ? (
              <div>
                <label className="block text-xs font-medium text-primary mb-1">Platform user</label>
                <Select value={form.identity_user_sub ?? ''} onChange={e => set('identity_user_sub', e.target.value || null)}
                  aria-label="Platform user">
                  <option value="">Select a user</option>
                  {userChoices.map(u => <option key={u.sub} value={u.sub}>{u.name || u.email || u.sub}{u.role ? ` (${u.role})` : ''}</option>)}
                </Select>
                <p className="text-xs text-status-warning-fg mt-1">
                  {form.direction === PHONE_INBOUND
                    ? 'Require a PIN below. Without one, anyone who dials this number acts as that user.'
                    : 'Whoever answers a call on this route acts as that user. Require a PIN below so they enter it first. Use this route for calls to your own phone.'}
                </p>
              </div>
            ) : (
              <SettingRow label="Remember callers" info="Keep each caller's notes between calls">
                <Switch aria-label="Remember callers" checked={form.remember_callers} onChange={v => set('remember_callers', v)} />
              </SettingRow>
            )}
            <McpPreview agent={form.agent} identityMode={form.identity_mode} />
          </div>

          {/* The PIN: every inbound route, and an outbound route tied to a
              user (the person who answers enters it before the agent
              speaks). A stored PIN on a form that no longer allows one stays
              visible in its removal state, and the save removes it first. */}
          {(pinFits || hasPin) && (
            <div className="border border-subtle rounded-card p-3 space-y-2">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <span className="text-sm font-medium text-primary">Require a PIN</span>
                  <InfoTip label="Require a PIN">
                    {form.direction === PHONE_INBOUND
                      ? 'Every caller must enter this code on the keypad before the agent answers. This includes people you know.'
                      : 'The person who answers must enter this code on the keypad before the agent speaks. A Twilio line hears every keypress. An Asterisk line needs its AMI event listener connected, or it hears only in-band tones.'}
                  </InfoTip>
                  {hasPin && <span className="text-xs text-status-success-fg ml-1.5">Set</span>}
                </div>
                {pinFits && <Switch aria-label="Require a PIN" checked={pinEnabled} onChange={setPinEnabled} />}
              </div>
              {pinFits && pinEnabled && (
                <div>
                  <Input
                    type="password" inputMode="numeric" autoComplete="off" maxLength={PIN_DIGITS}
                    value={pinValue}
                    onChange={e => setPinValue(e.target.value.replace(/\D/g, ''))}
                    className="max-w-40 font-mono tracking-widest"
                    placeholder={hasPin ? '••••••' : `${PIN_DIGITS} digits`}
                  />
                  <p className="text-xs text-tertiary mt-1">
                    {hasPin
                      ? 'Leave empty to keep the current PIN, or type a new one to replace it.'
                      : `Enter ${PIN_DIGITS} digits. The person presses pound (#) after the code.`}
                  </p>
                </div>
              )}
              {hasPin && (!pinFits || !pinEnabled) && (
                <p className="text-xs text-status-warning-fg">The PIN will be removed when you save.</p>
              )}
            </div>
          )}

          {form.direction === PHONE_INBOUND && (
            <>
              <div>
                <FieldLabel label="DID, the inbound number">The number the PBX sends to this route. The phone server links it to the route's AudioSocket UUID.</FieldLabel>
                <Input value={form.did} onChange={e => set('did', e.target.value)}
                  className="font-mono"
                  placeholder="+302101234567" />
              </div>
              <div>
                <label className="block text-xs font-medium text-primary mb-1">Greeting</label>
                <textarea value={form.greeting} onChange={e => set('greeting', e.target.value)} rows={2}
                  className={`${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER} resize-y`}
                  placeholder="TTS greeting text. Leave empty for the default." />
              </div>
            </>
          )}

          {form.direction === PHONE_OUTBOUND && (
            <div>
              <label className="block text-xs font-medium text-primary mb-1">Caller ID</label>
              <Input value={form.ami_caller_id} onChange={e => set('ami_caller_id', e.target.value)} placeholder='"Company" <+1234567890>' />
            </div>
          )}

          <div>
            <FieldLabel label="Transfer targets">The colleagues the agent may transfer the caller to, by name. These are the only numbers it ever dials. Asterisk lines transfer inbound calls only. Twilio lines transfer both ways.</FieldLabel>
            <p className="text-xs text-tertiary mb-1">Use a FreePBX extension or a number your outbound routes understand.</p>
            <div className="space-y-2" data-testid="transfer-targets">
              {/* Wraps on a phone: the number takes its own row below 640 px. */}
              {(form.transfer_targets ?? []).map((t, i) => (
                <div key={i} className="flex flex-wrap gap-2">
                  <Input value={t.label} aria-label={`Transfer target ${i + 1} name`}
                    onChange={e => set('transfer_targets', (form.transfer_targets ?? []).map((x, j) => j === i ? { ...x, label: e.target.value } : x))}
                    className="flex-1 min-w-[10rem]" placeholder="Name, e.g. Reception" />
                  <Input value={t.number} aria-label={`Transfer target ${i + 1} number`}
                    onChange={e => set('transfer_targets', (form.transfer_targets ?? []).map((x, j) => j === i ? { ...x, number: e.target.value } : x))}
                    className="sm:w-40 font-mono" placeholder="200 or +30210…" />
                  <button type="button" aria-label={`Remove transfer target ${i + 1}`}
                    onClick={() => set('transfer_targets', (form.transfer_targets ?? []).filter((_, j) => j !== i))}
                    className="px-2 text-xs text-secondary hover:text-status-danger-fg">Remove</button>
                </div>
              ))}
              <Button
                size="xs"
                type="button"
                onClick={() => set('transfer_targets', [...(form.transfer_targets ?? []), { label: '', number: '' }])}
                disabled={(form.transfer_targets ?? []).length >= 20}
              >
                + Add target
              </Button>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <SettingRow label="Backchannel" info="Active-listening sounds while the caller talks">
              <ModeToggle label="Backchannel" value={form.backchannel_mode} onChange={v => set('backchannel_mode', v)} />
            </SettingRow>
            <SettingRow label="Thinking filler" info="Plays only when the answer is slow to start">
              <ModeToggle label="Thinking filler" value={form.thinking_filler_mode} onChange={v => set('thinking_filler_mode', v)} />
            </SettingRow>
          </div>

          <div>
            <FieldLabel label="Background Ambience">A soft ambient bed played through the whole call so the line never goes dead-quiet.</FieldLabel>
            <Select value={form.background_sound || 'off'} onChange={e => set('background_sound', e.target.value as PhoneRouteCreate['background_sound'])}>
              <option value="off">Off</option>
              <option value="call_center">Call center</option>
              <option value="office">Office</option>
              <option value="city">City street</option>
              <option value="nature">Nature in a park</option>
            </Select>
          </div>

          <div>
            <FieldLabel label="Route-Specific Context">Additional instructions appended to the base call context for this route only.</FieldLabel>
            <textarea value={form.phone_context_override} onChange={e => set('phone_context_override', e.target.value)} rows={2}
              className={`${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER} resize-y`}
              placeholder="e.g., Always speak formally on this line" />
          </div>

          <div>
            <FieldLabel label="Bound Trigger">When set, inbound calls bring <code>{'${trigger.*}'}</code> values, such as the caller's phone and the DID. The agent's <code>agent_context</code> blocks read them. The trigger only gives the link a name. It does not run its task on the call.</FieldLabel>
            <div className="flex gap-2">
              <Select value={form.trigger_slug || ''} onChange={e => set('trigger_slug', e.target.value || null)}
                className="flex-1">
                <option value="">None</option>
                {(agentTriggers || []).map(t => <option key={t.slug} value={t.slug}>{t.name} ({t.slug})</option>)}
              </Select>
              <Button
                size="xs"
                className="shrink-0"
                type="button"
                onClick={() => setShowCreateTrigger(v => !v)}
                disabled={!form.agent}
                title="Create a new context-profile trigger for this agent"
              >+ New</Button>
            </div>
            {showCreateTrigger && (
              <div className="mt-2 p-3 border border-subtle rounded-card space-y-2">
                <p className="text-xs text-tertiary">Creates a trigger for this agent, with no webhook action. It only names the link between this route and the <code>{'${trigger.*}'}</code> values.</p>
                <div className="flex gap-2">
                  <Input autoFocus value={newTriggerName} onChange={e => setNewTriggerName(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') handleCreateTrigger() }} placeholder="e.g., Support call"
                    className="flex-1" />
                  <Button
                    variant="primary"
                    size="xs"
                    type="button"
                    onClick={handleCreateTrigger}
                    disabled={!newTriggerName.trim() || createTrigger.isPending}
                  >
                    {createTrigger.isPending ? 'Creating...' : 'Create'}
                  </Button>
                </div>
                {createTrigger.error && <p className="text-xs text-status-danger-fg">{(createTrigger.error as Error).message}</p>}
              </div>
            )}
          </div>

          {/* Blank by default: the provider overrides, a voice for this
              route alone, and the Asterisk plumbing (a Twilio server
              matches by DID and dials by REST, so its lines never show
              the three Asterisk fields). */}
          <details className="group" data-testid="route-advanced">
            <summary className="text-xs text-tertiary cursor-pointer select-none hover:text-primary">
              Advanced
            </summary>
            <div className="mt-2 space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-primary mb-1">STT Provider</label>
                  <Select value={form.stt_provider_id ?? ''} onChange={e => set('stt_provider_id', e.target.value ? Number(e.target.value) : null)}>
                    <option value="">Use the default</option>
                    {sttProviders.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
                  </Select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-primary mb-1">TTS Provider</label>
                  <Select value={form.tts_provider_id ?? ''} onChange={e => set('tts_provider_id', e.target.value ? Number(e.target.value) : null)}>
                    <option value="">Use the default</option>
                    {ttsProviders.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
                  </Select>
                </div>
              </div>
              <div>
                <FieldLabel label="Voice">A voice id from the TTS provider's library, for this route only.</FieldLabel>
                <Input value={form.voice_id ?? ''} aria-label="Voice id"
                  onChange={e => set('voice_id', e.target.value.replace(/\s+/g, ''))}
                  className="font-mono"
                  placeholder="The provider's voice for the language" />
                <p className="mt-1 text-xs text-tertiary">Needs the phone server of 1.8.0 or later.</p>
              </div>
              {form.direction === PHONE_OUTBOUND && selectedServerAdapter !== 'twilio' && (
                <>
                  <div>
                    <FieldLabel label="Outbound Dial Prefix">Digits dialled before the number to pick a trunk, with a matching FreePBX outbound route.</FieldLabel>
                    <Input value={form.dial_prefix} onChange={e => set('dial_prefix', e.target.value)} placeholder="(e.g. 81)" />
                    <p className="mt-1 text-xs text-tertiary">Blank: your default routing.</p>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-primary mb-1">Dialplan Context</label>
                    <Input value={form.ami_outbound_context} onChange={e => set('ami_outbound_context', e.target.value)} placeholder="oto-audiosocket-outbound" />
                    <p className="mt-1 text-xs text-tertiary">Blank unless you changed the installed dialplan.</p>
                  </div>
                </>
              )}
              {form.direction === PHONE_INBOUND && selectedServerAdapter !== 'twilio' && (
                <div>
                  <label className="block text-xs font-medium text-primary mb-1">AudioSocket UUID</label>
                  <Input value={form.audiosocket_uuid || ''} onChange={e => set('audiosocket_uuid', e.target.value || null)}
                    className="font-mono"
                    placeholder="Filled in when you save" />
                  <p className="mt-1 text-xs text-tertiary">Filled in when you save. Set it only to reuse the UUID your dialplan already uses.</p>
                </div>
              )}
            </div>
          </details>

          <div className="flex items-center gap-2">
            <Switch aria-labelledby="route-enabled-label" checked={form.enabled} onChange={v => set('enabled', v)} />
            <span id="route-enabled-label" className="text-sm text-primary">Enabled</span>
          </div>

          {error && <p role="alert" className="text-xs text-status-danger-fg">{error}</p>}
          <div className="flex justify-end gap-2 pt-2 border-t border-subtle">
            <Button onClick={onClose} disabled={saving} className="px-4">Cancel</Button>
            <Button variant="primary" onClick={() => { void save() }} disabled={!canSave} className="px-4">
              {saving ? 'Saving...' : 'Save'}
            </Button>
          </div>
        </div>
    </Dialog>
  )
}

function RoutesSection() {
  const dialogs = useDialogs()
  const { data: routes, isLoading } = usePhoneRoutes()
  const { data: agents } = useAgents({ all: true })
  const { data: servers } = usePhoneServers()
  const { data: providers } = useAudioProviders()
  const { data: settings } = usePhoneSettings()
  const createMut = useCreatePhoneRoute()
  const updateMut = useUpdatePhoneRoute()
  const deleteMut = useDeletePhoneRoute()
  const toggleMut = useUpdatePhoneRoute()
  const delPinMut = useDeleteRoutePin()
  const { stateOf, run } = useSaveState()
  const [editRoute, setEditRouteState] = useState<(PhoneRouteCreate & { id?: string; pin_configured?: boolean }) | null>(null)
  const [routeError, setRouteError] = useState('')
  // A route opened (or closed) starts without the last one's refusal.
  const setEditRoute = (r: (PhoneRouteCreate & { id?: string; pin_configured?: boolean }) | null) => {
    setRouteError('')
    setEditRouteState(r)
  }
  const [logRoute, setLogRoute] = useState<PhoneRoute | null>(null)

  const agentSlugs = (agents || []).map(a => a.name)
  const serverList = (servers || []).map(s => ({ id: s.id, name: s.name, adapter_type: s.adapter_type }))
  const defaultServerId = (servers || []).find(s => s.is_default)?.id ?? servers?.[0]?.id ?? null
  let configuredLanguages: string[] = []
  try { configuredLanguages = Object.keys(JSON.parse(settings?.phrases || '{}')) } catch { /* empty */ }

  const openNew = () => setEditRoute({ ...EMPTY_ROUTE, agent: agentSlugs[0] || '', phone_server_id: defaultServerId })

  // A PIN being set rides the route save itself (the server stores it before
  // any write); a removal goes to the write-only DELETE AFTER the route
  // saves, and its failure is loud: the route saved fine, but the admin must
  // know the gate is still armed. Server-computed identity advisories (a
  // user-tied line without a PIN, a tied user that lost access, …) ride the
  // save / removal responses, shown once, loudly; the routes table keeps the
  // marker. The "no PIN" note is dropped when the admin just confirmed that
  // exact risk: repeating it teaches people to dismiss alerts.
  const showWarnings = (
    res: { warnings?: string[] } | undefined, prefix: string, acknowledged: boolean,
  ): Promise<void> => {
    const w = (res?.warnings ?? []).filter(x => !acknowledged || !x.includes('no PIN'))
    if (!w.length) return Promise.resolve()
    return dialogs.alert({ title: prefix, body: w.map(x => `• ${x}`).join('\n') })
  }

  const removePin = (routeId: string, acknowledged: boolean) => {
    delPinMut.mutate({ id: routeId, acknowledgeNoPin: acknowledged }, {
      onSuccess: (res) => { void showWarnings(res, 'PIN removed. Please note:', acknowledged) },
      onError: toastError('The route was saved, but its PIN was not removed'),
    })
  }

  const handleSave = (
    data: PhoneRouteCreate & { id?: string; pin_configured?: boolean }, pin: PinIntent, acknowledged: boolean,
  ) => {
    // pin_configured + warnings are server-computed — never send them back.
    const { id, pin_configured, ...rest } = data
    delete (rest as { warnings?: string[] }).warnings
    const hadPin = !!pin_configured
    // A value rides the save only where a PIN belongs (every inbound route,
    // an outbound route tied to a user), and only a valid one.
    const fits = pinAllowed(rest)
    const settingPin = fits && pin.dirty && pin.enabled && isRoutePin(pin.value)
    if (settingPin) rest.pin = pin.value
    if (acknowledged) rest.acknowledge_no_pin = true
    // A removal the admin asked for goes after the save; a stored PIN the
    // edited route may no longer carry goes BEFORE it (the card said so),
    // since the server refuses the save while the PIN would be stranded.
    const removingPin = hadPin && pin.dirty && !pin.enabled
    const strandedPin = hadPin && !fits
    setRouteError('')
    const refused = (e: unknown) => setRouteError(errorText(e) || 'The route was not saved.')
    if (id) {
      const save = () => updateMut.mutate({ id, data: rest }, {
        onSuccess: (res) => {
          if (removingPin && !strandedPin) removePin(id, acknowledged)
          setEditRoute(null)
          void showWarnings(res, 'Route saved. Please note:', acknowledged)
        },
        onError: refused,
      })
      if (strandedPin) {
        // The card said the PIN goes with this save; the stored row may still
        // be an inbound user route, whose PIN removal the server lets through
        // only acknowledged.
        delPinMut.mutate({ id, acknowledgeNoPin: true }, {
          onSuccess: save,
          onError: (e) => { void showWarnings(
            { warnings: [(e as Error).message] }, 'The PIN could not be removed, so the route was not saved:', true) },
        })
      } else {
        save()
      }
    } else {
      createMut.mutate(rest as PhoneRouteCreate, {
        onSuccess: async (created) => {
          setEditRoute(null)
          // The instructions first, then the notes: each waits for the one before.
          if (created?.provisioning_instructions) {
            await dialogs.alert({ title: 'Set up the route on the phone system', body: created.provisioning_instructions })
          }
          await showWarnings(created, 'Route created. Please note:', acknowledged)
        },
        onError: refused,
      })
    }
  }

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  return (
    <>
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs text-tertiary">Set up your inbound and outbound call routes.</p>
        <Button variant="primary" size="xs" onClick={openNew}>Add Route</Button>
      </div>

      {(!routes || routes.length === 0) ? (
        <p className="text-sm text-secondary text-center py-6">No routes configured</p>
      ) : (
        // overflow-x-auto (not hidden): the 8-column table is wider than a
        // phone screen — let the admin scroll to the Actions column
        <div className={`${CARD_BORDERED} overflow-x-auto`}>
          <table className="w-full min-w-[760px] text-sm">
            <thead className="bg-surface-muted">
              <tr>
                <th className="text-left px-3 py-2 font-medium text-secondary">Name</th>
                <th className="text-left px-3 py-2 font-medium text-secondary">Direction</th>
                <th className="text-left px-3 py-2 font-medium text-secondary">DID</th>
                <th className="text-left px-3 py-2 font-medium text-secondary">Agent</th>
                <th className="text-left px-3 py-2 font-medium text-secondary">Identity</th>
                <th className="text-left px-3 py-2 font-medium text-secondary">Language</th>
                <th className="text-left px-3 py-2 font-medium text-secondary">Trigger</th>
                <th className="text-center px-3 py-2 font-medium text-secondary">Enabled</th>
                <th className="text-right px-3 py-2 font-medium text-secondary">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle">
              {routes.map(r => (
                <tr key={r.id} className="hover:bg-surface-hover">
                  <td className="px-3 py-2 text-primary">{r.name || r.id.slice(0, 8)}</td>
                  <td className="px-3 py-2">
                    <Badge variant={r.direction === PHONE_INBOUND ? 'info' : 'neutral'}>{r.direction}</Badge>
                    {r.pin_configured && (
                      <svg className="inline-block w-3 h-3 ml-1.5 text-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-label="PIN protected">
                        <title>PIN protected</title>
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                      </svg>
                    )}
                  </td>
                  <td className="px-3 py-2 text-primary font-mono text-xs">{r.did || <span className="text-tertiary">—</span>}</td>
                  <td className="px-3 py-2 text-primary font-mono text-xs">{r.agent}</td>
                  <td className="px-3 py-2 text-primary text-xs whitespace-nowrap">
                    {IDENTITY_LABEL[r.identity_mode] ?? r.identity_mode}
                    {(r.warnings?.length ?? 0) > 0 && (
                      <svg className="inline-block w-3.5 h-3.5 ml-1.5 text-status-warning-fg align-text-bottom" fill="none" viewBox="0 0 24 24" stroke="currentColor"
                        aria-label="Route warnings" role="img">
                        <title>{r.warnings.join('\n')}</title>
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                      </svg>
                    )}
                  </td>
                  <td className="px-3 py-2 text-primary">{r.language}</td>
                  <td className="px-3 py-2 text-primary font-mono text-xs">{r.trigger_slug || <span className="text-tertiary">—</span>}</td>
                  <td className="px-3 py-2 text-center">
                    <Switch aria-label={`${r.name || r.id.slice(0, 8)} enabled`} checked={r.enabled} onChange={v => { void run(r.id, () => toggleMut.mutateAsync({ id: r.id, data: { enabled: v } }), { failTitle: v ? 'The route was not turned on' : 'The route was not turned off' }) }} />
                    <SaveState state={stateOf(r.id)} />
                  </td>
                  <td className="px-3 py-2 text-right space-x-2 whitespace-nowrap">
                    <button onClick={() => setLogRoute(r)} title="Call log"
                      className="text-xs text-secondary hover:text-link transition-colors align-middle">
                      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                      </svg>
                    </button>
                    <button onClick={() => setEditRoute({ ...r })} className="text-xs text-link hover:underline">Edit</button>
                    <button onClick={async () => {
                      if (!(await dialogs.confirm({ title: 'Delete this route?', confirmLabel: 'Delete', tone: 'danger' }))) return
                      deleteMut.mutate(r.id, {
                        onSuccess: (res) => { if (res?.warning) void dialogs.alert({ title: 'The route is deleted. Please note:', body: res.warning }) },
                        onError: toastError('The route was not deleted'),
                      })
                    }} className="text-xs text-status-danger-fg hover:underline">Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editRoute && (
        <RouteModal
          route={editRoute} agents={agentSlugs} languages={configuredLanguages}
          servers={serverList} providers={providers || []}
          onSave={handleSave} onClose={() => setEditRoute(null)}
          saving={createMut.isPending || updateMut.isPending}
          error={routeError}
        />
      )}
      {logRoute && <CallLogModal route={logRoute} onClose={() => setLogRoute(null)} />}
    </>
  )
}

// ---------------------------------------------------------------------------
// Caller data — what external callers leave behind, and for how long
// ---------------------------------------------------------------------------

function CallerDataSection() {
  const dialogs = useDialogs()
  const { data, isLoading } = useExternalData()
  const save = useSaveExternalData()
  const forget = useForgetExternalData()
  const { stateOf, run } = useSaveState()
  const [days, setDays] = useState<string | null>(null)
  const shownDays = days ?? (data ? String(data.days) : '')

  // A refused number stays in the field as typed.
  const saveDays = () => {
    const n = Number(shownDays)
    if (!Number.isInteger(n) || n < 1 || n > 3650 || !data || n === data.days) { setDays(null); return }
    void run('days', () => save.mutateAsync({ days: n }).then(() => setDays(null)), { failTitle: 'The retention was not saved' })
  }

  const forgetAll = async () => {
    if (!(await dialogs.confirm({
      title: 'Forget all caller data now?',
      body: 'This deletes every caller\'s private notes and files and every phone conversation. It also deletes the whole call log. '
        + 'Calls in progress are skipped. Nothing goes to the Recover bin: this cannot be undone.',
      confirmLabel: 'Forget everything',
      tone: 'danger',
    }))) return
    forget.mutate(undefined, {
      onSuccess: (r) => emitAppToast({
        severity: 'success',
        title: 'Caller data forgotten',
        body: `${r.callers_forgotten} callers, ${r.phone_chats_deleted} conversations, `
          + `${r.call_log_rows_deleted} call-log rows, ${formatBytes(r.caller_bytes_freed)} freed`
          + (r.callers_busy_skipped || r.phone_chats_busy_skipped
            ? `. Skipped ${r.callers_busy_skipped} callers and ${r.phone_chats_busy_skipped} conversations still on a call: run again later.`
            : '.'),
      }),
      onError: toastError('The caller data was not forgotten'),
    })
  }

  if (isLoading || !data) return <p className="text-sm text-secondary">Loading...</p>
  return (
    <div className="space-y-4">
      <p className="text-xs text-tertiary">
        Callers who are not platform users leave three things behind. These are their notes and files,
        their phone conversations and the call log. Notes and files exist only on per-caller routes.
        All three age out together.
      </p>
      <SettingRow label="Forget callers automatically" save={stateOf('enabled')}
        info="Callers not heard from within the window below are forgotten on the daily cleanup. Off keeps everything until you forget it by hand.">
        <Switch aria-label="Forget callers automatically" checked={data.enabled} onChange={v => { void run('enabled', () => save.mutateAsync({ enabled: v }), { failTitle: 'The setting was not saved' }) }} />
      </SettingRow>
      <SettingRow label="Days to keep caller data" description="Minimum 1." info="Counted from the caller's last call." save={stateOf('days')}>
        <Input type="number" min={1} max={3650} value={shownDays} disabled={!data.enabled}
          aria-label="Days to keep caller data"
          onChange={e => setDays(e.target.value)} onBlur={saveDays}
          onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
          className="max-w-20 text-right" />
      </SettingRow>
      <div>
        <p className="text-sm font-medium text-primary mb-1">On this platform now</p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-1 text-xs" data-testid="caller-data-usage">
          <div className="flex justify-between gap-2"><span className="text-tertiary">Callers remembered</span><span className="text-primary tabular-nums">{data.callers}</span></div>
          <div className="flex justify-between gap-2"><span className="text-tertiary">Caller files</span><span className="text-primary tabular-nums">{formatBytes(data.bytes)}</span></div>
          <div className="flex justify-between gap-2"><span className="text-tertiary">Phone conversations</span><span className="text-primary tabular-nums">{data.phone_chats}</span></div>
          <div className="flex justify-between gap-2"><span className="text-tertiary">Call-log rows</span><span className="text-primary tabular-nums">{data.call_log_rows}</span></div>
        </div>
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <button onClick={forgetAll} disabled={forget.isPending}
          className="px-3 py-1.5 text-xs font-medium rounded-control border border-status-danger-border text-status-danger-fg hover:bg-status-danger-bg disabled:opacity-40">
          {forget.isPending ? 'Forgetting…' : 'Forget all caller data now'}
        </button>
        <span className="text-xs text-tertiary">Deletes every caller's files, the phone conversations and the call log. Nothing lands in the Recover bin.</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Call prompts (inbound / outbound context — phone_*)
// ---------------------------------------------------------------------------

function CallPromptsSection() {
  const { data: settings } = usePhoneSettings()
  // The shipped texts come from the server (the seed's source), so Restore
  // Default writes what a fresh install gets.
  const { data: defaults } = usePhonePromptDefaults()
  const saveMut = useSavePhoneSettings()
  const { stateOf, run } = useSaveState()

  const save = (key: string, value: string) => {
    void run(key, () => saveMut.mutateAsync({ [key]: value }), { failTitle: 'The call prompt was not saved' })
  }

  const prompt = (key: 'context_inbound' | 'context_outbound', label: string, rows: number) => (
    <div>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <label className="text-sm font-medium text-primary">{label}</label>
          <SaveState state={stateOf(key)} />
        </div>
        <button
          onClick={() => { if (defaults) save(key, defaults[key]) }}
          disabled={!defaults}
          className="text-xs text-secondary hover:text-primary disabled:opacity-50"
        >Restore Default</button>
      </div>
      <textarea
        defaultValue={settings?.[key] || ''}
        onBlur={e => { if (e.target.value !== (settings?.[key] || '')) save(key, e.target.value) }}
        rows={rows} key={`${key}-${settings?.[key] ?? ''}`}
        className={`${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER} font-mono resize-y`}
      />
    </div>
  )

  return (
    <div className="space-y-4">
      <p className="text-xs text-tertiary">Instructions added to the agent's prompt on every call. Edit them, or restore the defaults.</p>
      {prompt('context_inbound', 'Inbound Context', 10)}
      {prompt('context_outbound', 'Outbound Context', 10)}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Turn classifier (Groq) — call-only
// ---------------------------------------------------------------------------

function TurnClassifierSection() {
  const { data: tc, isLoading } = useTurnClassifier()

  if (isLoading || !tc) return <p className="text-sm text-secondary">Loading...</p>

  return (
    <div className="space-y-3">
      <p className="text-xs text-tertiary">
        A text classifier that decides from the words when a caller has finished speaking. Choose it for each language
        in the Languages section. Smart Turn is the alternative: it runs locally and listens to the tone of the voice.
      </p>
      <div className={`${CARD_BORDERED} flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 py-2.5`}>
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-primary">Groq</span>
          <Badge variant={tc.active ? 'success' : 'neutral'}>{tc.active ? 'Active' : 'Inactive'}</Badge>
        </div>
        <span className="text-xs text-tertiary font-mono">openai/gpt-oss-120b</span>
      </div>
      <p className="text-xs text-tertiary">
        {tc.active
          ? 'Using the Groq API key from the Direct LLM AI engine.'
          : 'No Groq key in the Direct LLM AI engine. Languages set to Groq use Smart Turn instead. Add a Groq key in Setup, AI Engines, Direct LLM to turn it on.'}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Languages (per-language phrases + filler sounds — call-only / phone_*)
// ---------------------------------------------------------------------------

const DEFAULT_LANGUAGES = {
  // NO turn_classifier pins here — matching the server seed since 2026-08-25:
  // the platform default backend (Groq, Smart Turn fallback when no Groq key)
  // covers every language, and a restore must not write a stale pin that
  // would silently override it.
  phrases: {
    en: { hold_message: 'One moment please, let me check.', greeting_fallback: 'Hello, how can I help you?' },
    el: { hold_message: 'Μια στιγμή παρακαλώ, θα το ελέγξω.', greeting_fallback: 'Γεια σας, πώς μπορώ να σας βοηθήσω;' },
    de: { hold_message: 'Einen Moment bitte, ich schaue nach.', greeting_fallback: 'Hallo, wie kann ich Ihnen helfen?' },
    es: { hold_message: 'Un momento por favor, déjeme comprobar.', greeting_fallback: 'Hola, ¿en qué puedo ayudarle?' },
    fr: { hold_message: 'Un instant s\'il vous plaît, je vérifie.', greeting_fallback: 'Bonjour, comment puis-je vous aider ?' },
    it: { hold_message: 'Un momento per favore, controllo subito.', greeting_fallback: 'Salve, come posso aiutarla?' },
  } as Record<string, Record<string, string>>,
  backchannel: {
    en: ['mhm', 'ok', 'right', 'mm-hmm', 'uh-huh'], el: ['ναι', 'mmm', 'mhm'],
    de: ['mhm', 'okay', 'ja', 'genau', 'aha'], es: ['ajá', 'sí', 'vale', 'claro', 'mmm'],
    fr: ['mhm', 'oui', 'd\'accord', 'hmm', 'voilà'], it: ['mhm', 'sì', 'certo', 'okay', 'aha'],
  } as Record<string, string[]>,
  thinking: {
    en: ['hmm', 'uhh', 'let me see', 'one moment', 'let me check'],
    el: ['εε', 'χμμ', 'για να δω', 'μισό λεπτό', 'μάλιστα'],
    de: ['hmm', 'äh', 'mal sehen', 'einen Moment'],
    es: ['mmm', 'eh', 'déjeme ver', 'un momento', 'a ver'],
    fr: ['hmm', 'euh', 'voyons voir', 'un instant', 'alors'],
    it: ['mmm', 'ehm', 'vediamo', 'un attimo', 'allora'],
  } as Record<string, string[]>,
}

// Phone calls use base language codes; the canonical LANGUAGES list is BCP-47,
// so dedupe by base code (en-US + en-GB → en) for the per-language picker.
const PHONE_LANGS: { code: string; label: string }[] = (() => {
  const seen = new Set<string>()
  const out: { code: string; label: string }[] = []
  for (const { code, label } of LANGUAGES) {
    const base = baseLang(code)
    if (seen.has(base)) continue
    seen.add(base)
    out.push({ code: base, label: label.replace(/,.*$/, '') })
  }
  return out
})()

function LanguagesSection() {
  const { data: settings } = usePhoneSettings()
  const saveMut = useSavePhoneSettings()
  const { stateOf, run } = useSaveState()
  const [lang, setLang] = useState(PHONE_LANGS[0].code)
  // Bumped on Restore to remount the (uncontrolled) inputs so they reflect the
  // restored defaults; normal edits don't change it (so tabbing between fields
  // keeps focus).
  const [restoreVersion, setRestoreVersion] = useState(0)

  const phrases: Record<string, Record<string, string>> = (() => {
    try { return settings?.phrases ? JSON.parse(settings.phrases) : {} } catch { return {} }
  })()
  const backchannel: Record<string, string[]> = (() => {
    try { return settings?.backchannel_phrases ? JSON.parse(settings.backchannel_phrases) : {} } catch { return {} }
  })()
  const thinking: Record<string, string[]> = (() => {
    try { return settings?.thinking_phrases ? JSON.parse(settings.thinking_phrases) : {} } catch { return {} }
  })()

  // `field` keys the save state: the phrases of one language share a setting.
  const save = (field: string, key: string, value: string) => {
    void run(field, () => saveMut.mutateAsync({ [key]: value }), { failTitle: 'The language settings were not saved' })
  }

  const updatePhrase = (key: string, value: string) => {
    save(key, 'phrases', JSON.stringify({ ...phrases, [lang]: { ...(phrases[lang] || {}), [key]: value } }))
  }
  const updateBackchannel = (value: string) => {
    save('backchannel', 'backchannel_phrases', JSON.stringify({ ...backchannel, [lang]: value.split(',').map(s => s.trim()).filter(Boolean) }))
  }
  const updateThinking = (value: string) => {
    save('thinking', 'thinking_phrases', JSON.stringify({ ...thinking, [lang]: value.split(',').map(s => s.trim()).filter(Boolean) }))
  }
  const restoreLang = () => {
    const dp = DEFAULT_LANGUAGES.phrases[lang]
    const db = DEFAULT_LANGUAGES.backchannel[lang]
    const dt = DEFAULT_LANGUAGES.thinking[lang]
    if (!dp && !db && !dt) return
    const payload: Record<string, string> = {}
    if (dp) payload.phrases = JSON.stringify({ ...phrases, [lang]: dp })
    if (db) payload.backchannel_phrases = JSON.stringify({ ...backchannel, [lang]: db })
    if (dt) payload.thinking_phrases = JSON.stringify({ ...thinking, [lang]: dt })
    void run('_all', () => saveMut.mutateAsync(payload).then(() => setRestoreVersion(v => v + 1)),
      { failTitle: 'The language defaults were not restored' })
  }

  const p: Record<string, string> = phrases[lang] || {}
  return (
    <div className="space-y-3">
      <p className="text-xs text-tertiary">
        Per-language phrases and filler sounds for calls. Pick a language to edit its prompts. Voice IDs are configured per provider in the Audio tab's TTS Providers section.
      </p>
      <SettingRow label="Language" info="Edit the prompts for one language at a time">
        <Select value={lang} onChange={e => setLang(e.target.value)}>
          {PHONE_LANGS.map(l => <option key={l.code} value={l.code}>{l.label} ({l.code})</option>)}
        </Select>
      </SettingRow>
      {/* Stacked full-width fields: comma-separated phrase lists want width,
          and fixed-width inputs crush the labels on phones. */}
      <div key={`${lang}:${restoreVersion}`} className={`${CARD_BORDERED} p-3 space-y-3`}>
        <div>
          <p className="text-sm font-medium text-primary mb-1"><label>Hold Message</label><SaveState state={stateOf('hold_message')} /></p>
          <Input defaultValue={p.hold_message || ''}
            onBlur={e => { if (e.target.value !== (p.hold_message || '')) updatePhrase('hold_message', e.target.value) }} placeholder="One moment please..." />
        </div>
        <div>
          <p className="text-sm font-medium text-primary mb-1"><label>Greeting Fallback</label><SaveState state={stateOf('greeting_fallback')} /></p>
          <Input defaultValue={p.greeting_fallback || ''}
            onBlur={e => { if (e.target.value !== (p.greeting_fallback || '')) updatePhrase('greeting_fallback', e.target.value) }} placeholder="Hello, how can I help you?" />
        </div>
        <SettingRow label="Turn Classifier" save={stateOf('turn_classifier')}
          info="Which service decides when the caller has finished speaking. Platform default: Groq when a Groq key is set, otherwise Smart Turn.">
          {/* Empty value = no per-language pin → the platform default backend
              decides (the phone daemon ignores a falsy pin). Only an explicit
              choice here overrides it for this language. */}
          <Select value={p.turn_classifier || ''}
            onChange={e => updatePhrase('turn_classifier', e.target.value)}>
            <option value="">Platform default: Groq, else Smart Turn</option>
            <option value="smart_turn">Smart Turn: audio, local</option>
            <option value="groq">Groq: text, cloud</option>
          </Select>
        </SettingRow>
        <div>
          <p className="text-sm font-medium text-primary mb-1"><label>Backchannel Sounds</label><SaveState state={stateOf('backchannel')} /></p>
          <p className="text-xs text-tertiary mb-1">Active listening sounds, separated by commas</p>
          <Input defaultValue={(backchannel[lang] || []).join(', ')}
            onBlur={e => updateBackchannel(e.target.value)} placeholder="Mhm, ok, right" />
        </div>
        <div>
          <FieldLabel label="Thinking Fillers" large aside={<SaveState state={stateOf('thinking')} />}>Played only when the answer is slow to start.</FieldLabel>
          <p className="text-xs text-tertiary mb-1">Separate them with commas.</p>
          <Input defaultValue={(thinking[lang] || []).join(', ')}
            onBlur={e => updateThinking(e.target.value)} placeholder="Hmm, let me see, one moment" />
        </div>
      </div>
      <div className="pt-2 border-t border-subtle">
        <Button size="xs" onClick={restoreLang} disabled={saveMut.isPending}>
          Restore {lang.toUpperCase()} to default
        </Button>
        <SaveState state={stateOf('_all')} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Infrastructure (transport ports + call lifecycle — phone_*)
// ---------------------------------------------------------------------------

// Split: transport (AudioSocket / HTTP API ports) vs call-lifecycle
// timeouts. ``audiosocket_host`` stays hidden (auto-derived per deployment).
const TRANSPORT_FIELDS = [
  { key: 'audiosocket_port', label: 'AudioSocket Port', info: 'TCP port for Asterisk connections', def: '9092' },
  { key: 'http_api_port', label: 'HTTP API Port', info: 'Port for outbound call management', def: '9093' },
]
const LIFECYCLE_FIELDS = [
  { key: 'idle_timeout_s', label: 'Idle Timeout (s)', info: 'Disconnect after silence', def: '30' },
  { key: 'call_max_duration_s', label: 'Max Call Duration (s)', info: 'Hard limit per call', def: '600' },
  { key: 'outbound_call_timeout_s', label: 'Outbound Call Timeout (s)', info: 'Max wait for call completion', def: '300' },
  { key: 'question_answer_timeout_s', label: 'Q&A Timeout (s)', info: 'Max wait for manager answer', def: '40' },
]

function InfrastructureSection() {
  const { data: settings } = usePhoneSettings()
  const saveMut = useSavePhoneSettings()
  const { stateOf, run } = useSaveState()

  const save = (key: string, value: string) => {
    if (value !== (settings?.[key] ?? '')) {
      void run(key, () => saveMut.mutateAsync({ [key]: value }), { failTitle: 'The setting was not saved' })
    }
  }

  const numRow = (f: { key: string; label: string; info: string; def: string }) => (
    <SettingRow key={f.key} label={f.label} info={f.info} save={stateOf(f.key)}>
      <Input type="number" defaultValue={settings?.[f.key] ?? f.def} key={`${f.key}-${settings?.[f.key] ?? ''}`}
        onBlur={e => save(f.key, e.target.value)}
        className="max-w-28 text-right" />
    </SettingRow>
  )

  return (
    <div className="space-y-5">
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">Transport</h4>
        <div className="space-y-2">{TRANSPORT_FIELDS.map(numRow)}</div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">Call Lifecycle</h4>
        <div className="space-y-2">{LIFECYCLE_FIELDS.map(numRow)}</div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">Logging</h4>
        <SettingRow label="Log Level" save={stateOf('log_level')}>
          <Select value={settings?.log_level || 'INFO'} onChange={e => save('log_level', e.target.value)}>
            {['DEBUG', 'INFO', 'WARNING', 'ERROR'].map(l => <option key={l} value={l}>{l}</option>)}
          </Select>
        </SettingRow>
        <SettingRow label="Save outgoing audio" description="Needs the phone server of 1.8.0 or later." save={stateOf('debug_outgoing_audio')}
          info="Saves the agent's audio of each call as a WAV file beside the phone log. This runs until you turn it off. It helps you check for playback glitches. The caller's audio is never saved.">
          <Switch aria-label="Save outgoing audio" checked={settings?.debug_outgoing_audio === 'on'}
            onChange={v => save('debug_outgoing_audio', v ? 'on' : 'off')} />
        </SettingRow>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

export default function PhoneServersTab() {
  return (
    <div className="space-y-4">
      <SectionCard title="Phone Servers"><ServersSection /></SectionCard>
      <SectionCard title="Routes"><RoutesSection /></SectionCard>
      <SectionCard title="Caller Data" defaultOpen={false}><CallerDataSection /></SectionCard>
      <SectionCard title="Call Prompts" defaultOpen={false}><CallPromptsSection /></SectionCard>
      <SectionCard title="Languages" defaultOpen={false}>
        <LanguageCandidatesRow />
        <LanguagesSection />
      </SectionCard>
      <SectionCard title="Turn Classifier"><TurnClassifierSection /></SectionCard>
      <SectionCard title="Infrastructure" defaultOpen={false}><InfrastructureSection /></SectionCard>
      <AdvancedTuningCard />
    </div>
  )
}
