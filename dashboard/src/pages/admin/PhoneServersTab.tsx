/**
 * Admin Phone Servers tab — telephony servers, call routes, call prompts,
 * per-language phrases, the (call-only) turn classifier, and infrastructure.
 * STT/TTS providers + chat audio live in the Audio tab.
 */

import { useState, useCallback } from 'react'
import {
  usePhoneRoutes, useCreatePhoneRoute, useUpdatePhoneRoute, useDeletePhoneRoute,
  usePhoneServers, useCreatePhoneServer,
  usePhoneSettings, useSavePhoneSettings,
  type PhoneRoute, type PhoneRouteCreate, type PhoneServerCreate, type RouteMode,
} from '../../api/phone'
import {
  useAudioProviders, type AudioProvider,
  useTurnClassifier,
} from '../../api/audio'
import { useAgents } from '../../api/agents'
import { useTriggers, useCreateTrigger } from '../../api/triggers'
import { SectionCard, SettingRow, Toggle, Badge, SavedBadge } from '../../components/ui/SettingsControls'
import { PhoneServerPill } from '../../components/phone/PhoneServerPill'
import { AdvancedTuningCard } from '../../components/audio/AdvancedTuning'
import { LANGUAGES, baseLang } from '../../audio/lang'

// ---------------------------------------------------------------------------
// Servers
// ---------------------------------------------------------------------------

type AdapterType = NonNullable<PhoneServerCreate['adapter_type']>

const ADAPTER_OPTIONS: { value: AdapterType; label: string; hint: string }[] = [
  { value: 'asterisk_manual', label: 'Asterisk (manual)', hint: 'Any Asterisk. You install a one-time dialplan snippet; each route gives you the exact AstDB command. No API automation needed.' },
  { value: 'asterisk_freepbx', label: 'FreePBX (automated)', hint: 'FreePBX with GraphQL + AMI — routes auto-provision end to end. (Coming soon.)' },
  { value: 'twilio', label: 'Twilio', hint: 'Cloud telephony via REST + Media Streams. (Coming soon.)' },
  { value: 'three_cx', label: '3CX', hint: '3CX PBX integration. (Coming soon.)' },
]

function AddServerForm() {
  const create = useCreatePhoneServer()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [host, setHost] = useState('')
  const [adapter, setAdapter] = useState<AdapterType>('asterisk_manual')

  const submit = () => {
    if (!name.trim()) return
    create.mutate({ name: name.trim(), adapter_type: adapter, host: host.trim() }, {
      onSuccess: () => { setName(''); setHost(''); setAdapter('asterisk_manual'); setOpen(false) },
      onError: (e) => alert((e as Error).message),
    })
  }

  if (!open) return <button onClick={() => setOpen(true)} className="text-xs text-brand hover:underline">+ Add phone server</button>
  const hint = ADAPTER_OPTIONS.find(o => o.value === adapter)?.hint
  return (
    <div className="p-3 border border-p-border-light rounded-lg bg-gray-50 dark:bg-gray-800/40 space-y-2">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <input value={name} onChange={e => setName(e.target.value)} placeholder="Name (e.g. Main PBX)"
          className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" />
        <input value={host} onChange={e => setHost(e.target.value)} placeholder="Host (pbx.example.com)"
          className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" />
      </div>
      <div>
        <label className="block text-xs font-medium text-p-text mb-1">Adapter</label>
        <select value={adapter} onChange={e => setAdapter(e.target.value as AdapterType)}
          className="w-full px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
          {ADAPTER_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {hint && <p className="text-xs text-p-text-light mt-1">{hint}</p>}
      </div>
      <div className="flex gap-2 justify-end">
        <button onClick={() => setOpen(false)} className="px-3 py-1 text-xs rounded-lg border border-p-border-light text-p-text-secondary">Cancel</button>
        <button onClick={submit} disabled={!name.trim() || create.isPending}
          className="px-3 py-1 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40">
          {create.isPending ? 'Adding...' : 'Add'}
        </button>
      </div>
    </div>
  )
}

function ServersSection() {
  const { data: servers, isLoading } = usePhoneServers()
  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>
  return (
    <div className="space-y-3">
      {(servers || []).length === 0
        ? <p className="text-sm text-p-text-secondary">No phone servers configured.</p>
        : servers!.map(s => <PhoneServerPill key={s.id} server={s} />)}
      <AddServerForm />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

const EMPTY_ROUTE: PhoneRouteCreate = {
  direction: 'inbound', name: '', agent: '', language: 'en', llm_mode: 'proxy',
  phone_server_id: null, stt_provider_id: null, tts_provider_id: null,
  greeting: '', phone_context_override: '',
  backchannel_mode: 'on', thinking_filler_mode: 'on',
  background_sound: 'off',
  enabled: true, audiosocket_uuid: null, did: '', ami_caller_id: '', ami_outbound_context: '', dial_prefix: '',
  trigger_slug: null,
}

// Per-route filler toggle: the wire value is 'on'/'off' (RouteMode), the
// control is a plain switch.
function ModeToggle({ value, onChange }: { value: RouteMode; onChange: (v: RouteMode) => void }) {
  return <Toggle checked={value !== 'off'} onChange={v => onChange(v ? 'on' : 'off')} />
}

function RouteModal({
  route, agents, languages, servers, providers, onSave, onClose, saving,
}: {
  route: PhoneRouteCreate & { id?: string }
  agents: string[]
  languages: string[]
  servers: { id: number; name: string }[]
  providers: AudioProvider[]
  onSave: (data: PhoneRouteCreate & { id?: string }) => void
  onClose: () => void
  saving: boolean
}) {
  const [form, setForm] = useState(route)
  const set = (field: string, value: unknown) => setForm(prev => ({ ...prev, [field]: value }))
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

  const canSave = !saving && !!form.agent && !!form.phone_server_id

  // Backdrop click closes the modal; unsaved edits get a confirm first.
  const dirty = JSON.stringify(form) !== JSON.stringify(route)
  const requestClose = () => {
    if (!dirty || window.confirm('Discard unsaved changes?')) onClose()
  }

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4"
      onClick={requestClose}>
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light shadow-xl w-full max-w-lg max-h-[85vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}>
        <div className="p-5 space-y-4">
          <h3 className="text-base font-semibold text-p-text">{form.id ? 'Edit Route' : 'New Route'}</h3>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-p-text mb-1">Name</label>
              <input value={form.name} onChange={e => set('name', e.target.value)}
                className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" placeholder="e.g., Main Line" />
            </div>
            <div>
              <label className="block text-xs font-medium text-p-text mb-1">Direction</label>
              <div className="flex gap-2">
                {(['inbound', 'outbound'] as const).map(d => (
                  <button key={d} onClick={() => set('direction', d)}
                    className={`flex-1 px-3 py-1.5 text-sm rounded-lg border transition-colors ${
                      form.direction === d ? 'border-brand bg-brand/10 text-brand font-medium' : 'border-p-border-light text-p-text-secondary hover:border-gray-300'
                    }`}>{d}</button>
                ))}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-p-text mb-1">Agent</label>
              <select value={form.agent} onChange={e => set('agent', e.target.value)}
                className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
                <option value="">— Select agent —</option>
                {agents.map(a => <option key={a} value={a}>{a}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-p-text mb-1">Language</label>
              <select value={form.language} onChange={e => set('language', e.target.value)}
                className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
                {languages.map(l => <option key={l} value={l}>{l.toUpperCase()}</option>)}
                {languages.length === 0 && <option value="en">EN</option>}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-p-text mb-1">Phone Server</label>
            <select value={form.phone_server_id ?? ''} onChange={e => set('phone_server_id', e.target.value ? Number(e.target.value) : null)}
              className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
              <option value="">— Select server —</option>
              {servers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            {servers.length === 0 && <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">Add a phone server first (Servers section).</p>}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-p-text mb-1">STT Provider</label>
              <select value={form.stt_provider_id ?? ''} onChange={e => set('stt_provider_id', e.target.value ? Number(e.target.value) : null)}
                className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
                <option value="">(use default)</option>
                {sttProviders.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-p-text mb-1">TTS Provider</label>
              <select value={form.tts_provider_id ?? ''} onChange={e => set('tts_provider_id', e.target.value ? Number(e.target.value) : null)}
                className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
                <option value="">(use default)</option>
                {ttsProviders.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
            </div>
          </div>

          {form.direction === 'inbound' && (
            <>
              <div>
                <label className="block text-xs font-medium text-p-text mb-1">DID (inbound number)</label>
                <input value={form.did} onChange={e => set('did', e.target.value)}
                  className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text font-mono"
                  placeholder="+302101234567" />
                <p className="text-xs text-p-text-light mt-1">The number the PBX maps to this route. Provisioned on the phone server (DID ↔ AudioSocket UUID).</p>
              </div>
              <div>
                <label className="block text-xs font-medium text-p-text mb-1">Greeting</label>
                <textarea value={form.greeting} onChange={e => set('greeting', e.target.value)} rows={2}
                  className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text resize-y"
                  placeholder="TTS greeting text (leave empty for default)" />
              </div>
              <div>
                <label className="block text-xs font-medium text-p-text mb-1">AudioSocket UUID</label>
                <input value={form.audiosocket_uuid || ''} onChange={e => set('audiosocket_uuid', e.target.value || null)}
                  className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text font-mono"
                  placeholder="(auto-allocated on save)" />
                <p className="text-xs text-p-text-light mt-1">Auto-allocated when you save — only set this to reuse an existing UUID.</p>
              </div>
            </>
          )}

          {form.direction === 'outbound' && (
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-p-text mb-1">Caller ID</label>
                <input value={form.ami_caller_id} onChange={e => set('ami_caller_id', e.target.value)}
                  className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" placeholder='"Company" <+1234567890>' />
              </div>
              <div>
                <label className="block text-xs font-medium text-p-text mb-1">Outbound Dial Prefix</label>
                <input value={form.dial_prefix} onChange={e => set('dial_prefix', e.target.value)}
                  className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" placeholder="(optional, e.g. 81)" />
                <p className="mt-1 text-xs text-p-text-light">Prepended to the dialed number to pick which SIP line/trunk this route uses. Create a matching FreePBX Outbound Route (pattern <span className="font-mono">{'<prefix>|.'}</span>) with that trunk + its Caller ID. Blank = your default outbound routing.</p>
              </div>
              <div>
                <label className="block text-xs font-medium text-p-text mb-1">Dialplan Context</label>
                <input value={form.ami_outbound_context} onChange={e => set('ami_outbound_context', e.target.value)}
                  className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" placeholder="oto-audiosocket-outbound" />
                <p className="mt-1 text-xs text-p-text-light">Blank = <span className="font-mono">oto-audiosocket-outbound</span> (installed by the bootstrap snippet).</p>
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <SettingRow label="Backchannel" description="Active-listening sounds while the caller talks">
              <ModeToggle value={form.backchannel_mode} onChange={v => set('backchannel_mode', v)} />
            </SettingRow>
            <SettingRow label="Thinking filler" description="Plays only when the answer is slow to start">
              <ModeToggle value={form.thinking_filler_mode} onChange={v => set('thinking_filler_mode', v)} />
            </SettingRow>
          </div>

          <div>
            <label className="block text-xs font-medium text-p-text mb-1">Background Ambience</label>
            <p className="text-xs text-p-text-light mb-1">A soft ambient bed played through the whole call so the line never goes dead-quiet.</p>
            <select value={form.background_sound || 'off'} onChange={e => set('background_sound', e.target.value as PhoneRouteCreate['background_sound'])}
              className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
              <option value="off">Off</option>
              <option value="call_center">Call center</option>
              <option value="office">Office</option>
              <option value="city">City street</option>
              <option value="nature">Nature / park</option>
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-p-text mb-1">Route-Specific Context</label>
            <p className="text-xs text-p-text-light mb-1">Additional instructions appended to the base call context for this route only.</p>
            <textarea value={form.phone_context_override} onChange={e => set('phone_context_override', e.target.value)} rows={2}
              className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text resize-y"
              placeholder="e.g., Always speak formally on this line" />
          </div>

          <div>
            <label className="block text-xs font-medium text-p-text mb-1">Bound Trigger</label>
            <p className="text-xs text-p-text-light mb-1">
              When set, inbound calls land with a <code>{'${trigger.*}'}</code> payload (caller phone, DID, etc.) that manifest <code>agent_context</code> blocks read. The trigger row acts only as a name — it does NOT fire its task on the call.
            </p>
            <div className="flex gap-2">
              <select value={form.trigger_slug || ''} onChange={e => set('trigger_slug', e.target.value || null)}
                className="flex-1 px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
                <option value="">— None —</option>
                {(agentTriggers || []).map(t => <option key={t.slug} value={t.slug}>{t.name} ({t.slug})</option>)}
              </select>
              <button type="button" onClick={() => setShowCreateTrigger(v => !v)} disabled={!form.agent}
                className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-40 shrink-0"
                title="Create a new context-profile trigger for this agent">+ New</button>
            </div>
            {showCreateTrigger && (
              <div className="mt-2 p-3 border border-p-border-light rounded-lg bg-gray-50 dark:bg-gray-800/40 space-y-2">
                <p className="text-xs text-p-text-light">Creates an agent-scope trigger (disabled, no webhook action). Used only as a label that binds this route to the <code>${'${'}trigger.*{'}'}</code> payload.</p>
                <div className="flex gap-2">
                  <input autoFocus value={newTriggerName} onChange={e => setNewTriggerName(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') handleCreateTrigger() }} placeholder="e.g., Support call"
                    className="flex-1 px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" />
                  <button type="button" onClick={handleCreateTrigger} disabled={!newTriggerName.trim() || createTrigger.isPending}
                    className="px-3 py-1.5 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40">
                    {createTrigger.isPending ? 'Creating...' : 'Create'}
                  </button>
                </div>
                {createTrigger.error && <p className="text-xs text-red-500">{(createTrigger.error as Error).message}</p>}
              </div>
            )}
          </div>

          <div className="flex items-center gap-2">
            <Toggle checked={form.enabled} onChange={v => set('enabled', v)} />
            <span className="text-sm text-p-text">Enabled</span>
          </div>

          <div className="flex justify-end gap-2 pt-2 border-t border-p-border-light">
            <button onClick={onClose} className="px-4 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800">Cancel</button>
            <button onClick={() => onSave(form)} disabled={!canSave}
              className="px-4 py-1.5 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40">
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function RoutesSection() {
  const { data: routes, isLoading } = usePhoneRoutes()
  const { data: agents } = useAgents({ all: true })
  const { data: servers } = usePhoneServers()
  const { data: providers } = useAudioProviders()
  const { data: settings } = usePhoneSettings()
  const createMut = useCreatePhoneRoute()
  const updateMut = useUpdatePhoneRoute()
  const deleteMut = useDeletePhoneRoute()
  const toggleMut = useUpdatePhoneRoute()
  const [editRoute, setEditRoute] = useState<(PhoneRouteCreate & { id?: string }) | null>(null)

  const agentSlugs = (agents || []).map(a => a.name)
  const serverList = (servers || []).map(s => ({ id: s.id, name: s.name }))
  const defaultServerId = (servers || []).find(s => s.is_default)?.id ?? servers?.[0]?.id ?? null
  let configuredLanguages: string[] = []
  try { configuredLanguages = Object.keys(JSON.parse(settings?.phrases || '{}')) } catch { /* empty */ }

  const openNew = () => setEditRoute({ ...EMPTY_ROUTE, agent: agentSlugs[0] || '', phone_server_id: defaultServerId })

  const handleSave = (data: PhoneRouteCreate & { id?: string }) => {
    if (data.id) {
      const { id, ...rest } = data
      updateMut.mutate({ id, data: rest }, { onSuccess: () => setEditRoute(null), onError: (e) => alert((e as Error).message) })
    } else {
      createMut.mutate(data as PhoneRouteCreate, {
        onSuccess: (created) => {
          setEditRoute(null)
          if (created?.provisioning_instructions) alert(created.provisioning_instructions)
        },
        onError: (e) => alert((e as Error).message),
      })
    }
  }

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <>
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs text-p-text-light">Map AudioSocket UUIDs to agent configurations for inbound/outbound calls.</p>
        <button onClick={openNew} className="px-3 py-1.5 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover">Add Route</button>
      </div>

      {(!routes || routes.length === 0) ? (
        <p className="text-sm text-p-text-secondary text-center py-6">No routes configured</p>
      ) : (
        // overflow-x-auto (not hidden): the 8-column table is wider than a
        // phone screen — let the admin scroll to the Actions column
        <div className="border border-p-border-light rounded-lg overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="bg-gray-50 dark:bg-gray-800/50">
              <tr>
                <th className="text-left px-3 py-2 font-medium text-p-text-secondary">Name</th>
                <th className="text-left px-3 py-2 font-medium text-p-text-secondary">Direction</th>
                <th className="text-left px-3 py-2 font-medium text-p-text-secondary">DID</th>
                <th className="text-left px-3 py-2 font-medium text-p-text-secondary">Agent</th>
                <th className="text-left px-3 py-2 font-medium text-p-text-secondary">Language</th>
                <th className="text-left px-3 py-2 font-medium text-p-text-secondary">Trigger</th>
                <th className="text-center px-3 py-2 font-medium text-p-text-secondary">Enabled</th>
                <th className="text-right px-3 py-2 font-medium text-p-text-secondary">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-p-border-light">
              {routes.map(r => (
                <tr key={r.id} className="hover:bg-gray-50/50 dark:hover:bg-gray-800/30">
                  <td className="px-3 py-2 text-p-text">{r.name || r.id.slice(0, 8)}</td>
                  <td className="px-3 py-2"><Badge variant={r.direction === 'inbound' ? 'blue' : 'amber'}>{r.direction}</Badge></td>
                  <td className="px-3 py-2 text-p-text font-mono text-xs">{r.did || <span className="text-p-text-light">—</span>}</td>
                  <td className="px-3 py-2 text-p-text font-mono text-xs">{r.agent}</td>
                  <td className="px-3 py-2 text-p-text">{r.language}</td>
                  <td className="px-3 py-2 text-p-text font-mono text-xs">{r.trigger_slug || <span className="text-p-text-light">—</span>}</td>
                  <td className="px-3 py-2 text-center">
                    <Toggle checked={r.enabled} onChange={v => toggleMut.mutate({ id: r.id, data: { enabled: v } })} />
                  </td>
                  <td className="px-3 py-2 text-right space-x-2">
                    <button onClick={() => setEditRoute({ ...r })} className="text-xs text-brand hover:underline">Edit</button>
                    <button onClick={() => { if (confirm('Delete this route?')) deleteMut.mutate(r.id, { onSuccess: (res) => { if (res?.warning) alert(res.warning) }, onError: (e) => alert((e as Error).message) }) }} className="text-xs text-red-500 hover:underline">Delete</button>
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
        />
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Call prompts (inbound / outbound context — phone_*)
// ---------------------------------------------------------------------------

const DEFAULT_INBOUND_CONTEXT =
  'You are on a live phone call. This is voice — not chat.\n' +
  'RULES:\n' +
  '- ALWAYS respond in the SAME LANGUAGE the user speaks. Greek → Greek. English → English.\n' +
  '- Keep responses SHORT: 1-3 sentences maximum. Summarize instead of listing details.\n' +
  '- Talk naturally like a real person on the phone — use casual, conversational language.\n' +
  '- Avoid stiff/formal phrasing. Say things the way you\'d say them out loud.\n' +
  '- NEVER read tables, lists, JSON, code, or formatted output aloud. Describe results naturally in plain speech.\n' +
  '- If there are many items (services, devices, etc.), give a high-level summary, NOT individual details.\n' +
  '- Don\'t spell out URLs, paths, or IPs unless asked.\n' +
  '- When using tools: say \'One moment\' before, then summarize the result in 1-2 short sentences.\n' +
  '- To end the call (e.g. user says goodbye or the conversation is clearly over), append [CALL_COMPLETE] at the end of your final message. The system will strip it before speaking.\n'

const DEFAULT_OUTBOUND_CONTEXT =
  'You are on a live phone call that YOU placed to complete a task. This is voice — not chat.\n' +
  'RULES:\n' +
  '- ALWAYS respond in the SAME LANGUAGE the other person speaks. Greek → Greek. English → English.\n' +
  '- Keep responses SHORT: 1-3 sentences maximum. Summarize instead of listing details.\n' +
  '- Talk naturally like a real person on the phone — use casual, conversational language.\n' +
  '- Avoid stiff/formal phrasing. Say things the way you\'d say them out loud.\n' +
  '- NEVER read tables, lists, JSON, code, or formatted output aloud. Describe results naturally in plain speech.\n' +
  '- If there are many items (services, devices, etc.), give a high-level summary, NOT individual details.\n' +
  '- Don\'t spell out URLs, paths, or IPs unless asked.\n' +
  '- When using tools: say \'One moment\' before, then summarize the result in 1-2 short sentences.\n' +
  '- Complete the task you were given, politely and professionally.\n' +
  '- If you need information from your manager during the call, emit [QUESTION: your question here] in your response. The system will relay the question while the call stays active.\n' +
  '- When the task is complete or clearly cannot be completed, end your final message with [CALL_COMPLETE].\n' +
  '- The [CALL_COMPLETE] and [QUESTION:] markers are stripped before speaking — they\'re signals for the system.\n'

function CallPromptsSection() {
  const { data: settings } = usePhoneSettings()
  const saveMut = useSavePhoneSettings()
  const [savedField, setSavedField] = useState('')

  const save = (key: string, value: string) => {
    saveMut.mutate({ [key]: value }, { onSuccess: () => { setSavedField(key); setTimeout(() => setSavedField(''), 2000) } })
  }

  const prompt = (key: 'context_inbound' | 'context_outbound', label: string, fallback: string, rows: number) => (
    <div>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <label className="text-sm font-medium text-p-text">{label}</label>
          <SavedBadge show={savedField === key} />
        </div>
        <button onClick={() => save(key, fallback)} className="text-xs text-p-text-secondary hover:text-p-text">Restore Default</button>
      </div>
      <textarea
        defaultValue={settings?.[key] || ''}
        onBlur={e => { if (e.target.value !== (settings?.[key] || '')) save(key, e.target.value) }}
        rows={rows} key={`${key}-${settings?.[key] ?? ''}`}
        className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text font-mono resize-y"
      />
    </div>
  )

  return (
    <div className="space-y-4">
      <p className="text-xs text-p-text-light">System prompt instructions injected into all call sessions. Edit to customize, or restore defaults.</p>
      {prompt('context_inbound', 'Inbound Context', DEFAULT_INBOUND_CONTEXT, 10)}
      {prompt('context_outbound', 'Outbound Context', DEFAULT_OUTBOUND_CONTEXT, 10)}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Turn classifier (Groq) — call-only
// ---------------------------------------------------------------------------

function TurnClassifierSection() {
  const { data: tc, isLoading } = useTurnClassifier()

  if (isLoading || !tc) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <div className="space-y-3">
      <p className="text-xs text-p-text-light">
        Text-based turn-completion classifier used on calls (per-language opt-in via the Languages section). Smart Turn (local prosody) is the audio-based alternative.
      </p>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border border-p-border-light rounded-lg px-3 py-2.5">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-p-text">Groq</span>
          <Badge variant={tc.active ? 'green' : 'default'}>{tc.active ? 'Active' : 'Inactive'}</Badge>
        </div>
        <span className="text-xs text-p-text-light font-mono">openai/gpt-oss-120b</span>
      </div>
      <p className="text-xs text-p-text-light">
        {tc.active
          ? 'Using the Groq API key from the Direct LLM execution layer.'
          : 'No Groq key in the Direct LLM execution layer — languages set to Groq fall back to Smart Turn. Add a Groq key under Execution Layers → Direct LLM to activate.'}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Languages (per-language phrases + filler sounds — call-only / phone_*)
// ---------------------------------------------------------------------------

const DEFAULT_LANGUAGES = {
  phrases: {
    en: { hold_message: 'One moment please, let me check.', greeting_fallback: 'Hello, how can I help you?', turn_classifier: 'smart_turn' },
    el: { hold_message: 'Μια στιγμή παρακαλώ, θα το ελέγξω.', greeting_fallback: 'Γεια σας, πώς μπορώ να σας βοηθήσω;', turn_classifier: 'groq' },
    de: { hold_message: 'Einen Moment bitte, ich schaue nach.', greeting_fallback: 'Hallo, wie kann ich Ihnen helfen?', turn_classifier: 'smart_turn' },
    es: { hold_message: 'Un momento por favor, déjeme comprobar.', greeting_fallback: 'Hola, ¿en qué puedo ayudarle?', turn_classifier: 'smart_turn' },
    fr: { hold_message: 'Un instant s\'il vous plaît, je vérifie.', greeting_fallback: 'Bonjour, comment puis-je vous aider ?', turn_classifier: 'smart_turn' },
    it: { hold_message: 'Un momento per favore, controllo subito.', greeting_fallback: 'Salve, come posso aiutarla?', turn_classifier: 'smart_turn' },
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
    out.push({ code: base, label: label.replace(/\s*\(.*\)\s*$/, '') })
  }
  return out
})()

function LanguagesSection() {
  const { data: settings } = usePhoneSettings()
  const saveMut = useSavePhoneSettings()
  const [savedField, setSavedField] = useState('')
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

  const save = useCallback((key: string, value: string) => {
    saveMut.mutate({ [key]: value }, {
      onSuccess: () => { setSavedField(key); setTimeout(() => setSavedField(''), 2000) },
    })
  }, [saveMut])

  const updatePhrase = (key: string, value: string) => {
    save('phrases', JSON.stringify({ ...phrases, [lang]: { ...(phrases[lang] || {}), [key]: value } }))
  }
  const updateBackchannel = (value: string) => {
    save('backchannel_phrases', JSON.stringify({ ...backchannel, [lang]: value.split(',').map(s => s.trim()).filter(Boolean) }))
  }
  const updateThinking = (value: string) => {
    save('thinking_phrases', JSON.stringify({ ...thinking, [lang]: value.split(',').map(s => s.trim()).filter(Boolean) }))
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
    saveMut.mutate(payload, {
      onSuccess: () => { setRestoreVersion(v => v + 1); setSavedField('_all'); setTimeout(() => setSavedField(''), 2000) },
    })
  }

  const p: Record<string, string> = phrases[lang] || {}
  return (
    <div className="space-y-3">
      <p className="text-xs text-p-text-light">
        Per-language phrases and filler sounds for calls. Pick a language to edit its prompts. Voice IDs are configured per provider in the Audio tab's TTS Providers section.
      </p>
      <SettingRow label="Language" description="Edit the prompts for one language at a time">
        <select value={lang} onChange={e => setLang(e.target.value)}
          className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
          {PHONE_LANGS.map(l => <option key={l.code} value={l.code}>{l.label} — {l.code}</option>)}
        </select>
        <SavedBadge show={savedField === '_all'} />
      </SettingRow>
      {/* Stacked full-width fields: comma-separated phrase lists want width,
          and fixed-width inputs crush the labels on phones. */}
      <div key={`${lang}:${restoreVersion}`} className="border border-p-border-light rounded-lg p-3 space-y-3">
        <div>
          <label className="block text-sm font-medium text-p-text mb-1">Hold Message<SavedBadge show={savedField === 'phrases'} /></label>
          <input defaultValue={p.hold_message || ''}
            onBlur={e => { if (e.target.value !== (p.hold_message || '')) updatePhrase('hold_message', e.target.value) }}
            className="w-full px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" placeholder="One moment please..." />
        </div>
        <div>
          <label className="block text-sm font-medium text-p-text mb-1">Greeting Fallback</label>
          <input defaultValue={p.greeting_fallback || ''}
            onBlur={e => { if (e.target.value !== (p.greeting_fallback || '')) updatePhrase('greeting_fallback', e.target.value) }}
            className="w-full px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" placeholder="Hello, how can I help you?" />
        </div>
        <SettingRow label="Turn Classifier" description="Which backend classifies turn completeness">
          <select value={p.turn_classifier || 'smart_turn'}
            onChange={e => updatePhrase('turn_classifier', e.target.value)}
            className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
            <option value="smart_turn">Smart Turn (audio, local)</option>
            <option value="groq">Groq (text, cloud)</option>
          </select>
        </SettingRow>
        <div>
          <label className="block text-sm font-medium text-p-text mb-1">Backchannel Sounds<SavedBadge show={savedField === 'backchannel_phrases'} /></label>
          <p className="text-xs text-p-text-light mb-1">Active listening sounds (comma-separated)</p>
          <input defaultValue={(backchannel[lang] || []).join(', ')}
            onBlur={e => updateBackchannel(e.target.value)}
            className="w-full px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" placeholder="mhm, ok, right" />
        </div>
        <div>
          <label className="block text-sm font-medium text-p-text mb-1">Thinking Fillers<SavedBadge show={savedField === 'thinking_phrases'} /></label>
          <p className="text-xs text-p-text-light mb-1">Played only when the answer is slow to start (comma-separated)</p>
          <input defaultValue={(thinking[lang] || []).join(', ')}
            onBlur={e => updateThinking(e.target.value)}
            className="w-full px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" placeholder="hmm, let me see, one moment" />
        </div>
      </div>
      <div className="pt-2 border-t border-p-border-light">
        <button onClick={restoreLang} disabled={saveMut.isPending}
          className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800">
          Restore {lang.toUpperCase()} to default
        </button>
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
  { key: 'audiosocket_port', label: 'AudioSocket Port', desc: 'TCP port for Asterisk connections', def: '9092' },
  { key: 'http_api_port', label: 'HTTP API Port', desc: 'Port for outbound call management', def: '9093' },
]
const LIFECYCLE_FIELDS = [
  { key: 'idle_timeout_s', label: 'Idle Timeout (s)', desc: 'Disconnect after silence', def: '30' },
  { key: 'call_max_duration_s', label: 'Max Call Duration (s)', desc: 'Hard limit per call', def: '600' },
  { key: 'outbound_call_timeout_s', label: 'Outbound Call Timeout (s)', desc: 'Max wait for call completion', def: '300' },
  { key: 'question_answer_timeout_s', label: 'Q&A Timeout (s)', desc: 'Max wait for manager answer', def: '40' },
]

function InfrastructureSection() {
  const { data: settings } = usePhoneSettings()
  const saveMut = useSavePhoneSettings()
  const [savedField, setSavedField] = useState('')

  const save = (key: string, value: string) => {
    if (value !== (settings?.[key] ?? '')) {
      saveMut.mutate({ [key]: value }, { onSuccess: () => { setSavedField(key); setTimeout(() => setSavedField(''), 2000) } })
    }
  }

  const numRow = (f: { key: string; label: string; desc: string; def: string }) => (
    <SettingRow key={f.key} label={f.label} description={f.desc}>
      <input type="number" defaultValue={settings?.[f.key] ?? f.def} key={`${f.key}-${settings?.[f.key] ?? ''}`}
        onBlur={e => save(f.key, e.target.value)}
        className="w-28 px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text text-right" />
      <SavedBadge show={savedField === f.key} />
    </SettingRow>
  )

  return (
    <div className="space-y-5">
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">Transport</h4>
        <div className="space-y-2">{TRANSPORT_FIELDS.map(numRow)}</div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">Call Lifecycle</h4>
        <div className="space-y-2">{LIFECYCLE_FIELDS.map(numRow)}</div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">Logging</h4>
        <SettingRow label="Log Level">
          <select value={settings?.log_level || 'INFO'} onChange={e => save('log_level', e.target.value)}
            className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text">
            {['DEBUG', 'INFO', 'WARNING', 'ERROR'].map(l => <option key={l} value={l}>{l}</option>)}
          </select>
          <SavedBadge show={savedField === 'log_level'} />
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
      <SectionCard title="Call Prompts" defaultOpen={false}><CallPromptsSection /></SectionCard>
      <SectionCard title="Languages" defaultOpen={false}><LanguagesSection /></SectionCard>
      <SectionCard title="Turn Classifier"><TurnClassifierSection /></SectionCard>
      <SectionCard title="Infrastructure" defaultOpen={false}><InfrastructureSection /></SectionCard>
      <AdvancedTuningCard />
    </div>
  )
}
