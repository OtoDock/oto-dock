/**
 * Larger section components for the Execution Layers tab split.
 */


// ---------------------------------------------------------------------------
// Setup banner
// ---------------------------------------------------------------------------

// Shown until a coding agent (Claude Code or Codex) has a working platform
// subscription.
export function SetupBanner() {
  return (
    <div className="rounded-xl border border-amber-300 bg-amber-50 dark:border-amber-900/40 dark:bg-amber-900/20 px-4 py-3">
      <div className="flex items-start gap-3">
        <svg className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M5.07 19h13.86c1.54 0 2.5-1.67 1.73-3L13.73 4a2 2 0 00-3.46 0L3.34 16c-.77 1.33.19 3 1.73 3z" />
        </svg>
        <div>
          <p className="text-sm font-semibold text-amber-800 dark:text-amber-300">Finish setting up OtoDock</p>
          <p className="text-sm text-amber-700 dark:text-amber-400/90 mt-0.5">
            Connect at least one AI engine — <span className="font-medium">Claude Code</span> or <span className="font-medium">Codex</span> — to unlock chat and agents for your team. Add an account or API key on its card below.
          </p>
        </div>
      </div>
    </div>
  )
}
