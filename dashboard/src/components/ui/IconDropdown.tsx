/**
 * A small icon button that opens a menu of options — the chat status bar's
 * mode and model pickers, the MCP lists' category filter. Closes on an
 * outside mousedown and on selection; `direction` says which way the menu
 * opens (the status bar sits at the bottom of the page and opens upward).
 */

import { useState, useEffect, useRef, type JSX, type ReactNode } from 'react'

import { TierMark } from '../common/TierMark'
import { tierTitle } from '../../lib/tiers'

export interface IconDropdownOption {
  value: string
  label: string
  /** A model row's capability tier: rendered as the four-dot mark, with the
   * tier word and the "good at" line as the row's tooltip. */
  tier?: number | null
  tierLabel?: string
  goodAt?: string
}
export interface IconDropdownGroup { layer: string; layerLabel: string; models: IconDropdownOption[] }

/**
 * The popup itself — the container, the header, the option rows with the
 * brand checkmark — positioned against the nearest `relative` ancestor.
 * IconDropdown renders it under its trigger; the mic's dictation-language
 * menu renders it under its own hold gesture, so both look the same.
 */
export function IconDropdownPanel({ label, value, options, groups, onPick, topSlot, footer, direction = 'up' }: {
  label: string
  value: string
  options?: IconDropdownOption[]      // flat list
  groups?: IconDropdownGroup[]        // grouped by layer
  onPick: (value: string) => void
  topSlot?: JSX.Element
  /** One short muted line under the rows (a rule the pick follows). */
  footer?: ReactNode
  direction?: 'up' | 'down'
}) {
  const renderOption = (opt: IconDropdownOption) => (
    <button
      key={opt.value}
      onClick={() => onPick(opt.value)}
      title={opt.tier || opt.goodAt ? tierTitle(opt.tier, opt.tierLabel, opt.goodAt) : undefined}
      className={`w-full text-left px-3 py-1.5 text-xs transition-colors flex items-center justify-between gap-2 ${
        opt.value === value
          ? 'text-brand font-medium bg-brand-50'
          : 'text-p-text-secondary hover:bg-p-surface-hover'
      }`}
    >
      <span className="truncate">{opt.label}</span>
      <span className="flex items-center gap-1.5 shrink-0">
        <TierMark tier={opt.tier} label={opt.tierLabel} goodAt={opt.goodAt} />
        {opt.value === value && (
          <svg className="w-3 h-3 text-brand" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
          </svg>
        )}
      </span>
    </button>
  )

  const placement = direction === 'up' ? 'bottom-full mb-1' : 'top-full mt-1'

  return (
    <div className={`absolute ${placement} right-0 w-52 bg-white dark:bg-p-surface rounded-xl shadow-lg border border-p-border-light py-1 z-50 max-h-72 overflow-y-auto`}>
      <div className="px-3 py-1.5 border-b border-p-border-light">
        <p className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider">{label}</p>
      </div>
      {topSlot}
      {groups && groups.length > 0 ? (
        groups.map((g, i) => (
          <div key={g.layer}>
            {(groups.length > 1) && (
              <div className={`px-3 py-1 ${i > 0 ? 'border-t border-p-border-light mt-0.5' : ''}`}>
                <p className="text-[10px] font-medium text-p-text-light uppercase tracking-wider">{g.layerLabel}</p>
              </div>
            )}
            {g.models.map(renderOption)}
          </div>
        ))
      ) : options ? (
        options.map(renderOption)
      ) : null}
      {footer && (
        <div className="px-3 py-1.5 border-t border-p-border-light">
          <p className="text-[10px] text-p-text-light">{footer}</p>
        </div>
      )}
    </div>
  )
}

export default function IconDropdown({ label, value, options, groups, trigger, onChange, topSlot, onOpen, direction = 'up' }: {
  label: string
  value: string
  options?: IconDropdownOption[]      // flat list
  groups?: IconDropdownGroup[]        // grouped by layer
  trigger: JSX.Element
  onChange: (value: string) => void
  /** Optional control rendered at the top of the popup, under the label header
   * (e.g. the interactive-terminal switch on the Model dropdown). Lives inside
   * the dropdown ref, so interacting with it does NOT close the popup. */
  topSlot?: JSX.Element
  /** Fired on each closed→open transition (the Model dropdown uses it to
   * lazily re-probe the chat's session liveness). */
  onOpen?: () => void
  direction?: 'up' | 'down'
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => { if (!open) onOpen?.(); setOpen(!open) }}>
        {trigger}
      </button>

      {open && (
        <IconDropdownPanel
          label={label}
          value={value}
          options={options}
          groups={groups}
          topSlot={topSlot}
          direction={direction}
          onPick={(v) => { onChange(v); setOpen(false) }}
        />
      )}
    </div>
  )
}
