/**
 * Shared admin UI primitives for the Audio + Phone Servers tabs.
 *
 * Extracted from the old single Voice/Phone tab so AudioTab and
 * PhoneServersTab (and their pills) render identically without duplicating
 * markup. Pure presentational components — no data fetching.
 */

import { useState } from 'react'

export function SavedBadge({ show }: { show: boolean }) {
  if (!show) return null
  return (
    <span className="text-xs text-green-600 dark:text-green-400 font-medium animate-pulse ml-2">
      Saved
    </span>
  )
}

export function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-hidden
        ${checked ? 'bg-brand' : 'bg-gray-300 dark:bg-gray-600'}`}
    >
      <span
        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm ring-0 transition duration-200
          ${checked ? 'translate-x-4' : 'translate-x-0'}`}
      />
    </button>
  )
}

export function Badge({ children, variant = 'default' }: { children: React.ReactNode; variant?: 'default' | 'green' | 'amber' | 'blue' | 'red' }) {
  const colors = {
    default: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
    green: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
    amber: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
    blue: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
    red: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  }
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full whitespace-nowrap ${colors[variant]}`}>
      {children}
    </span>
  )
}

export function SectionCard({ title, children, defaultOpen = true }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between p-4 text-left"
      >
        <h3 className="text-sm font-semibold text-p-text">{title}</h3>
        <span className="text-p-text-secondary text-xs">{open ? '−' : '+'}</span>
      </button>
      {open && <div className="px-4 pb-4 space-y-4">{children}</div>}
    </div>
  )
}

export function SettingRow({ label, description, children }: { label: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex-1 min-w-0">
        <label className="block text-sm font-medium text-p-text">{label}</label>
        {description && <p className="text-xs text-p-text-light">{description}</p>}
      </div>
      <div className="flex items-center gap-2 shrink-0">{children}</div>
    </div>
  )
}
