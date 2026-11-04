/**
 * ChatPolicyRadio — the chat audio policy selector (admin).
 *
 * Decides whether the chat sound/mic icons use the device's own voice and
 * recognizer, the platform providers, or let each person choose. Drives
 * ``audio_chat_user_policy`` (consumed by the chat-audio capability
 * resolver). "Platform only" and "User's choice" need a platform speech
 * provider to point at, so they are offered only while a chat default is
 * usable (the stored value always shows). The rule is the server's too, whose
 * write refuses only Platform only: User's choice saves without a provider.
 */

import { type AudioPolicy, platformProviderUsable, useAudioPolicy, useAudioProviders, useUpdateAudioPolicy } from '../../api/audio'
import { SaveState, SettingRow } from '../ui/SettingsControls'
import Switch from '../ui/Switch'
import { useSaveState } from '../../hooks/useSaveState'

type PolicyValue = AudioPolicy['chat_user_policy']

const OPTIONS: { value: PolicyValue; label: string; desc: string; needsProvider?: boolean }[] = [
  { value: 'native_only', label: 'Native only', desc: 'The device speaks and listens, and the icons hide where it cannot.' },
  { value: 'native_preferred', label: 'Native preferred', desc: "The device speaks and listens where it can, and the platform's speech provider does where it cannot." },
  { value: 'platform_only', label: 'Platform only', desc: "The platform's speech provider speaks and listens, and the device does only where no provider is set.", needsProvider: true },
  { value: 'user_choice', label: "User's choice", desc: 'Each person picks the device or the platform in User Settings, Audio, for every device of theirs.', needsProvider: true },
]

export function ChatPolicyRadio() {
  const { data: policy, isLoading } = useAudioPolicy()
  const { data: providers } = useAudioProviders()
  const update = useUpdateAudioPolicy()
  const { stateOf, run } = useSaveState()

  if (isLoading || !policy) return <p className="text-sm text-secondary">Loading...</p>

  const usable = platformProviderUsable(providers)
  const offered = OPTIONS.filter(opt => !opt.needsProvider || usable !== false || opt.value === policy.chat_user_policy)

  const select = (value: PolicyValue) => {
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
        {offered.map(opt => (
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
        <p className="text-xs text-tertiary">
          {usable === false
            ? "Add a speech provider below to offer the platform's voice or dictation."
            : 'Safari, iPhones, iPads and Automatic dictation use the speech provider when one is set.'}
        </p>
      </div>
    </div>
  )
}
