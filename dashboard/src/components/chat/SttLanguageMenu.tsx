// The dictation-language menu behind a HOLD (or a right-click) on the mic:
// the rows of the Audio settings tab, saved to the server-side audio prefs
// so the next dictation start uses the pick. The audio-prefs hooks live in
// the panel, which mounts only while the menu is open: the composer renders
// VoiceControl without a QueryClient in its test suites, and the prefs only
// matter once the user holds.

import { useEffect, useState, useSyncExternalStore, type RefObject } from 'react'
import { useMyAudioPrefs, useUpdateMyAudioPrefs } from '../../api/userAudio'
import { toastError } from '../../lib/http/apiJson'
import { pushEscHandler } from '../../lib/escStack'
import { IconDropdownPanel } from '../ui/IconDropdown'
import { STT_MENU_OPTIONS, currentSttLanguage } from '../../lib/chat/sttLanguages'
import { isSttDiagOn, saveSttDiag, sttDiagCount, subscribeSttDiag } from '../../audio/sttDiag'
import { useChatAudioCapability } from '../../hooks/useChatAudioCapability'
import { resolveSttBackend } from '../../audio/resolver'

// Which recognizer the next dictation gets, in the person's words. One
// line each: the panel scrolls, and a second line sits under its edge.
const LISTENS = {
  native: 'Listens on this device.',
  platform: 'Listens through OtoDock.',
} as const

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
  const { data: cap } = useChatAudioCapability()
  // The recognizer the stored pick resolves to (nothing while the
  // capability loads): the person sees where the next dictation runs.
  const engine = cap ? resolveSttBackend(cap, prefs?.stt_mode ?? 'auto', prefs?.stt_language)?.kind ?? null : null
  // The dictation recorder's save (the debug flag on and events held): a
  // tap writes one local file, the menu stays open.
  const recorded = useSyncExternalStore(subscribeSttDiag, sttDiagCount)
  const [saveNote, setSaveNote] = useState<string | null>(null)
  const showSave = isSttDiagOn() && recorded > 0

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
      footer={(engine || duplexActive || showSave) ? (
        <>
          {engine && !duplexActive && <span className="block">{LISTENS[engine]}</span>}
          {duplexActive && 'Applies to your next conversation'}
          {showSave && (
            <button
              type="button"
              className="block mt-1 text-link hover:underline"
              onClick={() => setSaveNote(saveSttDiag() ? 'The log was saved.' : 'The log could not be saved.')}
            >
              {saveNote ?? 'Save the dictation log'}
            </button>
          )}
        </>
      ) : undefined}
    />
  )
}
