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
import { toastError } from '../../lib/http/apiJson'
import Button from '../../components/ui/Button'
import { Select } from '../../components/ui/Field'

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
      onError: toastError('The provider was not added'),
    })
  }

  if (available.length === 0) {
    return <p className="text-xs text-tertiary">All supported {providerType.toUpperCase()} engines are added.</p>
  }
  if (!open) {
    return (
      <button onClick={() => { setName(available[0]); setOpen(true) }} className="text-xs text-link hover:underline">
        + Add provider
      </button>
    )
  }
  return (
    <div className="p-3 rounded-card bg-surface-raised shadow-elevation-2 space-y-2">
      <p className="text-xs text-tertiary">
        Pick a supported {providerType.toUpperCase()} engine. It starts turned off. Turn it on and add a credential when you're ready.
      </p>
      <div className="flex gap-2">
        <Select value={name} onChange={e => setName(e.target.value)}
          className="flex-1 font-mono">
          {available.map(n => <option key={n} value={n}>{n}</option>)}
        </Select>
        <Button variant="ghost" size="xs" onClick={() => setOpen(false)}>Cancel</Button>
        <Button
          variant="primary"
          size="xs"
          onClick={submit}
          disabled={!name || create.isPending}
        >
          {create.isPending ? 'Adding...' : 'Add'}
        </Button>
      </div>
    </div>
  )
}

function ProvidersSection({ providerType, languages }: { providerType: 'stt' | 'tts'; languages: string[] }) {
  const { data: providers, isLoading } = useAudioProviders()
  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>
  const rows = (providers || []).filter(p => p.provider_type === providerType)
  return (
    <div className="space-y-3">
      {rows.length === 0
        ? <p className="text-sm text-secondary">No {providerType.toUpperCase()} providers.</p>
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
