// The dictation-language menu's rows and the row it ticks.

import { AUTO_LANGUAGE, LANGUAGES, baseLang, browserSttLang, isAutoLanguage } from '../../audio/lang'

// The rows: Automatic (the platform engine identifies the language) first,
// then the languages of the Audio settings tab.
export const STT_MENU_OPTIONS: { value: string; label: string }[] = [
  { value: AUTO_LANGUAGE, label: 'Automatic' },
  ...LANGUAGES.map((l) => ({ value: l.code, label: l.label })),
]

/** The row to tick: the stored preference, else the browser default the
 *  recognizer really gets — an exact code, else the first row of its base
 *  language (`browserSttLang` yields a base code). An explicit Automatic
 *  pick ticks its own row; a person who never picked keeps the browser's. */
export function currentSttLanguage(stored: string | null | undefined): string {
  if (isAutoLanguage(stored)) return AUTO_LANGUAGE
  const want = stored || browserSttLang()
  const exact = LANGUAGES.find((l) => l.code === want)
  if (exact) return exact.code
  const base = baseLang(want)
  return LANGUAGES.find((l) => baseLang(l.code) === base)?.code ?? ''
}
