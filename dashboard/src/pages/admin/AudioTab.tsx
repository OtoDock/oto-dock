/**
 * Admin Audio tab — chat audio policy and STT/TTS providers. Advanced
 * call/audio tuning (VAD, smart-turn, barge-in, turn timing, fillers) lives on
 * the Phone Servers tab, alongside the rest of the call-only config (languages,
 * turn classifier, servers/routes/prompts).
 */

import { useState } from 'react'
import {
  useAudioProviders,
  useCreateAudioProvider,
  useKnownProviders,
} from '../../api/audio'
import { usePhoneSettings } from '../../api/phone'
import { SectionCard } from '../../components/ui/SettingsControls'
import { ProviderPill } from '../../components/audio/ProviderPill'
import { ChatPolicyRadio } from '../../components/audio/ChatPolicyRadio'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function parseLanguages(phrasesRaw: string | undefined): string[] {
  try { return Object.keys(JSON.parse(phrasesRaw || '{}')) } catch { return [] }
}

// ---------------------------------------------------------------------------
// Providers (STT or TTS)
// ---------------------------------------------------------------------------

function AddProviderForm({ providerType }: { providerType: 'stt' | 'tts' }) {
  const { data: providers } = useAudioProviders()
  const { data: known } = useKnownProviders()
  const create = useCreateAudioProvider()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')

  // Offer only the supported engines not already added.
  const existing = new Set((providers || []).filter(p => p.provider_type === providerType).map(p => p.provider_name))
  const available = (known?.[providerType] || []).filter(n => !existing.has(n))

  const submit = () => {
    if (!name) return
    create.mutate({
      provider_type: providerType,
      provider_name: name,
      label: name.charAt(0).toUpperCase() + name.slice(1),
      credential_key: `audio-${name}`,
      // New providers start disabled so they can't silently become a default.
      enabled_for_calls: false,
      enabled_for_chat: false,
    }, {
      onSuccess: () => { setName(''); setOpen(false) },
      onError: (e) => alert((e as Error).message),
    })
  }

  if (available.length === 0) {
    return <p className="text-xs text-p-text-light">All supported {providerType.toUpperCase()} engines are added.</p>
  }
  if (!open) {
    return (
      <button onClick={() => { setName(available[0]); setOpen(true) }} className="text-xs text-brand hover:underline">
        + Add provider
      </button>
    )
  }
  return (
    <div className="p-3 border border-p-border-light rounded-lg bg-gray-50 dark:bg-gray-800/40 space-y-2">
      <p className="text-xs text-p-text-light">
        Pick a supported {providerType.toUpperCase()} engine. Created disabled — enable it + set a credential once you're ready.
      </p>
      <div className="flex gap-2">
        <select value={name} onChange={e => setName(e.target.value)}
          className="flex-1 px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text font-mono">
          {available.map(n => <option key={n} value={n}>{n}</option>)}
        </select>
        <button onClick={() => setOpen(false)} className="px-3 py-1 text-xs rounded-lg border border-p-border-light text-p-text-secondary">Cancel</button>
        <button onClick={submit} disabled={!name || create.isPending}
          className="px-3 py-1 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40">
          {create.isPending ? 'Adding...' : 'Add'}
        </button>
      </div>
    </div>
  )
}

function ProvidersSection({ providerType, languages }: { providerType: 'stt' | 'tts'; languages: string[] }) {
  const { data: providers, isLoading } = useAudioProviders()
  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>
  const rows = (providers || []).filter(p => p.provider_type === providerType)
  return (
    <div className="space-y-3">
      {rows.length === 0
        ? <p className="text-sm text-p-text-secondary">No {providerType.toUpperCase()} providers.</p>
        : rows.map(p => <ProviderPill key={p.id} provider={p} languages={languages} />)}
      <AddProviderForm providerType={providerType} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

export default function AudioTab() {
  const { data: phoneSettings } = usePhoneSettings()
  const languages = parseLanguages(phoneSettings?.phrases)

  return (
    <div className="space-y-4">
      <SectionCard title="Chat Audio Policy"><ChatPolicyRadio /></SectionCard>
      <SectionCard title="STT Providers"><ProvidersSection providerType="stt" languages={languages} /></SectionCard>
      <SectionCard title="TTS Providers"><ProvidersSection providerType="tts" languages={languages} /></SectionCard>
    </div>
  )
}
