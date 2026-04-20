/**
 * Shared leaf widgets and const maps for the Execution Layers tab split.
 *
 * Leaf module: imports nothing from the sibling split files. `PROVIDER_LABELS`
 * lives here (not in .rows) because it is used by both .forms (AddModelForm) and
 * .rows (ModelsByProvider); keeping it in the leaf avoids a forms<->rows cycle.
 */

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

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

export function Badge({ children, variant = 'default' }: { children: React.ReactNode; variant?: 'default' | 'green' | 'amber' | 'red' | 'blue' }) {
  const colors = {
    default: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
    green: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
    amber: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
    red: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
    blue: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  }
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${colors[variant]}`}>
      {children}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Provider labels (shared by .forms AddModelForm and .rows ModelsByProvider)
// ---------------------------------------------------------------------------

export const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  groq: 'Groq',
  ollama: 'Ollama',
  openai_compatible: 'OpenAI-compatible endpoint',
}
