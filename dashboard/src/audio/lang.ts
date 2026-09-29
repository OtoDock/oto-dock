// Shared chat-audio language set + a lightweight automatic-language detector,
// mirroring audio/streaming/lang.py (keep the two in sync). Used to pick the right
// native voice and to tell the platform TTS which language to pronounce.

export const LANGUAGES: { code: string; label: string }[] = [
  { code: 'en-US', label: 'English (US)' },
  { code: 'en-GB', label: 'English (UK)' },
  { code: 'el-GR', label: 'Greek' },
  { code: 'de-DE', label: 'German' },
  { code: 'es-ES', label: 'Spanish' },
  { code: 'fr-FR', label: 'French' },
  { code: 'it-IT', label: 'Italian' },
]

export function baseLang(tag: string): string {
  return (tag || '').split('-')[0].toLowerCase()
}

// Default STT language from the browser/OS when the user hasn't picked one — the
// closest the web exposes to "keyboard language" (no API gives the active layout).
// A base code, not a tag: the recognizers accept it and the mic menu maps it to
// its first matching row.
export function browserSttLang(): string {
  try {
    const base = (navigator.language || 'en').slice(0, 2).toLowerCase()
    return ['en', 'el', 'de', 'es', 'fr', 'it'].includes(base) ? base : 'en'
  } catch { return 'en' }
}

// High-frequency function words per Latin language. A match-count scorer is enough
// to PICK A VOICE — never has to be perfect; a multilingual TTS voice pronounces
// the text either way. Greek is handled by script, above.
const STOPWORDS: Record<string, Set<string>> = {
  en: new Set(['the', 'and', 'is', 'are', 'you', 'of', 'to', 'for', 'with', 'this', 'that', 'have', 'was', 'not', 'it']),
  de: new Set(['der', 'die', 'das', 'und', 'ist', 'ich', 'nicht', 'mit', 'ein', 'eine', 'sie', 'auch', 'wird', 'haben']),
  es: new Set(['que', 'de', 'no', 'es', 'los', 'las', 'una', 'por', 'con', 'para', 'está', 'muy', 'pero', 'como']),
  fr: new Set(['le', 'les', 'est', 'une', 'des', 'que', 'pour', 'dans', 'pas', 'vous', 'avec', 'mais', 'très', 'cette']),
  it: new Set(['che', 'di', 'è', 'una', 'con', 'per', 'non', 'sono', 'gli', 'questo', 'anche', 'più', 'della', 'ma']),
}

// Pick the base TTS language (en/de/es/fr/it/el) of a message. Greek by script; the
// five Latin languages by stopword frequency. Returns `fallback` with no clear
// signal. Heuristic — it only steers voice choice.
export function detectTtsLanguage(text: string, fallback = 'en'): string {
  if (!text) return fallback
  if (/[Ͱ-Ͽἀ-῿]/.test(text)) return 'el'  // Greek + Greek Extended
  const words = (text.toLowerCase().match(/\p{L}+/gu) || []).slice(0, 200)
  if (!words.length) return fallback
  let best = fallback
  let bestScore = 0
  for (const code in STOPWORDS) {
    let score = 0
    for (const w of words) if (STOPWORDS[code].has(w)) score++
    if (score > bestScore) {
      bestScore = score
      best = code
    }
  }
  return bestScore > 0 ? best : fallback
}
