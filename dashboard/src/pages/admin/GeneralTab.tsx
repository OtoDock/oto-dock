import { useState, useEffect } from 'react'
import { usePlatformSettings, useSavePlatformSettings } from './PlatformPage.hooks'
import { SavedBadge } from './PlatformPage.shared'

// ---------------------------------------------------------------------------
// General tab
// ---------------------------------------------------------------------------
export default function GeneralTab() {
  const { data, isLoading } = usePlatformSettings()
  const saveMutation = useSavePlatformSettings()

  const [companyName, setCompanyName] = useState('')
  const [instructions, setInstructions] = useState('')
  const [savedField, setSavedField] = useState('')

  useEffect(() => {
    if (data) {
      setCompanyName(data.company_name)
      setInstructions(data.platform_instructions)
    }
  }, [data])

  const save = (field: string, value: string | boolean) => {
    saveMutation.mutate(
      { [field]: value },
      { onSuccess: () => { setSavedField(field); setTimeout(() => setSavedField(''), 2000) } },
    )
  }

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <div className="space-y-6">
      {/* Identity */}
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-5">
        <h3 className="text-sm font-semibold text-p-text">Identity</h3>

        <div>
          <label className="block text-sm font-medium text-p-text mb-1">Company Name</label>
          <p className="text-xs text-p-text-light mb-2">
            Shown as a heading in every agent's system prompt.
          </p>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={companyName}
              onChange={(e) => setCompanyName(e.target.value)}
              onBlur={() => { if (companyName !== data?.company_name) save('company_name', companyName) }}
              placeholder="e.g., Acme Corp"
              className="flex-1 px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
            />
            <SavedBadge show={savedField === 'company_name'} />
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-p-text mb-1">General Instructions</label>
          <p className="text-xs text-p-text-light mb-2">
            Rules and context injected into all agents' prompts.
          </p>
          <textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={6}
            placeholder="e.g., Always respond in English. Be professional and concise."
            className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 resize-y"
          />
          <div className="flex items-center mt-2">
            <button
              onClick={() => save('platform_instructions', instructions)}
              disabled={instructions === data?.platform_instructions || saveMutation.isPending}
              className="px-3 py-1.5 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Save Instructions
            </button>
            <SavedBadge show={savedField === 'platform_instructions'} />
          </div>
        </div>
      </div>
    </div>
  )
}
