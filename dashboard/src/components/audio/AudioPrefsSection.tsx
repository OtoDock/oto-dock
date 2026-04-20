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
import { LANGUAGES } from '../../audio/lang'

const MODES: AudioMode[] = ['auto', 'native', 'platform']

// Dictation (STT) languages — the shared chat-audio language set (BCP-47 tags the
// native recognizer accepts; normalized to provider codes server-side).
const STT_LANGS = LANGUAGES

// Friendly language name for a 2-letter code ("de" → "German — de"), via the
// platform's own Intl data so we don't ship a language table.
function langLabel(code: string): string {
  try {
    const name = new Intl.DisplayNames([navigator.language || 'en'], { type: 'language' }).of(code)
    return name && name.toLowerCase() !== code ? `${name} — ${code}` : code.toUpperCase()
  } catch { return code.toUpperCase() }
}

function ModeRow({ label, value, onChange }: { label: string; value: AudioMode; onChange: (m: AudioMode) => void }) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <span className="text-sm text-p-text">{label}</span>
      <div className="flex gap-1">
        {MODES.map(m => (
          <button
            key={m}
            onClick={() => onChange(m)}
            className={`px-2.5 py-1 text-xs rounded-lg border transition-colors capitalize ${
              value === m ? 'border-brand bg-brand/10 text-brand font-medium'
                : 'border-p-border-light text-p-text-secondary hover:border-gray-300'
            }`}
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
      <h2 className="text-lg font-medium text-p-text mb-3">Audio</h2>
      {capLoading ? (
        <p className="text-sm text-p-text-secondary">Checking audio availability…</p>
      ) : !cap ? (
        <p className="text-sm text-p-text-secondary">Couldn't check chat audio availability — reload the page to retry.</p>
      ) : !cap.icons_enabled ? (
        <p className="text-sm text-p-text-secondary">Chat audio (sound &amp; mic icons) is currently turned off by the administrator.</p>
      ) : (
        <div className="space-y-3">
          <p className="text-sm text-p-text-secondary">Sound &amp; mic icons in chat. {cap.reason}</p>

          {(cap.tts === 'either' || cap.stt === 'either') ? (
            <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4 space-y-1">
              {cap.tts === 'either' && (
                <ModeRow label="Play voice (TTS)" value={prefs?.tts_mode ?? 'auto'}
                  onChange={m => update.mutate({ tts_mode: m })} />
              )}
              {cap.stt === 'either' && (
                <ModeRow label="Dictation (STT)" value={prefs?.stt_mode ?? 'auto'}
                  onChange={m => update.mutate({ stt_mode: m })} />
              )}
            </div>
          ) : (
            <p className="text-xs text-p-text-light">Voice engine is set by the platform policy.</p>
          )}

          {/* Dictation (STT) language — the language the mic recognizes */}
          {cap.stt !== 'unavailable' && (
            <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <span className="text-sm text-p-text">Dictation language</span>
                  <p className="text-xs text-p-text-light">The language the mic recognizes (set this to dictate non-English).</p>
                </div>
                <select
                  value={prefs?.stt_language || 'en-US'}
                  onChange={e => update.mutate({ stt_language: e.target.value })}
                  className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text"
                >
                  {STT_LANGS.map(l => <option key={l.code} value={l.code}>{l.label}</option>)}
                </select>
              </div>
            </div>
          )}

          {/* Native voice — one language at a time (device-local). A full
              per-language list was cluttered on devices with many voices. */}
          {voiceLangs.length > 0 && (
            <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4 space-y-3">
              <p className="text-xs text-p-text-light">Preferred device voice for native playback on this device. Set one language at a time.</p>
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm text-p-text">Language</span>
                <select
                  value={effectiveLang}
                  onChange={e => setEditLang(e.target.value)}
                  className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text max-w-[16rem]"
                >
                  {voiceLangs.map(l => <option key={l} value={l}>{langLabel(l)}</option>)}
                </select>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm text-p-text">Voice</span>
                <select
                  value={nativeVoiceURI[effectiveLang] || ''}
                  onChange={e => setNativeVoice(effectiveLang, e.target.value)}
                  className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text max-w-[16rem]"
                >
                  <option value="">(device default)</option>
                  {voices.filter(v => v.lang.toLowerCase().startsWith(effectiveLang))
                    .map(v => <option key={v.voiceURI} value={v.voiceURI}>{v.name}</option>)}
                </select>
              </div>
              {customizedLangs.length > 0 && (
                <p className="text-xs text-p-text-light">Customized: {customizedLangs.map(l => l.toUpperCase()).join(', ')}</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
