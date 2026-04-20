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
import { Badge, SavedBadge } from '../ui/SettingsControls'

function num(v: unknown, fallback = ''): string {
  return v === undefined || v === null ? fallback : String(v)
}

export function ProviderPill({ provider, languages }: { provider: AudioProvider; languages: string[] }) {
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
  const [saved, setSaved] = useState('')

  const toggleOpen = () => {
    const next = !open
    setOpen(next)
    localStorage.setItem(`audio-pill-${provider.id}`, next ? 'open' : 'closed')
  }

  const flash = (field: string) => { setSaved(field); setTimeout(() => setSaved(''), 2000) }

  const saveField = (data: Parameters<typeof update.mutate>[0]['data'], field: string) =>
    update.mutate({ id: provider.id, data }, { onSuccess: () => flash(field) })

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
    setCred.mutate({ id: provider.id, value: credValue.trim() }, {
      onSuccess: () => { setCredValue(''); flash('cred') },
    })
  }

  const isStt = provider.provider_type === 'stt'

  return (
    <div className="border border-p-border-light rounded-lg">
      {/* flex-wrap: on narrow screens the badge group drops to its own row
          instead of overlapping the truncated label */}
      <button onClick={toggleOpen} className="w-full flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5 p-3 text-left">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-medium text-p-text truncate">{provider.label}</span>
          <span className="text-xs text-p-text-light font-mono shrink-0">{provider.provider_name}</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {provider.is_default_calls && <Badge variant="blue">Default · calls</Badge>}
          {provider.is_default_chat && <Badge variant="blue">Default · chat</Badge>}
          {provider.credential_key && (
            <Badge variant={provider.credential_configured ? 'green' : 'default'}>
              {provider.credential_configured ? 'Configured' : 'Not set'}
            </Badge>
          )}
          <span className="text-p-text-secondary text-xs ml-1">{open ? '−' : '+'}</span>
        </div>
      </button>

      {open && (
        <div className="px-3 pb-3 space-y-3 border-t border-p-border-light pt-3">
          {/* Enabled-for checkboxes */}
          <div className="flex items-center gap-5">
            {(['calls', 'chat'] as const).map(ctx => (
              <label key={ctx} className="flex items-center gap-1.5 text-sm text-p-text cursor-pointer">
                <input
                  type="checkbox"
                  checked={ctx === 'calls' ? provider.enabled_for_calls : provider.enabled_for_chat}
                  onChange={e => toggleEnabled(ctx, e.target.checked)}
                  className="rounded-sm border-p-border-light"
                />
                Enabled for {ctx}
                <SavedBadge show={saved === `enabled_${ctx}`} />
              </label>
            ))}
          </div>

          {/* Credential */}
          {provider.credential_key && (
            <div className="flex gap-2">
              <input
                type="password"
                value={credValue}
                onChange={e => setCredValue(e.target.value)}
                placeholder={provider.credential_configured ? '********' : 'Enter API key'}
                className="flex-1 px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text font-mono"
              />
              <button
                onClick={saveCredential}
                disabled={!credValue.trim() || setCred.isPending}
                className="px-3 py-1.5 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40"
              >
                Save
              </button>
              {provider.credential_configured && (
                <button
                  onClick={() => { if (confirm(`Remove ${provider.label} API key?`)) delCred.mutate(provider.id) }}
                  className="px-3 py-1.5 text-xs rounded-lg border border-red-200 text-red-500 hover:bg-red-50 dark:border-red-800 dark:hover:bg-red-900/20"
                >
                  Remove
                </button>
              )}
              <SavedBadge show={saved === 'cred'} />
            </div>
          )}

          {/* Voices (TTS only) */}
          {!isStt && (
            <div className="space-y-1.5">
              <p className="text-xs text-p-text-light">Voice ID per language</p>
              {languages.length === 0 && (
                <p className="text-xs text-p-text-light">Add languages in the Languages section first.</p>
              )}
              {languages.map(lang => (
                <div key={lang} className="flex items-center gap-2">
                  <Badge variant="blue">{lang.toUpperCase()}</Badge>
                  <input
                    defaultValue={provider.voices[lang] || ''}
                    onBlur={e => { if (e.target.value !== (provider.voices[lang] || '')) saveVoice(lang, e.target.value) }}
                    className="flex-1 px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text font-mono"
                    placeholder="Voice UUID"
                  />
                  <SavedBadge show={saved === `voice_${lang}`} />
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
                  onClick={() => setDefault.mutate({ id: provider.id, context: ctx })}
                  disabled={isDefault || !enabled}
                  title={!enabled ? `Enable for ${ctx} first` : ''}
                  className={`px-2.5 py-1 text-xs rounded-lg border transition-colors ${
                    isDefault
                      ? 'border-brand bg-brand/10 text-brand font-medium'
                      : 'border-p-border-light text-p-text-secondary hover:border-gray-300 disabled:opacity-40'
                  }`}
                >
                  {isDefault ? `Default for ${ctx}` : `Set default · ${ctx}`}
                </button>
              )
            })}
          </div>

          {/* Advanced — Model ID (universal) + the engine's own declared settings */}
          <div>
            <button
              onClick={() => setShowAdvanced(v => !v)}
              className="text-xs text-p-text-secondary hover:text-p-text"
            >
              {showAdvanced ? '▾' : '▸'} Advanced
            </button>
            {showAdvanced && (
              <div className="mt-2 space-y-2 pl-3 border-l-2 border-p-border-light">
                {/* Model ID — both STT + TTS (provider engine model) */}
                <div className="flex items-center justify-between gap-2">
                  <label className="text-sm text-p-text">
                    Model ID
                    <span className="block text-xs text-p-text-light">leave blank for the built-in default</span>
                  </label>
                  <div className="flex items-center gap-2">
                    <input
                      defaultValue={num(provider.advanced.model_id)}
                      onBlur={e => { if (e.target.value !== num(provider.advanced.model_id)) saveAdvancedStr('model_id', e.target.value) }}
                      placeholder={isStt ? 'nova-3' : 'sonic-3.5'}
                      className="w-40 px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text font-mono"
                    />
                    <SavedBadge show={saved === 'adv_model_id'} />
                  </div>
                </div>
                {/* Endpointing — only engines that declare it (streaming STT, e.g. Deepgram) */}
                {(['call_endpointing_ms', 'chat_endpointing_ms'] as const).filter(key => key in advDefaults).map(key => (
                  <div key={key} className="flex items-center justify-between gap-2">
                    <label className="text-sm text-p-text">
                      {key === 'call_endpointing_ms' ? 'Call' : 'Chat'} Endpointing (ms)
                    </label>
                    <div className="flex items-center gap-2">
                      <input
                        type="number"
                        defaultValue={num(provider.advanced[key])}
                        placeholder={num(advDefaults[key])}
                        onBlur={e => { if (e.target.value !== num(provider.advanced[key])) saveAdvanced(key, e.target.value) }}
                        className="w-24 px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text text-right"
                      />
                      <SavedBadge show={saved === `adv_${key}`} />
                    </div>
                  </div>
                ))}
                {Object.keys(advDefaults).length > 0 && (
                  <>
                    <button onClick={restoreAdvanced} className="text-xs text-p-text-secondary hover:text-p-text">
                      Restore defaults
                    </button>
                    <SavedBadge show={saved === 'adv_restore'} />
                  </>
                )}
              </div>
            )}
          </div>

          {/* Delete */}
          <div className="pt-2 border-t border-p-border-light flex justify-end">
            <button
              onClick={() => {
                if (!confirm(`Delete provider "${provider.label}"?`)) return
                del.mutate(provider.id, {
                  onError: (e) => alert((e as Error).message),
                })
              }}
              className="text-xs text-red-500 hover:underline"
            >
              Delete provider
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
