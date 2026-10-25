// The languages the automatic language detection may pick: the install's
// list, a subset of the platform's six (the audio setting
// `language_candidates`, read by the phone daemon for calls and live voice
// and by the dictation relay). A wrong-language move needs two candidates
// that confuse each other, so an install that lists the languages it speaks
// cannot be moved to a third one.

import { useState } from 'react'
import { useAudioSettings, useSaveAudioSettings, type AudioSettings } from '../../api/audio'
import { SUPPORTED_BASES, parseLanguageCandidates } from '../../audio/lang'
import { SettingRow } from '../ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'

const NAMES: Record<string, string> = {
  en: 'English', el: 'Greek', de: 'German', es: 'Spanish', fr: 'French', it: 'Italian',
}

export function LanguageCandidatesRow() {
  const { data: settings, isLoading } = useAudioSettings()
  const save = useSaveAudioSettings()
  const { stateOf, run } = useSaveState()
  // The boxes show the list the admin last clicked for as long as the
  // server's copy is the one that click was made over (the query changes
  // only after the refetch): nothing snaps back meanwhile, the next copy
  // takes over, and a save that fails returns to the server's list.
  const [pending, setPending] = useState<{ list: string[]; over: AudioSettings | undefined } | null>(null)
  const fetched = parseLanguageCandidates(settings?.language_candidates)
  const picked = pending && pending.over === settings ? pending.list : fetched
  const busy = isLoading || save.isPending

  const toggle = (code: string) => {
    if (busy) return
    const next = picked.includes(code) ? picked.filter(c => c !== code) : [...picked, code]
    if (!next.length) return // at least one language stays
    const ordered = SUPPORTED_BASES.filter(c => next.includes(c))
    setPending({ list: ordered, over: settings })
    void run('languages', () => save.mutateAsync({ language_candidates: ordered.join(',') }), {
      failTitle: 'The languages were not saved',
      revert: () => setPending(null),
    })
  }

  return (
    <SettingRow
      label="Languages the automatic detection may pick"
      description="Fewer is safer."
      save={stateOf('languages')}
      info="Automatic language detection picks from these. It applies to calls, live voice and dictation. Take German, Spanish and Italian callers on a phone line. With all six, about one in ten of them lands on a neighbouring language. A company that speaks Greek and English lists just those two. Then detection never moves to a third."
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 justify-end">
        {SUPPORTED_BASES.map(code => (
          <label key={code} className="flex items-center gap-1 text-xs text-primary cursor-pointer">
            <input type="checkbox" checked={picked.includes(code)} onChange={() => toggle(code)}
              disabled={busy} className="accent-brand" />
            {NAMES[code] || code}
          </label>
        ))}
      </div>
    </SettingRow>
  )
}
