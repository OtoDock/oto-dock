// The dictation-language menu behind a HOLD (or a right-click) on the mic:
// the rows of the Audio settings tab, saved to the server-side audio prefs
// so the next dictation start uses the pick. The audio-prefs hooks live in
// the panel, which mounts only while the menu is open: the composer renders
// VoiceControl without a QueryClient in its test suites, and the prefs only
// matter once the user holds.

import { useEffect, type RefObject } from 'react'
import { useMyAudioPrefs, useUpdateMyAudioPrefs } from '../../api/userAudio'
import { toastError } from '../../lib/http/apiJson'
import { pushEscHandler } from '../../lib/escStack'
import { IconDropdownPanel } from '../ui/IconDropdown'
import { STT_MENU_OPTIONS, currentSttLanguage } from '../../lib/chat/sttLanguages'

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
      options={STT_MENU_OPTIONS}
      onPick={(code) => { update.mutateAsync({ stt_language: code }).catch(toastError('The dictation language was not saved')); onClose() }}
      footer={duplexActive ? 'Applies to your next conversation' : undefined}
    />
  )
}
