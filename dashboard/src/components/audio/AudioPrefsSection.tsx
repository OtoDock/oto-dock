// User audio preferences (in My Settings). Cross-device mode/language are saved
// server-side (user_audio_prefs); the per-language native voice pick is
// device-local (audioPrefsStore). Mode radios only appear when the admin policy
// is "user choice" (the capability returns "either"); otherwise the policy
// decides and there's nothing for the user to set.

import { useEffect, useState } from 'react'
import { useChatAudioCapability } from '../../hooks/useChatAudioCapability'
import { useMyAudioPrefs, useUpdateMyAudioPrefs, type AudioMode } from '../../api/userAudio'
import { useAudioPrefsStore } from '../../store/audioPrefsStore'
import { loadVoices, type VoiceOption } from '../../audio/backends/nativeTts'
import { AUTO_LANGUAGE, LANGUAGES } from '../../audio/lang'
import { useSaveState, type SaveStatus } from '../../hooks/useSaveState'
import { Select } from '../ui/Field'
import { BUTTON_BASE, BUTTON_SIZE, CARD, SEGMENT } from '../ui/classes'
import { SaveState, SettingRow } from '../ui/SettingsControls'
import { PAGE_TITLE } from '../ui/classes'

const MODES: AudioMode[] = ['auto', 'native', 'platform']

// Dictation (STT) languages — the shared chat-audio language set (BCP-47 tags the
// native recognizer accepts; normalized to provider codes server-side).
const STT_LANGS = LANGUAGES

// Friendly language name for a 2-letter code ("de" → "German (de)"), via the
// platform's own Intl data so we don't ship a language table.
function langLabel(code: string): string {
  try {
    const name = new Intl.DisplayNames([navigator.language || 'en'], { type: 'language' }).of(code)
    return name && name.toLowerCase() !== code ? `${name} (${code})` : code.toUpperCase()
  } catch { return code.toUpperCase() }
}

function ModeRow({ label, value, save, onChange }: { label: string; value: AudioMode; save: SaveStatus; onChange: (m: AudioMode) => void }) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <span className="text-sm text-primary">{label}<SaveState state={save} /></span>
      <div role="group" aria-label={label} className="flex gap-1">
        {MODES.map(m => (
          <button
            key={m}
            type="button"
            onClick={() => onChange(m)}
            aria-pressed={value === m}
            className={`${BUTTON_BASE} ${BUTTON_SIZE.xs} capitalize ${value === m ? SEGMENT.on : SEGMENT.off}`}
          >{m}</button>
        ))}
      </div>
    </div>
  )
}

export function AudioPrefsSection() {
  const { data: cap, isLoading: capLoading } = useChatAudioCapability()
  const { data: prefs } = useMyAudioPrefs()
  const update = useUpdateMyAudioPrefs()
  const { stateOf, run } = useSaveState()
  const nativeVoiceURI = useAudioPrefsStore(s => s.nativeVoiceURI)
  const setNativeVoice = useAudioPrefsStore(s => s.setNativeVoice)
  const [voices, setVoices] = useState<VoiceOption[]>([])

  // Native voices load asynchronously (Capacitor plugin on device; speechSynthesis
  // in the browser, which also fires `voiceschanged` after first populate).
  useEffect(() => {
    let alive = true
    const load = () => { loadVoices().then(v => { if (alive) setVoices(v) }) }
    load()
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
      window.speechSynthesis.addEventListener('voiceschanged', load)
      return () => { alive = false; window.speechSynthesis.removeEventListener('voiceschanged', load) }
    }
    return () => { alive = false }
  }, [])

  // Languages the device actually has voices for (derived, not hardcoded).
  const voiceLangs = [...new Set(voices.map(v => v.lang.slice(0, 2).toLowerCase()).filter(Boolean))].sort()

  // Single-language editor (avoids a long per-language list on devices with
  // many installed voices). `effectiveLang` falls back to the dictation
  // language, then English, then the first available — no sync effect needed.
  const [editLang, setEditLang] = useState('')
  const fallbackLang = (prefs?.stt_language || 'en').slice(0, 2).toLowerCase()
  const effectiveLang = (editLang && voiceLangs.includes(editLang)) ? editLang
    : voiceLangs.includes(fallbackLang) ? fallbackLang
    : voiceLangs.includes('en') ? 'en'
    : voiceLangs[0] || ''
  const customizedLangs = Object.keys(nativeVoiceURI).filter(l => nativeVoiceURI[l]).sort()

  return (
    <div className="mb-8">
      <h2 className={`${PAGE_TITLE} mb-3`}>Audio</h2>
      {capLoading ? (
        <p className="text-sm text-secondary">Checking audio availability…</p>
      ) : !cap ? (
        <p className="text-sm text-secondary">Couldn't check if chat audio is available. Reload the page to try again.</p>
      ) : !cap.icons_enabled ? (
        <p className="text-sm text-secondary">Chat audio, the sound and mic icons, is turned off by the administrator.</p>
      ) : (
        <div className="space-y-3">
          <p className="text-sm text-secondary">Sound and mic icons in chat. {cap.reason}</p>

          {(cap.tts === 'either' || cap.stt === 'either') ? (
            <div className={`${CARD} p-4 space-y-1`}>
              {cap.tts === 'either' && (
                <ModeRow label="Voice playback" value={prefs?.tts_mode ?? 'auto'} save={stateOf('tts_mode')}
                  onChange={m => { void run('tts_mode', () => update.mutateAsync({ tts_mode: m }), { failTitle: 'The voice setting was not saved' }) }} />
              )}
              {cap.stt === 'either' && (
                <ModeRow label="Dictation" value={prefs?.stt_mode ?? 'auto'} save={stateOf('stt_mode')}
                  onChange={m => { void run('stt_mode', () => update.mutateAsync({ stt_mode: m }), { failTitle: 'The dictation setting was not saved' }) }} />
              )}
            </div>
          ) : (
            <p className="text-xs text-tertiary">Voice engine is set by the platform policy.</p>
          )}

          {/* Dictation (STT) language — the language the mic recognizes */}
          {cap.stt !== 'unavailable' && (
            <div className={`${CARD} p-4`}>
              <SettingRow
                label="Dictation language"
                htmlFor="dictation-language"
                save={stateOf('stt_language')}
                info="The language the mic recognizes. Set it to dictate in a language other than English. Automatic starts in your browser language. Within a second or two, it moves to the language you speak. The device recognizer keeps your browser language."
              >
                <Select
                  id="dictation-language"
                  className="w-max shrink-0"
                  value={prefs?.stt_language || 'en-US'}
                  onChange={e => {
                    const language = e.target.value
                    void run('stt_language', () => update.mutateAsync({ stt_language: language }), { failTitle: 'The dictation language was not saved' })
                  }}
                >
                  <option value={AUTO_LANGUAGE}>Automatic</option>
                  {STT_LANGS.map(l => <option key={l.code} value={l.code}>{l.label}</option>)}
                </Select>
              </SettingRow>
            </div>
          )}

          {/* Native voice — one language at a time (device-local). A full
              per-language list was cluttered on devices with many voices. */}
          {voiceLangs.length > 0 && (
            <div className={`${CARD} p-4 space-y-3`}>
              <p className="text-xs text-tertiary">Preferred device voice for native playback on this device. Set one language at a time.</p>
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm text-primary">Language</span>
                <Select
                  value={effectiveLang}
                  onChange={e => setEditLang(e.target.value)}
                  className="max-w-[16rem]"
                >
                  {voiceLangs.map(l => <option key={l} value={l}>{langLabel(l)}</option>)}
                </Select>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm text-primary">Voice</span>
                <Select
                  value={nativeVoiceURI[effectiveLang] || ''}
                  onChange={e => setNativeVoice(effectiveLang, e.target.value)}
                  className="max-w-[16rem]"
                >
                  <option value="">Device default</option>
                  {voices.filter(v => v.lang.toLowerCase().startsWith(effectiveLang))
                    .map(v => <option key={v.voiceURI} value={v.voiceURI}>{v.name}</option>)}
                </Select>
              </div>
              {customizedLangs.length > 0 && (
                <p className="text-xs text-tertiary">Customized: {customizedLangs.map(l => l.toUpperCase()).join(', ')}</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
