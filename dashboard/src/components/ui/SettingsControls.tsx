/**
 * The settings pages' shared pieces: the save indication, the badge, the
 * collapsible section card and the two row shapes. A row's label stands
 * alone; `description` is the one visible hint (a cost, a risk, a restart,
 * who else is affected) and `info` the longer text behind the InfoTip
 * beside the label. Pure presentational components, no data fetching.
 */

import { useId, useState, type ReactNode } from 'react'

import type { SaveStatus } from '../../hooks/useSaveState'
import { BADGE_BASE, BADGE_TONE, CARD } from './classes'
import InfoTip from './InfoTip'

/** The state `useSaveState` keeps for one field. The region is always
 *  mounted and empty at rest: a live region that mounts with its text is
 *  often not announced. Never inside a `<label>`, whose control would take
 *  the word into its name. `besideButton`: a Save button that says
 *  Saving… itself, so only Saved shows here. */
export function SaveState({ state, besideButton = false }: { state: SaveStatus; besideButton?: boolean }) {
  const saving = state === 'saving' && !besideButton
  const saved = state === 'saved'
  return (
    <span
      role="status"
      className={`${saving || saved ? 'ml-2 ' : ''}inline-flex items-center gap-1 text-xs font-medium ${saved ? 'text-status-success-fg' : 'text-secondary'}`}
    >
      {saving && 'Saving…'}
      {saved && (
        <>
          <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
          </svg>
          Saved
        </>
      )}
    </span>
  )
}

type BadgeTone = keyof typeof BADGE_TONE

export function Badge({ children, variant = 'neutral' }: { children: React.ReactNode; variant?: BadgeTone }) {
  return <span className={`${BADGE_BASE} ${BADGE_TONE[variant]}`}>{children}</span>
}

export function SectionCard({ title, children, defaultOpen = true }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className={CARD}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="w-full flex items-center justify-between p-4 text-left"
      >
        <h3 className="text-sm font-semibold text-primary">{title}</h3>
        <span className="text-secondary text-xs" aria-hidden="true">{open ? '−' : '+'}</span>
      </button>
      {open && <div className="px-4 pb-4 space-y-4">{children}</div>}
    </div>
  )
}

/** Label and hint on the left, the control on the right. */
export function SettingRow({ label, description, info, save, htmlFor, labelId, children }: {
  label: string
  /** The one visible hint, one short sentence. */
  description?: ReactNode
  /** The longer text, behind the info button. */
  info?: ReactNode
  /** The row's save state (`useSaveState().stateOf(key)`), shown after
   *  the label and its info button. */
  save?: SaveStatus
  htmlFor?: string
  /** For a Switch's `aria-labelledby`. */
  labelId?: string
  children: ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-primary">
          <label id={labelId} htmlFor={htmlFor}>{label}</label>
          {info && <InfoTip label={label}>{info}</InfoTip>}
          {save !== undefined && <SaveState state={save} />}
        </p>
        {description && <p className="text-xs text-tertiary">{description}</p>}
      </div>
      <div className="flex items-center gap-2 shrink-0">{children}</div>
    </div>
  )
}

/** A checkbox on the left, its label, info button and hint beside it. The
 *  label is stretched over the row, so a tap anywhere on it toggles the box
 *  (the hint too); the info button sits above that layer and never does. */
export function CheckboxRow({ label, description, info, save, checked, onChange, disabled, id }: {
  label: string
  description?: ReactNode
  info?: ReactNode
  save?: SaveStatus
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  id?: string
}) {
  const autoId = useId()
  const inputId = id ?? autoId
  const hintId = `${inputId}-hint`
  return (
    <div className="relative flex items-start gap-3">
      <input
        id={inputId}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-describedby={description ? hintId : undefined}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer accent-action rounded-control disabled:cursor-not-allowed"
      />
      <div className="min-w-0">
        <p className="text-sm font-medium text-primary">
          <label htmlFor={inputId} className={`after:absolute after:inset-0 ${disabled ? 'cursor-not-allowed after:cursor-not-allowed' : 'cursor-pointer after:cursor-pointer'}`}>{label}</label>
          {info && <span className="relative z-10"><InfoTip label={label}>{info}</InfoTip></span>}
          {save !== undefined && <SaveState state={save} />}
        </p>
        {description && <p id={hintId} className="text-xs text-tertiary">{description}</p>}
      </div>
    </div>
  )
}
