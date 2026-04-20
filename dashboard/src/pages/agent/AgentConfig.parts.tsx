import { useState } from 'react'

// ---------------------------------------------------------------------------
// Color presets
// ---------------------------------------------------------------------------

export const COLOR_PRESETS = [
  { hex: '#3B82F6', name: 'Blue' },
  { hex: '#F59E0B', name: 'Amber' },
  { hex: '#EC4899', name: 'Pink' },
  { hex: '#8B5CF6', name: 'Purple' },
  { hex: '#22C55E', name: 'Green' },
  { hex: '#14B8A6', name: 'Teal' },
  { hex: '#F43F5E', name: 'Rose' },
  { hex: '#6366F1', name: 'Indigo' },
  { hex: '#F97316', name: 'Orange' },
  { hex: '#06B6D4', name: 'Cyan' },
  { hex: '#84CC16', name: 'Lime' },
  { hex: '#64748B', name: 'Slate' },
]

// Display metadata + ordering for execution-layer engines. Order drives the
// AI Engines list and the Default Model picker (Claude Code → Codex → Direct →
// any others). Provider badges mirror the AI Engines section in User Settings.
const ENGINE_ORDER = ['claude-code-cli', 'codex-cli', 'direct-llm']
export const ENGINE_META: Record<string, { badge?: string; label?: string; desc?: string }> = {
  'claude-code-cli': { badge: 'Anthropic', label: 'Claude Code CLI' },
  'codex-cli': { badge: 'OpenAI', label: 'Codex' },
  'direct-llm': { label: 'Direct LLM API', desc: 'Not all tools supported — use only for low latency.' },
}
export function orderEngines(paths: string[]): string[] {
  return [...paths].sort(
    (a, b) =>
      (ENGINE_ORDER.indexOf(a) === -1 ? 99 : ENGINE_ORDER.indexOf(a)) -
      (ENGINE_ORDER.indexOf(b) === -1 ? 99 : ENGINE_ORDER.indexOf(b)),
  )
}

// Models per execution path are now fetched from /v1/execution-layers API
// (see useExecutionLayers hook). No hardcoded lists here.

// ---------------------------------------------------------------------------
// Small reusable pieces
// ---------------------------------------------------------------------------

export function SavedIndicator({ show }: { show: boolean }) {
  if (!show) return null
  return (
    <span className="text-[11px] text-green-600 dark:text-green-400 font-medium animate-pulse">
      Saved
    </span>
  )
}

export function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-hidden
        ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}
        ${checked ? 'bg-brand' : 'bg-gray-300 dark:bg-gray-600'}`}
    >
      <span
        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm ring-0 transition duration-200
          ${checked ? 'translate-x-4' : 'translate-x-0'}`}
      />
    </button>
  )
}

// ---------------------------------------------------------------------------
// Delete confirmation modal
// ---------------------------------------------------------------------------

export function DeleteModal({
  slug,
  onConfirm,
  onCancel,
  isPending,
}: {
  slug: string
  onConfirm: () => void
  onCancel: () => void
  isPending: boolean
}) {
  const [typed, setTyped] = useState('')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-p-border-light w-full max-w-sm mx-4 p-5">
        <h3 className="text-base font-semibold text-red-600 mb-2">Delete Agent</h3>
        <p className="text-sm text-p-text mb-3">
          This will permanently delete the agent and all its data. Type{' '}
          <span className="font-mono font-bold text-red-600">{slug}</span> to confirm.
        </p>
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={slug}
          className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-red-400 mb-4"
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={typed !== slug || isPending}
            className="px-3 py-1.5 text-sm rounded-lg bg-red-600 text-white hover:bg-red-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {isPending ? 'Deleting...' : 'Delete Agent'}
          </button>
        </div>
      </div>
    </div>
  )
}
