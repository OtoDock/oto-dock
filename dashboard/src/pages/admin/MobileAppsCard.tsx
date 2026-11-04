import { useState } from 'react'
import { usePlatformSettings, useSavePlatformSettings } from './PlatformPage.hooks'
import { SaveState, SettingRow } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import { Input } from '../../components/ui/Field'
import { CARD } from '../../components/ui/classes'

const ON = 'store_review_enabled'
const DAYS = 'store_review_days'
const DEFAULT_DAYS = '90'

/** System Settings, Mobile apps: whether the Android and iOS apps ask each
 *  person for a store rating, and how long before the second ask. The ask
 *  saves at once, the days when the field is left; a refused value goes
 *  back with the server's sentence. */
export default function MobileAppsCard() {
  const { data } = usePlatformSettings()
  const save = useSavePlatformSettings()
  const { stateOf, run } = useSaveState()
  const storedOn = data?.store_review_enabled !== false
  const storedDays = String(data?.store_review_days ?? DEFAULT_DAYS)
  const [on, setOn] = useState(storedOn)
  const [days, setDays] = useState(storedDays)
  const [filledFrom, setFilledFrom] = useState(data)
  if (data && filledFrom !== data) {
    setFilledFrom(data)
    setOn(storedOn)
    setDays(storedDays)
  }
  if (!data) return null
  const forced = (k: string) => (data.forced_keys || []).includes(k)

  const toggle = (value: boolean) => {
    setOn(value)
    void run(ON, () => save.mutateAsync({ [ON]: value }),
      { failTitle: 'The store rating setting was not saved', revert: () => setOn(storedOn) })
  }
  const commitDays = () => {
    const value = days.trim()
    if (value === storedDays) return
    void run(DAYS, () => save.mutateAsync({ [DAYS]: value }),
      { failTitle: 'The days were not saved', revert: () => setDays(storedDays) })
  }

  return (
    <div className={`${CARD} p-5 space-y-4`}>
      <div>
        <h3 className="text-sm font-semibold text-primary">Mobile apps</h3>
        <p className="text-xs text-tertiary mt-0.5">
          The Android and iOS apps can ask each person to rate OtoDock, twice at most. Google Play or the App
          Store shows its own rating window, or nothing.
        </p>
      </div>
      <SettingRow save={stateOf(ON)}
        label="Ask for a store rating"
        htmlFor="store-review-enabled"
        description="Never on the first day, never right after an error, and only after something went well."
      >
        <input
          id="store-review-enabled"
          type="checkbox"
          checked={on}
          disabled={forced(ON)}
          onChange={(e) => toggle(e.target.checked)}
          className="h-4 w-4 accent-action disabled:opacity-50 disabled:cursor-not-allowed"
        />
      </SettingRow>
      <div className="max-w-xs">
        <div className="mb-1">
          <label htmlFor="store-review-days" className="text-xs text-secondary">Days before the second ask</label>
          <SaveState state={stateOf(DAYS)} />
        </div>
        <Input id="store-review-days" type="number" min={30} max={730}
               value={days} disabled={!on || forced(DAYS)}
               onChange={(e) => setDays(e.target.value)}
               onBlur={commitDays}
               onKeyDown={(e) => { if (e.key === 'Enter') commitDays() }} />
        <span className="block text-3xs text-tertiary mt-1">90 by default, from 30 to 730.</span>
      </div>
    </div>
  )
}
