// The dictation-language menu behind a HOLD (or a right-click) on the mic:
// the rows of the Audio settings tab, saved to the server-side audio prefs
// so the next dictation start uses the pick. The audio-prefs hooks live in
// the panel, which mounts only while the menu is open: the composer renders
// VoiceControl without a QueryClient in its test suites, and the prefs only
// matter once the user holds.

import { useEffect, type RefObject } from 'react'
import { useMyAudioPrefs, useUpdateMyAudioPrefs } from '../../api/userAudio'
import { LANGUAGES, baseLang, browserSttLang } from '../../audio/lang'
import { pushEscHandler } from '../../lib/escStack'
import { IconDropdownPanel } from '../ui/IconDropdown'

/** The row to tick: the stored preference, else the browser default the
 *  recognizer really gets — an exact code, else the first row of its base
 *  language (`browserSttLang` yields a base code). */
export function currentSttLanguage(stored: string | null | undefined): string {
  const want = stored || browserSttLang()
  const exact = LANGUAGES.find((l) => l.code === want)
  if (exact) return exact.code
  const base = baseLang(want)
  return LANGUAGES.find((l) => baseLang(l.code) === base)?.code ?? ''
}

interface SttLanguageMenuProps {
  open: boolean
  onClose: () => void
  /** VoiceControl's root wrapper: the mic, the phone toggle and this panel.
   *  A mousedown outside it closes the menu. */
  anchorRef: RefObject<HTMLElement | null>
  /** A live conversation fixed its language when it opened: say so. */
  duplexActive: boolean
}

export function SttLanguageMenu({ open, ...rest }: SttLanguageMenuProps) {
  if (!open) return null
  return <Panel {...rest} />
}

function Panel({ onClose, anchorRef, duplexActive }: Omit<SttLanguageMenuProps, 'open'>) {
  const { data: prefs } = useMyAudioPrefs()
  const update = useUpdateMyAudioPrefs()

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (anchorRef.current && !anchorRef.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    const pop = pushEscHandler(onClose)
    return () => { document.removeEventListener('mousedown', handler); pop() }
  }, [anchorRef, onClose])

  return (
    <IconDropdownPanel
      label="Dictation language"
      value={currentSttLanguage(prefs?.stt_language)}
      options={LANGUAGES.map((l) => ({ value: l.code, label: l.label }))}
      onPick={(code) => { update.mutate({ stt_language: code }); onClose() }}
      footer={duplexActive ? 'Applies to your next conversation' : undefined}
    />
  )
}
