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

import { useState } from 'react'
import { useAudioSettings, useSaveAudioSettings, useAudioPolicy, useUpdateAudioPolicy } from '../../api/audio'
import { usePhoneSettings, useSavePhoneSettings } from '../../api/phone'
import { SectionCard, SettingRow, Toggle, SavedBadge } from '../ui/SettingsControls'

const AUDIO_ADV_DEFAULTS: Record<string, string> = {
  vad_threshold: '0.40', vad_silence_duration_ms: '350', vad_speech_pad_ms: '64',
  vad_min_energy_rms: '150', vad_silence_offset_ms: '50',
  smart_turn_threshold: '0.65', smart_turn_onnx_threads: '1', smart_turn_audio_window_s: '8.0',
}
const PHONE_ADV_DEFAULTS: Record<string, string> = {
  bargein_threshold: '0.35', bargein_debounce_ms: '300', bargein_chunk_ratio: '0.5',
  bargein_silence_duration_ms: '500', bargein_timer_s: '0.6',
  turn_complete_timeout_s: '1.0', turn_incomplete_timeout_s: '2.0', turn_classifier_grace_s: '0.0',
  tts_buffer_chars: '20', tts_response_gap_s: '0.4',
  backchannel_min_segments: '1', backchannel_min_gap_s: '0.4',
  thinking_filler_delay_s: '0',
}

function AdvancedSection() {
  const { data: audioSettings } = useAudioSettings()
  const { data: phoneSettings } = usePhoneSettings()
  const saveAudio = useSaveAudioSettings()
  const savePhone = useSavePhoneSettings()
  const [savedField, setSavedField] = useState('')

  const flash = (k: string) => { setSavedField(k); setTimeout(() => setSavedField(''), 2000) }
  const valueOf = (domain: 'audio' | 'phone', key: string) =>
    (domain === 'audio' ? audioSettings : phoneSettings)?.[key] ?? ''
  const saveKey = (domain: 'audio' | 'phone', key: string, value: string) => {
    const mut = domain === 'audio' ? saveAudio : savePhone
    mut.mutate({ [key]: value }, { onSuccess: () => flash(key) })
  }

  const numField = (domain: 'audio' | 'phone', key: string, label: string, desc?: string) => (
    <SettingRow key={key} label={label} description={desc}>
      <input type="number" step="any" defaultValue={valueOf(domain, key)}
        onBlur={e => { if (e.target.value !== valueOf(domain, key)) saveKey(domain, key, e.target.value) }}
        className="w-20 px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text text-right" />
      <SavedBadge show={savedField === key} />
    </SettingRow>
  )

  const restore = () => {
    if (!confirm('Restore all experimental tuning to defaults?')) return
    saveAudio.mutate(AUDIO_ADV_DEFAULTS)
    savePhone.mutate(PHONE_ADV_DEFAULTS, { onSuccess: () => flash('_all') })
  }

  return (
    <div className="space-y-5">
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">Voice Activity Detection</h4>
        <div className="space-y-2">
          {numField('audio', 'vad_threshold', 'VAD Threshold', 'Speech probability threshold (0-1)')}
          {numField('audio', 'vad_silence_duration_ms', 'Silence Duration (ms)', 'Silence before SPEECH_END')}
          {numField('audio', 'vad_speech_pad_ms', 'Speech Pad (ms)', 'Speech confirmation period')}
          {numField('audio', 'vad_min_energy_rms', 'Min Energy RMS', 'Minimum RMS to accept as speech')}
          {numField('audio', 'vad_silence_offset_ms', 'Silence Offset (ms)', 'Added to STT endpointing')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">Smart Turn (local)</h4>
        <p className="text-[11px] text-p-text-light mb-2">
          Local ONNX prosody model. Always available — pick Smart Turn vs the text classifier
          per language under <span className="font-medium">Languages → Turn Classifier</span>.
        </p>
        <div className="space-y-2">
          {numField('audio', 'smart_turn_threshold', 'Smart Turn Threshold')}
          {numField('audio', 'smart_turn_onnx_threads', 'ONNX Threads')}
          {numField('audio', 'smart_turn_audio_window_s', 'Audio Window (s)')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">Barge-In (calls)</h4>
        <div className="space-y-2">
          {numField('phone', 'bargein_threshold', 'Threshold', 'Speech threshold during TTS')}
          {numField('phone', 'bargein_debounce_ms', 'Debounce (ms)', 'Sliding window size')}
          {numField('phone', 'bargein_chunk_ratio', 'Chunk Ratio', 'Required speech ratio in window')}
          {numField('phone', 'bargein_silence_duration_ms', 'Silence Duration (ms)', 'Silence before SPEECH_END during barge-in')}
          {numField('phone', 'bargein_timer_s', 'Timer (s)', 'Min speech before TTS cancel')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">Turn Timing (calls)</h4>
        <div className="space-y-2">
          {numField('phone', 'turn_complete_timeout_s', 'Complete Timeout (s)', 'Timeout for complete sentences')}
          {numField('phone', 'turn_incomplete_timeout_s', 'Incomplete Timeout (s)', 'Timeout for incomplete sentences')}
          {numField('phone', 'turn_classifier_grace_s', 'Grace Period (s)', 'After "complete" classification')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">TTS Buffering (calls)</h4>
        <div className="space-y-2">
          {numField('phone', 'tts_buffer_chars', 'Buffer Chars', 'Min chars before flushing to TTS')}
          {numField('phone', 'tts_response_gap_s', 'Response Gap (s)', 'Pause between consecutive responses')}
        </div>
      </div>
      <div>
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider mb-2">Fillers (calls)</h4>
        <p className="text-[11px] text-p-text-light mb-2">
          Backchannel sounds and thinking fillers are turned on or off per phone route
          (Routes → edit). These are their timing knobs when enabled.
        </p>
        <div className="space-y-2">
          {numField('phone', 'backchannel_min_segments', 'Min Segments', 'Play after Nth segment')}
          {numField('phone', 'backchannel_min_gap_s', 'Min Gap (s)', 'Minimum silence before playing')}
          {numField('phone', 'thinking_filler_delay_s', 'Thinking Delay (s)', 'Delay before playing filler')}
        </div>
      </div>
      <div className="pt-2 border-t border-p-border-light">
        <button onClick={restore} disabled={saveAudio.isPending || savePhone.isPending}
          className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800">
          Restore Defaults
        </button>
        <SavedBadge show={savedField === '_all'} />
      </div>
    </div>
  )
}

export function AdvancedTuningCard() {
  const { data: policy } = useAudioPolicy()
  const update = useUpdateAudioPolicy()
  const show = policy?.show_experimental ?? false
  return (
    <SectionCard title="Advanced" defaultOpen={false}>
      <SettingRow label="Show experimental tuning" description="VAD, smart-turn, barge-in, turn timing, fillers">
        <Toggle checked={show} onChange={v => update.mutate({ show_experimental: v })} />
      </SettingRow>
      {show && <div className="pt-3 border-t border-p-border-light"><AdvancedSection /></div>}
    </SectionCard>
  )
}
