/**
 * A small icon button that opens a menu of options — the chat status bar's
 * mode and model pickers, the MCP lists' category filter. Closes on an
 * outside mousedown and on selection; `direction` says which way the menu
 * opens (the status bar sits at the bottom of the page and opens upward).
 */

import { useState, useEffect, useRef, type JSX } from 'react'

export interface IconDropdownOption { value: string; label: string }
export interface IconDropdownGroup { layer: string; layerLabel: string; models: IconDropdownOption[] }

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

  const renderOption = (opt: IconDropdownOption) => (
    <button
      key={opt.value}
      onClick={() => { onChange(opt.value); setOpen(false) }}
      className={`w-full text-left px-3 py-1.5 text-xs transition-colors flex items-center justify-between ${
        opt.value === value
          ? 'text-brand font-medium bg-brand-50'
          : 'text-p-text-secondary hover:bg-p-surface-hover'
      }`}
    >
      {opt.label}
      {opt.value === value && (
        <svg className="w-3 h-3 text-brand" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
        </svg>
      )}
    </button>
  )

  const placement = direction === 'up' ? 'bottom-full mb-1' : 'top-full mt-1'

  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => { if (!open) onOpen?.(); setOpen(!open) }}>
        {trigger}
      </button>

      {open && (
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
        </div>
      )}
    </div>
  )
}
