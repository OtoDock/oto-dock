import { useState, useEffect, useRef, type JSX } from 'react'

export interface ActiveAgent {
  id: string
  description: string
  type: string
  startTime: number
  background?: boolean
}

interface CacheStats {
  cacheRead: number
  cacheWrite: number
  inputTokens: number
  outputTokens: number
}

interface Props {
  streaming: boolean
  /** Session is spawning (MCP install / daemon start) — shows a "Getting ready…"
   * badge until warmup_ready. Distinct from streaming/thinking. */
  warming?: boolean
  startTime: number | null
  thinkingActive: boolean
  compressingActive: boolean
  activeAgents: ActiveAgent[]
  mode: string
  model: string
  modelValue?: string  // compound value for dropdown matching (layer::model_id)
  costUsd: number
  contextUsed: number
  contextMax: number
  cacheStats?: CacheStats
  meetingActive?: boolean
  /** Whether the active execution layer supports plan mode (Claude Code CLI
   * only — Codex and Direct LLM do not). When false, the "Plan" permission
   * option is hidden. Undefined keeps it shown (back-compat / unknown layer). */
  supportsPlanMode?: boolean
  modelOptions?: { value: string; label: string }[]
  modelGroups?: ModelGroup[]
  /** Interactive CLI toggle. When `interactiveAvailable`
   * the Model popup shows an on/off switch at the top; ON runs the chat as the
   * native TUI under a PTY, OFF runs headless `-p`. Hidden entirely for agents
   * without a claude-code-cli layer. `interactiveDisabled` locks it while a
   * session is live. */
  interactiveAvailable?: boolean
  interactiveOn?: boolean
  interactiveDisabled?: boolean
  onInteractiveToggle?: (next: boolean) => void
  /** View-toggle: while a live interactive terminal
   * exists (`richViewAvailable`), a button flips the view between the terminal
   * and the DB rich conversation history WITHOUT killing the session.
   * `richViewActive` is true while the rich history is shown. Hidden entirely
   * when no live terminal exists (the normal message list is already shown). */
  richViewAvailable?: boolean
  richViewActive?: boolean
  onToggleRichView?: () => void
  /** Interactive mode manages permissions inside the TUI (Shift+Tab) on top of
   * the platform's always-on baseline + path enforcement, so the dashboard
   * permission-mode dropdown is hidden (it can't drive the live TUI). */
  hidePermissions?: boolean
  /** In interactive mode the turn metrics (live timer, context gauge, cost) come
   * from the `-p` pump, which doesn't run — the TUI owns its own status line. Any
   * leftover values (e.g. a prior `-p` turn persisted to this chat before it was
   * switched to interactive) would render stale badges, so hide all three. The
   * "Getting ready…" warmup badge is unaffected (it still shows during spawn). */
  interactiveActive?: boolean
  /** A live interactive PTY's model can't be changed from here (use /model in
   * the TUI), so the Model dropdown shows only the active model read-only. The
   * popup still opens for the interactive switch. Also set for task-run
   * chats: a run's model is a fact of the run (the agent's configured
   * default), not a viewer choice — and the locked row renders the raw model
   * id even when the layer's current catalog no longer lists it. */
  modelLocked?: boolean
  /** Task-run chats: the permission chip shows the RUN's stored mode (the
   * scheduler's 'auto' → Don't Ask) read-only — the popup lists only the
   * active mode and selection is a no-op. */
  modeLocked?: boolean
  /** Optional content rendered on the LEFT of the status row, filling the space
   * that is otherwise the flex spacer (so the model control stays right-aligned).
   * Used by the interactive terminal control-key bar —
   * it scrolls horizontally within this slot, ~20px before the model control. */
  leftSlot?: JSX.Element
  onModeChange: (mode: string) => void
  onModelChange: (model: string) => void
  onCompactContext?: () => void
}

// --- Permission mode config ---
const MODE_CONFIG: Record<string, { label: string; bg: string; text: string; border: string; icon: JSX.Element }> = {
  default: {
    label: 'Default',
    bg: 'bg-p-surface', text: 'text-p-text-secondary', border: 'border-p-border-light/60 dark:border-gray-700',
    icon: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" /></svg>,
  },
  acceptEdits: {
    label: 'Accept Edits',
    bg: 'bg-brand-50', text: 'text-brand', border: 'border-brand/30',
    icon: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" /></svg>,
  },
  plan: {
    label: 'Plan',
    bg: 'bg-[#673a97]/10', text: 'text-p-accent-purple', border: 'border-[#673a97]/30',
    icon: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01" /></svg>,
  },
  dontAsk: {
    label: "Don't Ask",
    bg: 'bg-[#f4b206]/10', text: 'text-[#b8860b]', border: 'border-[#f4b206]/30',
    icon: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>,
  },
}

const MODE_OPTIONS = Object.entries(MODE_CONFIG).map(([value, c]) => ({ value, label: c.label }))

// --- Model config (fallback for known models) ---
const MODEL_LETTERS: Record<string, string> = {
  'claude-fable-5': 'F',
  'claude-opus-4-8[1m]': 'O',
  'claude-sonnet-5': 'S',
  'claude-haiku-4-5': 'H',
  'gpt-5': 'G',
  'o3': '3',
  'o4-mini': '4',
}

function getModelLetter(model: string): string {
  if (MODEL_LETTERS[model]) return MODEL_LETTERS[model]
  // First letter of model name, uppercased
  return (model[0] || '?').toUpperCase()
}

// Default fallback options (used when modelOptions prop not provided)
const DEFAULT_MODEL_OPTIONS = [
  { value: 'claude-fable-5', label: 'Fable 5 (1M)' },
  { value: 'claude-opus-4-8[1m]', label: 'Opus 4.8 (1M)' },
  { value: 'claude-sonnet-5', label: 'Sonnet 5 (1M)' },
]

// --- Icon dropdown (shared by mode and model) ---
interface ModelOption { value: string; label: string }
interface ModelGroup { layer: string; layerLabel: string; models: ModelOption[] }

function IconDropdown({ label, value, options, groups, trigger, onChange, topSlot }: {
  label: string
  value: string
  options?: ModelOption[]           // flat list (legacy)
  groups?: ModelGroup[]             // grouped by layer
  trigger: JSX.Element
  onChange: (value: string) => void
  /** Optional control rendered at the top of the popup, under the label header
   * (e.g. the interactive-terminal switch on the Model dropdown). Lives inside
   * the dropdown ref, so interacting with it does NOT close the popup. */
  topSlot?: JSX.Element
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

  const renderOption = (opt: ModelOption) => (
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

  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(!open)}>
        {trigger}
      </button>

      {open && (
        <div className="absolute bottom-full mb-1 right-0 w-52 bg-white dark:bg-p-surface rounded-xl shadow-lg border border-p-border-light py-1 z-50 max-h-72 overflow-y-auto">
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

// Interactive-terminal on/off switch — rendered at the top of the Model popup.
// A row: label + sliding switch. Disabled while a session is live;
// switching a running session involves a kill+rewarm.
function InteractiveToggle({ on, disabled, onToggle }: {
  on: boolean
  disabled: boolean
  onToggle?: (next: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={() => { if (!disabled) onToggle?.(!on) }}
      title={disabled
        ? 'Switching…'
        : on
          ? 'Interactive terminal is ON — toggle off to switch back to normal mode'
          : 'Run this chat as the interactive CLI (terminal)'}
      className={`w-full flex items-center justify-between gap-2 px-3 py-2 border-b border-p-border-light text-left transition-colors ${
        disabled ? 'opacity-50 cursor-not-allowed' : 'hover:bg-p-surface-hover'
      }`}
    >
      <span className="flex flex-col min-w-0">
        <span className="text-xs font-medium text-p-text">Interactive terminal</span>
        <span className="text-[10px] text-p-text-light leading-tight truncate">Claude or Codex runs as a live TUI</span>
      </span>
      <span className={`relative inline-block w-9 h-5 rounded-full shrink-0 transition-colors ${on ? 'bg-brand' : 'bg-p-border'}`}>
        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow-sm transition-transform ${on ? 'translate-x-4' : ''}`} />
      </span>
    </button>
  )
}

export default function ChatStatusBar({
  streaming,
  warming,
  startTime,
  thinkingActive,
  compressingActive,
  activeAgents,
  mode,
  model,
  costUsd,
  contextUsed,
  contextMax,
  cacheStats,
  meetingActive,
  supportsPlanMode,
  modelOptions: modelOptionsProp,
  modelGroups,
  modelValue: modelValueProp,
  interactiveAvailable,
  interactiveOn,
  interactiveDisabled,
  onInteractiveToggle,
  richViewAvailable,
  richViewActive,
  onToggleRichView,
  hidePermissions,
  interactiveActive,
  modelLocked,
  modeLocked,
  leftSlot,
  onModeChange,
  onModelChange,
  onCompactContext,
}: Props) {
  const resolvedModelOptions = modelOptionsProp && modelOptionsProp.length > 0
    ? modelOptionsProp.filter(m => m.value !== '')  // exclude "System Default" from runtime dropdown
    : DEFAULT_MODEL_OPTIONS
  const [elapsed, setElapsed] = useState(0)
  const [contextPopupOpen, setContextPopupOpen] = useState(false)
  const contextRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!streaming || !startTime) {
      setElapsed(0)
      return
    }
    const interval = setInterval(() => {
      setElapsed((Date.now() - startTime) / 1000)
    }, 100)
    return () => clearInterval(interval)
  }, [streaming, startTime])

  // Close context popup on outside click
  useEffect(() => {
    if (!contextPopupOpen) return
    const handler = (e: MouseEvent) => {
      if (contextRef.current && !contextRef.current.contains(e.target as Node)) setContextPopupOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [contextPopupOpen])

  const fgAgents = activeAgents.filter(a => !a.background && a.type !== 'delegate' && a.type !== 'bgcommand')
  const bgAgents = activeAgents.filter(a => a.background && a.type !== 'bgcommand')
  const bgCommands = activeAgents.filter(a => a.type === 'bgcommand')
  const delegatedAgents = activeAgents.filter(a => a.type === 'delegate')
  const hasActiveAgents = fgAgents.length > 0 || bgAgents.length > 0 || bgCommands.length > 0 || delegatedAgents.length > 0

  return (
    <div className="px-3 py-1.5 text-xs select-none">
      {/* Agent + meeting badges — own row on mobile, inline on desktop */}
      {(hasActiveAgents || meetingActive) && (
        <div className="flex items-center gap-2 mb-1.5 justify-end md:hidden">
          <AgentBadge agents={fgAgents} label="Foreground" bgClass="bg-brand-50 border-brand/20" textClass="text-brand" spinnerClass="border-brand" />
          <AgentBadge agents={bgAgents} label="Background" bgClass="bg-[#f4b206]/10 border-[#f4b206]/20" textClass="text-[#b8860b]" spinnerClass="border-[#f4b206]" />
          <AgentBadge agents={bgCommands} label="Commands" bgClass="bg-[#10b981]/10 border-[#10b981]/25" textClass="text-[#059669]" spinnerClass="border-[#10b981]" />
          <AgentBadge agents={delegatedAgents} label="Delegated" bgClass="bg-[#0d9488]/10 border-[#0d9488]/20" textClass="text-p-accent-teal" spinnerClass="border-p-accent-teal" />
          {meetingActive && (
            <span className="flex items-center gap-1 px-2 py-0.5 rounded-lg bg-[#0891b2]/10 border border-[#0891b2]/25 text-[#0891b2]">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-[#0891b2] animate-pulse" />
              Meeting
            </span>
          )}
        </div>
      )}

      {/* Main status row */}
      <div className="flex items-center gap-2">
        {/* Timer badge — hidden during warmup (the "Getting ready…" badge shows
            instead) and in interactive mode (no -p turn → no meaningful timer). */}
        {streaming && startTime && !warming && !interactiveActive && (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded-lg bg-p-surface border border-p-border-light/60 dark:border-gray-700 text-p-text-secondary tabular-nums font-mono">
            <svg className="w-3 h-3 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {elapsed < 60 ? `${elapsed.toFixed(0)}s` : `${Math.floor(elapsed / 60)}m ${Math.floor(elapsed % 60)}s`}
          </span>
        )}

        {/* Context gauge / Thinking / Compressing — swap on mobile to save space.
            Priority: compressing > thinking > context gauge. */}
        {warming ? (
          <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-lg bg-p-surface border border-p-border-light/60 dark:border-gray-700 text-p-text-secondary">
            <span className="inline-block w-2 h-2 border-[1.5px] border-p-text-light border-t-transparent rounded-full animate-spin" />
            Getting ready…
          </span>
        ) : compressingActive ? (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded-lg bg-[#b8860b]/10 border border-[#b8860b]/20 text-[#b8860b]">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-[#b8860b] animate-pulse" />
            Compressing
          </span>
        ) : thinkingActive ? (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded-lg bg-[#673a97]/10 border border-[#673a97]/20 text-p-accent-purple">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-p-accent-purple animate-pulse" />
            Thinking
          </span>
        ) : contextMax > 0 && !interactiveActive && (() => {
          const pct = Math.round((contextUsed / contextMax) * 100)
          const fmtK = (n: number) => n >= 1000000 ? `${(n / 1000000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(0)}K` : String(n)
          const usedK = fmtK(contextUsed)
          const maxK = fmtK(contextMax)
          const bgColor = pct >= 90 ? 'bg-p-accent-red/10 border border-p-accent-red/30'
            : pct >= 80 ? 'bg-[#f4b206]/10 border border-[#f4b206]/30'
            : 'bg-p-surface border border-p-border-light/60 dark:border-gray-700'
          const textColor = pct >= 90 ? 'text-p-accent-red'
            : pct >= 80 ? 'text-[#b8860b]'
            : 'text-p-text-secondary'
          return (
            <div className="relative" ref={contextRef}>
              <button
                onClick={() => setContextPopupOpen(!contextPopupOpen)}
                className={`flex items-center gap-1 px-2 py-0.5 rounded-lg ${bgColor} ${textColor} tabular-nums font-mono cursor-pointer`}
              >
                <svg className="w-3 h-3" viewBox="0 0 16 16">
                  <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" opacity="0.2" />
                  <circle
                    cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2"
                    strokeDasharray={`${(pct / 100) * 37.7} 37.7`}
                    strokeLinecap="round"
                    transform="rotate(-90 8 8)"
                  />
                </svg>
                {pct}%
              </button>
              {contextPopupOpen && (
                <div className="absolute bottom-full mb-1 left-0 md:left-1/2 md:-translate-x-1/2 w-52 bg-white dark:bg-p-surface rounded-xl shadow-lg border border-p-border-light py-2 px-3 z-50">
                  <p className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider mb-1.5">Context Usage</p>
                  {/* Progress bar */}
                  <div className="w-full h-1.5 rounded-full bg-p-surface mb-2">
                    <div
                      className={`h-full rounded-full transition-all ${pct >= 90 ? 'bg-p-accent-red' : pct >= 80 ? 'bg-[#f4b206]' : 'bg-brand'}`}
                      style={{ width: `${Math.min(pct, 100)}%` }}
                    />
                  </div>
                  <div className="flex justify-between text-[11px] text-p-text-secondary mb-2">
                    <span className="font-mono">{usedK} / {maxK}</span>
                    <span className={`font-semibold ${textColor}`}>{pct}%</span>
                  </div>
                  {cacheStats && (cacheStats.cacheRead > 0 || cacheStats.cacheWrite > 0 || cacheStats.outputTokens > 0) && (
                    <>
                      <div className="border-t border-p-border-light pt-1.5 mt-1">
                        <p className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider mb-1">Last Turn</p>
                      </div>
                      <div className="space-y-0.5 text-[11px] text-p-text-secondary">
                        {cacheStats.cacheRead > 0 && (
                          <div className="flex justify-between">
                            <span>Cache Read</span>
                            <span className="font-mono">{fmtK(cacheStats.cacheRead)}</span>
                          </div>
                        )}
                        {cacheStats.cacheWrite > 0 && (
                          <div className="flex justify-between">
                            <span>Cache Write</span>
                            <span className="font-mono">{fmtK(cacheStats.cacheWrite)}</span>
                          </div>
                        )}
                        {cacheStats.outputTokens > 0 && (
                          <div className="flex justify-between">
                            <span>Output</span>
                            <span className="font-mono">{fmtK(cacheStats.outputTokens)}</span>
                          </div>
                        )}
                      </div>
                    </>
                  )}
                  {onCompactContext && pct > 0 && !meetingActive && (
                    <div className="border-t border-p-border-light pt-1.5 mt-1.5">
                      <button
                        onClick={() => {
                          if (window.confirm('Compress context? This will summarize the conversation to free up context window.')) {
                            onCompactContext()
                            setContextPopupOpen(false)
                          }
                        }}
                        disabled={streaming}
                        className="w-full text-[11px] font-medium text-[#b8860b] hover:bg-[#b8860b]/10 rounded-lg py-1 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        Compress Context
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })()}

        {/* Meeting badge — desktop only (mobile shows in row above) */}
        {meetingActive && (
          <span className="hidden md:flex items-center gap-1 px-2 py-0.5 rounded-lg bg-[#0891b2]/10 border border-[#0891b2]/25 text-[#0891b2]">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-[#0891b2] animate-pulse" />
            Meeting
          </span>
        )}

        {/* Agent badges — inline on desktop, hidden on mobile (shown in row above) */}
        <div className="hidden md:contents">
          <AgentBadge agents={fgAgents} label="Foreground" bgClass="bg-brand-50 border-brand/20" textClass="text-brand" spinnerClass="border-brand" />
          <AgentBadge agents={bgAgents} label="Background" bgClass="bg-[#f4b206]/10 border-[#f4b206]/20" textClass="text-[#b8860b]" spinnerClass="border-[#f4b206]" />
          <AgentBadge agents={bgCommands} label="Commands" bgClass="bg-[#10b981]/10 border-[#10b981]/25" textClass="text-[#059669]" spinnerClass="border-[#10b981]" />
          <AgentBadge agents={delegatedAgents} label="Delegated" bgClass="bg-[#0d9488]/10 border-[#0d9488]/20" textClass="text-p-accent-teal" spinnerClass="border-p-accent-teal" />
        </div>

        {/* Left slot (interactive control-key bar) fills the spacer so the model
            control stays right-aligned; scrolls horizontally within itself. */}
        {leftSlot
          ? <div className="flex-1 min-w-0 mr-3">{leftSlot}</div>
          : <div className="flex-1" />}

        {/* Cost badge — hidden in interactive mode (cost accrues inside the TUI,
            not via the -p pump; a leftover value would be stale). */}
        {costUsd > 0 && !interactiveActive && (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded-lg bg-p-surface border border-p-border-light/60 dark:border-gray-700 text-p-text-secondary tabular-nums font-mono">
            <svg className="w-3 h-3 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            ${costUsd.toFixed(2)}
          </span>
        )}

        {/* View toggle — flip the live terminal ⇄ the DB
            rich conversation history WITHOUT killing the session. Shown only
            while a terminal is live (richViewAvailable); the session keeps
            running (the terminal stays mounted, hidden) in either view. */}
        {richViewAvailable && (
          <button
            type="button"
            onClick={() => onToggleRichView?.()}
            aria-pressed={!!richViewActive}
            title={richViewActive ? 'Back to the live terminal' : 'Show the conversation history'}
            className={`flex items-center justify-center w-7 h-7 rounded-lg border transition-colors cursor-pointer ${
              richViewActive
                ? 'bg-brand-50 border-brand/30 text-brand'
                : 'bg-p-surface border-p-border-light/60 dark:border-gray-700 text-p-text-secondary hover:bg-white dark:hover:bg-p-surface-hover hover:border-p-border'
            }`}
          >
            {richViewActive ? (
              // Terminal glyph — click returns to the live terminal.
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 9l3 3-3 3m5 0h3M4 5h16a1 1 0 011 1v12a1 1 0 01-1 1H4a1 1 0 01-1-1V6a1 1 0 011-1z" />
              </svg>
            ) : (
              // Chat-bubble glyph (speech bubble + text lines) — click shows
              // the rich conversation history.
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M4.6 5h14.8A1.6 1.6 0 0121 6.6v8.3a1.6 1.6 0 01-1.6 1.6h-6.9L8 20v-3.5H4.6A1.6 1.6 0 013 14.9V6.6A1.6 1.6 0 014.6 5z" />
                <path strokeLinecap="round" strokeWidth={2} d="M7.5 9.2h9M7.5 12.4h5.5" />
              </svg>
            )}
          </button>
        )}

        {/* Mode icon dropdown — hidden in interactive mode (the TUI owns its
            permission mode; our baseline + path enforcement still apply). */}
        {!hidePermissions && (() => {
          const mc = MODE_CONFIG[mode] || MODE_CONFIG.default
          const modeOptions = modeLocked
            ? [{ value: mode, label: mc.label }]
            : (meetingActive || supportsPlanMode === false) ? MODE_OPTIONS.filter(o => o.value !== 'plan') : MODE_OPTIONS
          return (
            <IconDropdown
              label="Permissions"
              value={mode}
              options={modeOptions}
              onChange={modeLocked ? () => {} : onModeChange}
              trigger={
                <span
                  className={`flex items-center justify-center w-7 h-7 rounded-lg border ${mc.bg} ${mc.border} ${mc.text} hover:brightness-95 transition-colors cursor-pointer`}
                  title={`Mode: ${mc.label}`}
                >
                  {mc.icon}
                </span>
              }
            />
          )
        })()}

        {/* Model icon dropdown */}
        {(() => {
          const matchedModel = resolvedModelOptions.find(m => m.value === model)
          const modelLabel = matchedModel?.label || model || 'Unknown'
          const modelLetter = getModelLetter(model)
          // A live interactive PTY's model can't be changed from here (use /model
          // in the TUI), so show only the active model read-only — the popup still
          // opens for the interactive switch.
          const activeValue = modelValueProp || model
          const lockedGroups = modelLocked
            ? [{ layer: 'active', layerLabel: '', models: [{ value: activeValue, label: modelLabel }] }]
            : modelGroups
          return (
            <IconDropdown
              label="Model"
              value={activeValue}
              options={modelLocked ? undefined : (!modelGroups ? resolvedModelOptions : undefined)}
              groups={lockedGroups}
              onChange={modelLocked ? () => {} : onModelChange}
              topSlot={interactiveAvailable
                ? <InteractiveToggle on={!!interactiveOn} disabled={!!interactiveDisabled} onToggle={onInteractiveToggle} />
                : undefined}
              trigger={
                <span
                  className="flex items-center justify-center w-7 h-7 rounded-lg border bg-p-surface border-p-border-light/60 dark:border-gray-700 text-p-text-secondary hover:bg-white dark:hover:bg-p-surface-hover hover:border-p-border transition-colors cursor-pointer text-xs font-semibold"
                  title={`Model: ${modelLabel}`}
                >
                  {modelLetter}
                </span>
              }
            />
          )
        })()}
      </div>
    </div>
  )
}

function AgentBadge({
  agents,
  label,
  bgClass,
  textClass,
  spinnerClass,
}: {
  agents: ActiveAgent[]
  label: string
  bgClass: string
  textClass: string
  spinnerClass: string
}) {
  if (agents.length === 0) return null
  return (
    <span
      className={`flex items-center gap-1 px-2 py-0.5 rounded-lg border ${bgClass} ${textClass}`}
      title={agents.map(a => a.description).join('\n')}
    >
      <span className={`inline-block w-2 h-2 border-[1.5px] ${spinnerClass} border-t-transparent rounded-full animate-spin`} />
      {agents.length} {label}
    </span>
  )
}
