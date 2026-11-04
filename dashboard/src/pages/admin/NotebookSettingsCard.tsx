import { useState } from 'react'
import { usePlatformSettings, useSavePlatformSettings } from './PlatformPage.hooks'
import { SaveState } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import InfoTip from '../../components/ui/InfoTip'
import { Input } from '../../components/ui/Field'
import { CARD } from '../../components/ui/classes'

const KEY = 'notebook_inline_bytes'
const DEFAULT_BYTES = '3072'

/** System Settings, Notebook: how much of every prompt the Notebook's
 *  latest entries may take. Saved when the field is left. */
export default function NotebookSettingsCard() {
  const { data } = usePlatformSettings()
  const save = useSavePlatformSettings()
  const { stateOf, run } = useSaveState()
  const stored = data?.notebook_inline_bytes ?? DEFAULT_BYTES
  const [draft, setDraft] = useState(stored)
  const [filledFrom, setFilledFrom] = useState(stored)
  if (filledFrom !== stored) {
    setFilledFrom(stored)
    setDraft(stored)
  }
  if (!data) return null
  const forced = (data.forced_keys || []).includes(KEY)

  const commit = () => {
    const value = draft.trim()
    if (value === stored) return
    void run(KEY, () => save.mutateAsync({ [KEY]: value }),
      { failTitle: 'The Notebook setting was not saved', revert: () => setDraft(stored) })
  }

  return (
    <div className={`${CARD} p-5 space-y-4`}>
      <div>
        <h3 className="text-sm font-semibold text-primary">Notebook</h3>
        <p className="text-xs text-tertiary mt-0.5">
          Agents post short dated notes for the agents one delegation step away. The latest ones
          load into every session.
        </p>
      </div>
      <div className="max-w-xs">
        <div className="mb-1">
          <label htmlFor="notebook-inline-bytes" className="text-xs text-secondary">Share of each prompt in bytes</label>
          <InfoTip label="Share of each prompt in bytes">
            The newest entries that fit this size load into each session. Agents read older ones
            with their Notebook tool.
          </InfoTip>
          <SaveState state={stateOf(KEY)} />
        </div>
        <Input id="notebook-inline-bytes" type="number" min={0} max={16384} step={512}
               value={draft} disabled={forced}
               onChange={(e) => setDraft(e.target.value)}
               onBlur={commit}
               onKeyDown={(e) => { if (e.key === 'Enter') commit() }} />
        <span className="block text-3xs text-tertiary mt-1">3,072 by default. 0 loads no entries.</span>
      </div>
    </div>
  )
}
