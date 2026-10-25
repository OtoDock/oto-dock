/**
 * ProviderPill — one expandable card per STT/TTS provider.
 *
 * Header: label + Configured/Not-set + default badges. Expanded: enabled-for
 * checkboxes (calls/chat), API key, per-language voices (TTS), an Advanced
 * sub-section (endpointing, STT only) with Restore, default-for radios, and
 * delete. Self-contained — drives its own mutations.
 */

import { useState } from 'react'
import {
  type AudioProvider,
  useUpdateAudioProvider,
  useDeleteAudioProvider,
  useSetProviderDefault,
  useSetProviderCredential,
  useDeleteProviderCredential,
} from '../../api/audio'
import { Badge, SaveState } from '../ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import { useDialogs } from '../ui/dialogs'
import InfoTip from '../ui/InfoTip'
import { toastError } from '../../lib/http/apiJson'
import Button from '../ui/Button'
import { Input } from '../ui/Field'
import { CARD_BORDERED } from '../ui/classes'

function num(v: unknown, fallback = ''): string {
  return v === undefined || v === null ? fallback : String(v)
}

export function ProviderPill({ provider, languages }: { provider: AudioProvider; languages: string[] }) {
  const dialogs = useDialogs()
  const update = useUpdateAudioProvider()
  const del = useDeleteAudioProvider()
  const setDefault = useSetProviderDefault()
  const setCred = useSetProviderCredential()
  const delCred = useDeleteProviderCredential()

  // Collapsed by default — providers are a long list; only a pill the user
  // explicitly expanded (persisted 'open') starts open.
  const [open, setOpen] = useState(() => localStorage.getItem(`audio-pill-${provider.id}`) === 'open')
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [credValue, setCredValue] = useState('')
  const { stateOf, run } = useSaveState()

  const toggleOpen = () => {
    const next = !open
    setOpen(next)
    localStorage.setItem(`audio-pill-${provider.id}`, next ? 'open' : 'closed')
  }

  const saveField = (data: Parameters<typeof update.mutate>[0]['data'], field: string) => {
    void run(field, () => update.mutateAsync({ id: provider.id, data }), { failTitle: 'The provider setting was not saved' })
  }

  const toggleEnabled = (ctx: 'calls' | 'chat', v: boolean) =>
    saveField(ctx === 'calls' ? { enabled_for_calls: v } : { enabled_for_chat: v }, `enabled_${ctx}`)

  const saveVoice = (lang: string, voiceId: string) => {
    const voices = { ...provider.voices, [lang]: voiceId }
    saveField({ voices }, `voice_${lang}`)
  }

  const saveAdvanced = (key: string, value: string) => {
    const advanced = { ...provider.advanced, [key]: value === '' ? undefined : Number(value) }
    saveField({ advanced }, `adv_${key}`)
  }

  const saveAdvancedStr = (key: string, value: string) => {
    const advanced = { ...provider.advanced, [key]: value || undefined }
    saveField({ advanced }, `adv_${key}`)
  }

  // The engine's defaults come from the server (the provider class is the
  // single source of truth) — they decide which advanced fields render.
  const advDefaults = provider.advanced_defaults || {}

  const restoreAdvanced = () => saveField({ advanced: { ...advDefaults } }, 'adv_restore')

  const saveCredential = () => {
    if (!credValue.trim()) return
    void run('cred', () => setCred.mutateAsync({ id: provider.id, value: credValue.trim() }).then(() => setCredValue('')),
      { failTitle: 'The API key was not saved' })
  }

  const isStt = provider.provider_type === 'stt'

  return (
    <div className={CARD_BORDERED}>
      {/* flex-wrap: on narrow screens the badge group drops to its own row
          instead of overlapping the truncated label */}
      <button type="button" onClick={toggleOpen} aria-expanded={open} className="w-full flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5 p-3 text-left">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-medium text-primary truncate">{provider.label}</span>
          <span className="text-xs text-tertiary font-mono shrink-0">{provider.provider_name}</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {provider.is_default_calls && <Badge variant="action">Default · calls</Badge>}
          {provider.is_default_chat && <Badge variant="action">Default · chat</Badge>}
          {provider.credential_key && (
            <Badge variant={provider.credential_configured ? 'success' : 'neutral'}>
              {provider.credential_configured ? 'Configured' : 'Not set'}
            </Badge>
          )}
          <span className="text-secondary text-xs ml-1">{open ? '−' : '+'}</span>
        </div>
      </button>

      {open && (
        <div className="px-3 pb-3 space-y-3 border-t border-subtle pt-3">
          {/* Enabled-for checkboxes */}
          <div className="flex items-center gap-5">
            {(['calls', 'chat'] as const).map(ctx => (
              <span key={ctx} className="inline-flex items-center">
                <label className="flex items-center gap-1.5 text-sm text-primary cursor-pointer">
                  <input
                    type="checkbox"
                    checked={ctx === 'calls' ? provider.enabled_for_calls : provider.enabled_for_chat}
                    onChange={e => toggleEnabled(ctx, e.target.checked)}
                    className="accent-action"
                  />
                  Enabled for {ctx}
                </label>
                <SaveState state={stateOf(`enabled_${ctx}`)} />
              </span>
            ))}
          </div>

          {/* Credential — min-w-0 lets the input shrink below its intrinsic
              width so the buttons never push past the viewport on mobile */}
          {provider.credential_key && (
            <div className="flex gap-2">
              <Input
                type="password"
                value={credValue}
                onChange={e => setCredValue(e.target.value)}
                placeholder={provider.credential_configured ? '********' : 'Enter API key'}
                className="flex-1 min-w-0 font-mono"
              />
              <Button
                variant="primary"
                size="xs"
                className="shrink-0"
                onClick={saveCredential}
                disabled={!credValue.trim() || setCred.isPending}
              >
                Save
              </Button>
              {provider.credential_configured && (
                <button
                  onClick={async () => {
                    if (await dialogs.confirm({ title: `Remove the ${provider.label} API key?`, confirmLabel: 'Remove', tone: 'danger' })) delCred.mutateAsync(provider.id).catch(toastError('The API key was not removed'))
                  }}
                  className="shrink-0 px-3 py-1.5 text-xs rounded-control border border-status-danger-border text-status-danger-fg hover:bg-status-danger-bg"
                >
                  Remove
                </button>
              )}
              <SaveState state={stateOf('cred')} />
            </div>
          )}

          {/* Voices (TTS only) */}
          {!isStt && (
            <div className="space-y-1.5">
              <p className="text-xs text-tertiary">
                Voice ID per language
                {languages.some(lang => provider.voice_defaults?.[lang]) && (
                  <InfoTip label="Voice ID per language">Blank: the voice shipped for this language</InfoTip>
                )}
              </p>
              {languages.length === 0 && (
                <p className="text-xs text-tertiary">Add languages in the Languages section first.</p>
              )}
              {languages.map(lang => (
                <div key={lang} className="flex items-center gap-2">
                  <Badge variant="info">{lang.toUpperCase()}</Badge>
                  <Input
                    defaultValue={provider.voices[lang] || ''}
                    onBlur={e => { if (e.target.value !== (provider.voices[lang] || '')) saveVoice(lang, e.target.value) }}
                    className="flex-1 min-w-0 font-mono"
                    placeholder={provider.voice_defaults?.[lang] || 'Voice id'}
                  />
                  <SaveState state={stateOf(`voice_${lang}`)} />
                </div>
              ))}
            </div>
          )}

          {/* Default-for buttons */}
          <div className="flex items-center gap-2">
            {(['calls', 'chat'] as const).map(ctx => {
              const isDefault = ctx === 'calls' ? provider.is_default_calls : provider.is_default_chat
              const enabled = ctx === 'calls' ? provider.enabled_for_calls : provider.enabled_for_chat
              return (
                <button
                  key={ctx}
                  onClick={() => { void run(`default:${ctx}`, () => setDefault.mutateAsync({ id: provider.id, context: ctx }), { failTitle: 'The default provider was not set' }) }}
                  disabled={isDefault || !enabled}
                  title={!enabled ? `Enable for ${ctx} first` : ''}
                  className={`px-2.5 py-1 text-xs rounded-control border transition-colors ${
                    isDefault
                      ? 'border-action bg-action-subtle text-link font-medium'
                      : 'border-default text-secondary hover:bg-surface-hover disabled:opacity-40'
                  }`}
                >
                  {isDefault ? `Default for ${ctx}` : `Set default · ${ctx}`}
                </button>
              )
            })}
            <SaveState state={stateOf('default')} />
          </div>

          {/* Advanced — Model ID (universal) + the engine's own declared settings */}
          <div>
            <button
              onClick={() => setShowAdvanced(v => !v)}
              className="text-xs text-secondary hover:text-primary"
            >
              {showAdvanced ? '▾' : '▸'} Advanced
            </button>
            {showAdvanced && (
              <div className="mt-2 space-y-2 pl-3 border-l-2 border-subtle">
                {/* Model ID — both STT + TTS (provider engine model) */}
                <div className="flex items-center justify-between gap-2">
                  <label className="text-sm text-primary">
                    Model ID
                    <span className="block text-xs text-tertiary">leave blank for the built-in default</span>
                  </label>
                  <div className="flex items-center gap-2 shrink-0">
                    <Input
                      defaultValue={num(provider.advanced.model_id)}
                      onBlur={e => { if (e.target.value !== num(provider.advanced.model_id)) saveAdvancedStr('model_id', e.target.value) }}
                      placeholder={provider.model_defaults?.model_id || ''}
                      className="max-w-40 font-mono"
                    />
                    <SaveState state={stateOf('adv_model_id')} />
                  </div>
                </div>
                {/* Endpointing — only engines that declare it (streaming STT, e.g. Deepgram) */}
                {(['call_endpointing_ms', 'chat_endpointing_ms'] as const).filter(key => key in advDefaults).map(key => (
                  <div key={key} className="flex items-center justify-between gap-2">
                    <label className="text-sm text-primary">
                      {key === 'call_endpointing_ms' ? 'Call' : 'Chat'} endpointing in ms
                    </label>
                    <div className="flex items-center gap-2 shrink-0">
                      <Input
                        type="number"
                        defaultValue={num(provider.advanced[key])}
                        placeholder={num(advDefaults[key])}
                        onBlur={e => { if (e.target.value !== num(provider.advanced[key])) saveAdvanced(key, e.target.value) }}
                        className="max-w-24 text-right"
                      />
                      <SaveState state={stateOf(`adv_${key}`)} />
                    </div>
                  </div>
                ))}
                {Object.keys(advDefaults).length > 0 && (
                  <>
                    <button onClick={restoreAdvanced} className="text-xs text-secondary hover:text-primary">
                      Restore defaults
                    </button>
                    <SaveState state={stateOf('adv_restore')} />
                  </>
                )}
              </div>
            )}
          </div>

          {/* Delete */}
          <div className="pt-2 border-t border-subtle flex justify-end">
            <button
              onClick={async () => {
                if (!(await dialogs.confirm({ title: `Delete the provider "${provider.label}"?`, confirmLabel: 'Delete', tone: 'danger' }))) return
                del.mutate(provider.id, { onError: toastError('The provider was not deleted') })
              }}
              className="text-xs text-status-danger-fg hover:underline"
            >
              Delete provider
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
