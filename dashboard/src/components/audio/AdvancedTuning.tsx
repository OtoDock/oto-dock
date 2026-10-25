/**
 * Advanced call/audio tuning — experimental VAD, smart-turn, barge-in, turn
 * timing, TTS buffering, and filler-timing knobs. Rendered on the Phone Servers
 * tab (these are call-tuning settings); gated behind the `show_experimental`
 * audio-policy toggle, which is OFF by default.
 *
 * Smart Turn is always available (selected per-language via the Turn Classifier
 * picker), and backchannel / thinking-filler are toggled per phone route — so
 * those master enable toggles are intentionally absent here; only their timing
 * knobs remain.
 */

import { useAudioSettings, useSaveAudioSettings, useAudioPolicy, useUpdateAudioPolicy } from '../../api/audio'
import { usePhoneSettings, useSavePhoneSettings } from '../../api/phone'
import { SaveState, SectionCard, SettingRow } from '../ui/SettingsControls'
import Switch from '../ui/Switch'
import { useSaveState } from '../../hooks/useSaveState'
import { useDialogs } from '../ui/dialogs'
import Button from '../ui/Button'
import { Input } from '../ui/Field'

const AUDIO_ADV_DEFAULTS: Record<string, string> = {
  vad_threshold: '0.40', vad_silence_duration_ms: '350', vad_speech_pad_ms: '64',
  vad_min_energy_rms: '150', vad_silence_offset_ms: '50',
  smart_turn_threshold: '0.65', smart_turn_onnx_threads: '1', smart_turn_audio_window_s: '8.0',
}
const PHONE_ADV_DEFAULTS: Record<string, string> = {
  bargein_threshold: '0.35', bargein_debounce_ms: '300', bargein_chunk_ratio: '0.5',
  bargein_silence_duration_ms: '500', bargein_timer_s: '0.6',
  bargein_confirm_timeout_s: '3.0', bargein_resume_grace_s: '1.0',
  turn_complete_timeout_s: '1.0', turn_incomplete_timeout_s: '2.0', turn_classifier_grace_s: '0.0',
  tts_buffer_chars: '20', tts_response_gap_s: '0.4',
  backchannel_min_segments: '1', backchannel_min_gap_s: '0.4',
  thinking_filler_delay_s: '0',
}

function AdvancedSection() {
  const dialogs = useDialogs()
  const { data: audioSettings } = useAudioSettings()
  const { data: phoneSettings } = usePhoneSettings()
  const saveAudio = useSaveAudioSettings()
  const savePhone = useSavePhoneSettings()
  const { stateOf, run } = useSaveState()

  const valueOf = (domain: 'audio' | 'phone', key: string) =>
    (domain === 'audio' ? audioSettings : phoneSettings)?.[key] ?? ''

  // A refused value (the server names the key and its rule) is shown as a
  // toast and the field returns to the stored value.
  const numField = (domain: 'audio' | 'phone', key: string, label: string, info?: string, hint?: string) => (
    <SettingRow key={key} label={label} description={hint} info={info} htmlFor={`adv-${key}`} save={stateOf(key)}>
      <Input id={`adv-${key}`} type="number" step="any" defaultValue={valueOf(domain, key)}
        onBlur={e => {
          const input = e.target
          if (input.value === valueOf(domain, key)) return
          const mut = domain === 'audio' ? saveAudio : savePhone
          void run(key, () => mut.mutateAsync({ [key]: input.value }), {
            failTitle: `${label} was not saved`,
            revert: () => { input.value = valueOf(domain, key) },
          })
        }}
        className="max-w-20 text-right" />
    </SettingRow>
  )

  const restore = async () => {
    if (!(await dialogs.confirm({ title: 'Restore all experimental tuning to its defaults?', confirmLabel: 'Restore' }))) return
    void run('_all', () => Promise.all([saveAudio.mutateAsync(AUDIO_ADV_DEFAULTS), savePhone.mutateAsync(PHONE_ADV_DEFAULTS)]),
      { failTitle: 'The defaults were not restored' })
  }

  return (
    <div className="space-y-5">
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">Voice Activity Detection</h4>
        <div className="space-y-2">
          {numField('audio', 'vad_threshold', 'VAD Threshold', undefined, 'Speech probability threshold, from 0 to 1')}
          {numField('audio', 'vad_silence_duration_ms', 'Silence Duration in ms', 'Silence before SPEECH_END')}
          {numField('audio', 'vad_speech_pad_ms', 'Speech Pad in ms', 'Speech confirmation period')}
          {numField('audio', 'vad_min_energy_rms', 'Min Energy RMS', 'Minimum RMS to accept as speech')}
          {numField('audio', 'vad_silence_offset_ms', 'Silence Offset in ms', 'Added to STT endpointing')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">Smart Turn</h4>
        <p className="text-2xs text-tertiary mb-2">
          A local model that hears when a speaker has finished. It's always available. Choose
          it or the text classifier for each language in <span className="font-medium">Languages, Turn Classifier</span>.
        </p>
        <div className="space-y-2">
          {numField('audio', 'smart_turn_threshold', 'Smart Turn Threshold')}
          {numField('audio', 'smart_turn_onnx_threads', 'ONNX Threads')}
          {numField('audio', 'smart_turn_audio_window_s', 'Audio Window in s')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">Barge-In on Calls</h4>
        <div className="space-y-2">
          {numField('phone', 'bargein_threshold', 'Threshold', 'Speech threshold during TTS')}
          {numField('phone', 'bargein_debounce_ms', 'Debounce in ms', 'Sliding window size')}
          {numField('phone', 'bargein_chunk_ratio', 'Chunk Ratio', 'Required speech ratio in window')}
          {numField('phone', 'bargein_silence_duration_ms', 'Silence Duration in ms', 'Silence before SPEECH_END during barge-in')}
          {numField('phone', 'bargein_timer_s', 'Min Speech in s', 'Shorter speech over playback, like acks or coughs, is ignored')}
          {numField('phone', 'bargein_confirm_timeout_s', 'Confirm Timeout in s', 'Paused playback resumes if no transcript by then')}
          {numField('phone', 'bargein_resume_grace_s', 'Resume Grace in s', 'Wait for the final transcript after speech ends')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">Turn Timing on Calls</h4>
        <div className="space-y-2">
          {numField('phone', 'turn_complete_timeout_s', 'Complete Timeout in s', 'Timeout for complete sentences')}
          {numField('phone', 'turn_incomplete_timeout_s', 'Incomplete Timeout in s', 'Timeout for incomplete sentences')}
          {numField('phone', 'turn_classifier_grace_s', 'Grace Period in s', 'After "complete" classification')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">TTS Buffering on Calls</h4>
        <div className="space-y-2">
          {numField('phone', 'tts_buffer_chars', 'Buffer Chars', 'Min chars before flushing to TTS')}
          {numField('phone', 'tts_response_gap_s', 'Response Gap in s', 'Pause between consecutive responses')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-2">Fillers on Calls</h4>
        <p className="text-2xs text-tertiary mb-2">
          Each phone route turns backchannel sounds and thinking fillers on or off,
          in Routes, Edit. These settings time them when they are on.
        </p>
        <div className="space-y-2">
          {numField('phone', 'backchannel_min_segments', 'Min Segments', 'Play after Nth segment')}
          {numField('phone', 'backchannel_min_gap_s', 'Min Gap in s', 'Minimum silence before playing')}
          {numField('phone', 'thinking_filler_delay_s', 'Thinking Delay in s', 'Delay before playing filler')}
        </div>
      </div>
      <div className="pt-2 border-t border-subtle">
        <Button variant="secondary" size="xs" onClick={restore} disabled={saveAudio.isPending || savePhone.isPending}>
          Restore Defaults
        </Button>
        <SaveState state={stateOf('_all')} />
      </div>
    </div>
  )
}

export function AdvancedTuningCard() {
  const { data: policy } = useAudioPolicy()
  const update = useUpdateAudioPolicy()
  const { stateOf, run } = useSaveState()
  const show = policy?.show_experimental ?? false
  return (
    <SectionCard title="Advanced" defaultOpen={false}>
      <SettingRow label="Show experimental tuning" info="VAD, smart-turn, barge-in, turn timing, fillers" save={stateOf('show_experimental')}>
        <Switch aria-label="Show experimental tuning" checked={show} onChange={v => { void run('show_experimental', () => update.mutateAsync({ show_experimental: v }), { failTitle: 'The setting was not saved' }) }} />
      </SettingRow>
      {show && <div className="pt-3 border-t border-subtle"><AdvancedSection /></div>}
    </SectionCard>
  )
}
