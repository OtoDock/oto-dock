import { useState } from 'react'
import { usePlatformSettings, useSavePlatformSettings } from './PlatformPage.hooks'
import { SaveState } from '../../components/ui/SettingsControls'
import InfoTip from '../../components/ui/InfoTip'
import { useSaveState } from '../../hooks/useSaveState'
import Button from '../../components/ui/Button'
import { Input } from '../../components/ui/Field'
import { FIELD, FIELD_REGULAR, FIELD_BORDER, CARD } from '../../components/ui/classes'

// ---------------------------------------------------------------------------
// General tab
// ---------------------------------------------------------------------------
export default function GeneralTab() {
  const { data, isLoading } = usePlatformSettings()
  const saveMutation = useSavePlatformSettings()

  const [companyName, setCompanyName] = useState('')
  const [instructions, setInstructions] = useState('')
  const { stateOf, run } = useSaveState()

  // The saved settings fill the form when they arrive or change (in render).
  const [filledFrom, setFilledFrom] = useState(data)
  if (data && filledFrom !== data) {
    setFilledFrom(data)
    setCompanyName(data.company_name)
    setInstructions(data.platform_instructions)
  }

  const save = (field: string, value: string | boolean) => {
    void run(field, () => saveMutation.mutateAsync({ [field]: value }), { failTitle: 'The setting was not saved' })
  }

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  return (
    <div className="space-y-6">
      {/* Identity */}
      <div className={`${CARD} p-5 space-y-5`}>
        <h3 className="text-sm font-semibold text-primary">Identity</h3>

        <div>
          <div className="mb-1">
            <label className="text-sm font-medium text-primary">Company Name</label>
            <InfoTip label="Company Name">Shown as a heading in every agent's system prompt.</InfoTip>
            <SaveState state={stateOf('company_name')} />
          </div>
          <div className="flex items-center gap-2">
            <Input
              type="text"
              value={companyName}
              onChange={(e) => setCompanyName(e.target.value)}
              onBlur={() => { if (companyName !== data?.company_name) save('company_name', companyName) }}
              placeholder="e.g., Acme Corp"
              className="flex-1"
            />
          </div>
        </div>

        <div>
          <div className="mb-1">
            <label className="text-sm font-medium text-primary">General Instructions</label>
            <InfoTip label="General Instructions">Rules and context injected into all agents' prompts.</InfoTip>
          </div>
          <textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={6}
            placeholder="e.g., Always respond in English. Be professional and concise."
            className={`${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER} resize-y`}
          />
          <div className="flex items-center mt-2">
            <Button variant="primary" size="xs"
              onClick={() => save('platform_instructions', instructions)}
              disabled={instructions === data?.platform_instructions || saveMutation.isPending}
            >
              Save Instructions
            </Button>
            <SaveState state={stateOf('platform_instructions')} />
          </div>
        </div>
      </div>
    </div>
  )
}
