/** The map's small presentational overlays: the link/move ModeBanner, the
 * MapMenu context-menu idiom (+ its MenuIcon and MapMenuAction shape) and
 * the inline New-department form (split out of AgentsMap3D.tsx on
 * 2026-09-10). */
import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { useCreateDepartment } from '../../api/departments'
import { pushEscHandler } from '../../lib/escStack'

/** The mode banner (link / move): the app's glass idiom — dark panel,
 * classic border, the source agent as a color-tinted chip (initials +
 * name), a short hint and a proper cancel button. */
export function ModeBanner({ icon, name, color, hint, onCancel }: {
  icon: ReactNode
  name: string
  color: string
  hint: string
  onCancel: () => void
}) {
  const initials = name.trim().slice(0, 2).toUpperCase()
  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2 pl-3 pr-1.5 py-1.5 rounded-xl bg-[#171b30]/90 border border-white/12 backdrop-blur-md shadow-2xl">
      <span className="text-brand-light shrink-0">{icon}</span>
      <span
        className="flex items-center gap-1.5 pl-1 pr-2 py-0.5 rounded-lg border shrink min-w-0"
        style={{ background: `${color}1f`, borderColor: `${color}55` }}
      >
        <span
          className="flex items-center justify-center w-[18px] h-[18px] rounded-md text-[8px] font-bold shrink-0"
          style={{ background: `${color}33`, color }}
        >
          {initials}
        </span>
        <span className="text-xs font-medium text-slate-100 max-w-36 truncate">{name}</span>
      </span>
      <span className="text-xs text-slate-400 whitespace-nowrap">{hint}</span>
      <button
        onClick={onCancel}
        aria-label="Cancel"
        className="p-1 rounded-md text-slate-400 hover:text-slate-200 hover:bg-white/8 shrink-0"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  )
}

export function MenuIcon({ d }: { d: string }) {
  return (
    <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={d} />
    </svg>
  )
}

export interface MapMenuAction {
  key: string
  label: string
  tone?: 'default' | 'danger'
  icon: ReactNode
  onClick: () => void
}

/** The workspace context-menu idiom (FileContextMenu's exact styling) with
 * an optional header block: agent/department name + subtitle (operator
 * 2026-08-15: no description — the menu is for acting, not reading). */
export function MapMenu({ x, y, header, actions, onClose }: {
  x: number
  y: number
  header?: { title: string; subtitle?: string }
  actions: MapMenuAction[]
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => pushEscHandler(onClose), [onClose])
  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', onMouseDown)
    return () => document.removeEventListener('mousedown', onMouseDown)
  }, [onClose])

  // Clamp inside viewport.
  const maxX = typeof window !== 'undefined' ? window.innerWidth - 210 : x
  const maxY = typeof window !== 'undefined' ? window.innerHeight - 240 : y
  const clampedX = Math.min(x, maxX)
  const clampedY = Math.min(y, maxY)

  return (
    <div
      ref={ref}
      style={{ position: 'fixed', top: clampedY, left: clampedX, zIndex: 60 }}
      className="min-w-[190px] max-w-[250px] bg-white dark:bg-p-surface rounded-lg border border-p-border-light shadow-lg py-1"
    >
      {header && (
        <div className={`px-3 pt-1.5 pb-2 ${actions.length ? 'mb-1 border-b border-p-border-light' : ''}`}>
          <div className="text-sm font-medium text-p-text truncate">{header.title}</div>
          {header.subtitle && (
            <div className="text-[11px] text-p-text-secondary mt-0.5">{header.subtitle}</div>
          )}
        </div>
      )}
      {actions.map((a) => (
        <button
          key={a.key}
          onClick={() => {
            a.onClick()
            onClose()
          }}
          className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs ${
            a.tone === 'danger'
              ? 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20'
              : 'text-p-text hover:bg-p-surface-hover'
          }`}
        >
          {a.icon}
          <span>{a.label}</span>
        </button>
      ))}
    </div>
  )
}

export function NewDepartmentInline({ name, onName, onClose, onCreated }: {
  name: string
  onName: (v: string) => void
  onClose: () => void
  onCreated: (id: string) => void
}) {
  const create = useCreateDepartment()
  return (
    <div className="absolute z-30 top-14 left-1/2 -translate-x-1/2 rounded-xl bg-[#171b30]/95 border border-white/10 shadow-2xl backdrop-blur-md p-3 flex items-center gap-2">
      <input
        autoFocus
        value={name}
        onChange={(e) => onName(e.target.value)}
        placeholder="Department name"
        className="px-2 py-1 text-sm rounded-md bg-transparent border border-white/12 text-slate-200 outline-none focus:border-brand"
        onKeyDown={(e) => { if (e.key === 'Escape') onClose() }}
      />
      <button
        disabled={!name.trim() || create.isPending}
        className="px-2.5 py-1 text-xs rounded-md bg-brand text-white hover:bg-brand-hover disabled:opacity-50"
        onClick={() =>
          create.mutate({ name: name.trim() }, {
            onSuccess: (d) => onCreated(d.id),
          })}
      >
        Create
      </button>
      <button onClick={onClose} className="text-slate-500 hover:text-slate-300 text-xs">✕</button>
      {create.isError && (
        <span className="text-xs text-p-error">{(create.error as Error).message}</span>
      )}
    </div>
  )
}
