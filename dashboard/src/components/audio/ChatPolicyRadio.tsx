/**
 * ChatPolicyRadio — the 3-mode chat audio policy selector (admin).
 *
 * Decides whether the chat sound/mic icons use the browser's native voice,
 * the platform providers, or let each user choose. Drives
 * ``audio_chat_user_policy`` (consumed by the chat-audio capability resolver).
 */

import { type AudioPolicy, useAudioPolicy, useUpdateAudioPolicy } from '../../api/audio'
import { SaveState, SettingRow } from '../ui/SettingsControls'
import Switch from '../ui/Switch'
import { useSaveState } from '../../hooks/useSaveState'

const OPTIONS: { value: AudioPolicy['chat_user_policy']; label: string; desc: string }[] = [
  { value: 'native_only', label: 'Native only', desc: 'Use only the device or browser voice. The icons hide when it is unavailable. The platform voice is never used.' },
  { value: 'native_preferred', label: 'Native preferred', desc: 'Use native when available, otherwise the platform provider. Users cannot override.' },
  { value: 'user_choice', label: 'User choice', desc: 'Each user picks native or platform in their audio settings. The choice follows them to all their devices.' },
]

export function ChatPolicyRadio() {
  const { data: policy, isLoading } = useAudioPolicy()
  const update = useUpdateAudioPolicy()
  const { stateOf, run } = useSaveState()

  if (isLoading || !policy) return <p className="text-sm text-secondary">Loading...</p>

  const select = (value: AudioPolicy['chat_user_policy']) => {
    if (value === policy.chat_user_policy) return
    void run('policy', () => update.mutateAsync({ chat_user_policy: value }), { failTitle: 'The voice setting was not saved' })
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <p className="text-xs text-tertiary">How the chat sound and mic icons choose a voice engine.</p>
        <SaveState state={stateOf('policy')} />
      </div>
      <SettingRow
        label="Chat audio"
        description="Turns the sound and mic icons on or off for everyone."
        info="When on, the icons appear wherever a native or provider voice engine works."
        save={stateOf('chat_enabled')}
      >
        <Switch
          aria-label="Chat audio"
          checked={policy.chat_enabled}
          onChange={(v) => { void run('chat_enabled', () => update.mutateAsync({ chat_enabled: v }), { failTitle: 'Chat audio was not switched' }) }}
        />
      </SettingRow>
      <div className={`space-y-1.5 ${policy.chat_enabled ? '' : 'opacity-50 pointer-events-none'}`}>
        {OPTIONS.map(opt => (
          <button
            key={opt.value}
            onClick={() => select(opt.value)}
            className={`w-full text-left p-3 rounded-card border transition-colors ${
              policy.chat_user_policy === opt.value
                ? 'border-action bg-action-subtle'
                : 'border-default hover:bg-surface-hover'
            }`}
          >
            <div className="flex items-center gap-2">
              <span className={`h-3.5 w-3.5 rounded-full border-2 shrink-0 ${
                policy.chat_user_policy === opt.value ? 'border-action bg-action' : 'border-strong'
              }`} />
              <span className="text-sm font-medium text-primary">{opt.label}</span>
            </div>
            <p className="text-xs text-tertiary mt-1 ml-5">{opt.desc}</p>
          </button>
        ))}
      </div>
    </div>
  )
}
