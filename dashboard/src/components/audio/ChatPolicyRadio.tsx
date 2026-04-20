/**
 * ChatPolicyRadio — the 3-mode chat audio policy selector (admin).
 *
 * Decides whether the chat sound/mic icons use the browser's native voice,
 * the platform providers, or let each user choose. Drives
 * ``audio_chat_user_policy`` (consumed by the chat-audio capability resolver).
 */

import { type AudioPolicy, useAudioPolicy, useUpdateAudioPolicy } from '../../api/audio'
import { SavedBadge, SettingRow, Toggle } from '../ui/SettingsControls'
import { useState } from 'react'

const OPTIONS: { value: AudioPolicy['chat_user_policy']; label: string; desc: string }[] = [
  { value: 'native_only', label: 'Native only', desc: 'Use only the device/browser voice. Icons hide when unavailable; platform fallback is disabled.' },
  { value: 'native_preferred', label: 'Native preferred', desc: 'Use native when available, otherwise the platform provider. Users cannot override.' },
  { value: 'user_choice', label: 'User choice', desc: 'Each user picks native vs platform in their audio preferences (synced across their devices).' },
]

export function ChatPolicyRadio() {
  const { data: policy, isLoading } = useAudioPolicy()
  const update = useUpdateAudioPolicy()
  const [saved, setSaved] = useState(false)

  if (isLoading || !policy) return <p className="text-sm text-p-text-secondary">Loading...</p>

  const flash = () => { setSaved(true); setTimeout(() => setSaved(false), 2000) }

  const select = (value: AudioPolicy['chat_user_policy']) => {
    if (value === policy.chat_user_policy) return
    update.mutate({ chat_user_policy: value }, { onSuccess: flash })
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <p className="text-xs text-p-text-light">How chat sound / mic icons choose a voice engine.</p>
        <SavedBadge show={saved} />
      </div>
      <SettingRow
        label="Chat audio"
        description="Master switch for the sound & mic icons. When on, they appear automatically wherever a usable voice engine exists (native or provider)."
      >
        <Toggle
          checked={policy.chat_enabled}
          onChange={(v) => update.mutate({ chat_enabled: v }, { onSuccess: flash })}
        />
      </SettingRow>
      <div className={`space-y-1.5 ${policy.chat_enabled ? '' : 'opacity-50 pointer-events-none'}`}>
        {OPTIONS.map(opt => (
          <button
            key={opt.value}
            onClick={() => select(opt.value)}
            className={`w-full text-left p-3 rounded-lg border transition-colors ${
              policy.chat_user_policy === opt.value
                ? 'border-brand bg-brand/5'
                : 'border-p-border-light hover:border-gray-300'
            }`}
          >
            <div className="flex items-center gap-2">
              <span className={`h-3.5 w-3.5 rounded-full border-2 shrink-0 ${
                policy.chat_user_policy === opt.value ? 'border-brand bg-brand' : 'border-gray-400'
              }`} />
              <span className="text-sm font-medium text-p-text">{opt.label}</span>
            </div>
            <p className="text-xs text-p-text-light mt-1 ml-5">{opt.desc}</p>
          </button>
        ))}
      </div>
    </div>
  )
}
